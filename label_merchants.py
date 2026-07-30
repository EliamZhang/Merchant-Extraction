import argparse
import atexit
import csv
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import (
    china_timestamp_now,
    clean_output_value,
    extract_json_object,
    normalize_space,
    open_csv_dict_reader,
    post_json,
    safe_url,
    split_kb_keywords,
    KEYWORD_SEPARATOR,
)


DEFAULT_MERCHANT_KB = Path("merchant_kb.csv")
DEFAULT_CACHE = Path("cache/merchant_category_cache.json")
DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEFAULT_THINKING_TYPE = os.environ.get("DEEPSEEK_THINKING_TYPE", "none")
DEFAULT_REASONING_EFFORT = os.environ.get("DEEPSEEK_REASONING_EFFORT", "none")
MERCHANT_CATEGORIES = (
    "Automotive",
    "Department Stores",
    "Dining Out",
    "Donations",
    "Education",
    "Entertainment",
    "Financial Institutions",
    "Gambling",
    "Groceries",
    "Gyms and other memberships",
    "Health",
    "Home Improvement",
    "Information",
    "Insurance",
    "Personal Care",
    "Pet Care",
    "Rent",
    "Retail",
    "Subscription TV",
    "Telecommunications",
    "Transport",
    "Travel",
    "Utilities",
)
MERCHANT_CATEGORY_BY_CASEFOLD = {category.casefold(): category for category in MERCHANT_CATEGORIES}
KB_FIELDNAMES = [
    "merchant_name",
    "keywords",
    "link",
    "category",
    "category_source",
    "keyword_updated_at",
    "category_updated_at",
]


def clean_category(value: str) -> str:
    raw_category = clean_output_value(value)
    return MERCHANT_CATEGORY_BY_CASEFOLD.get(raw_category.casefold(), "")


def build_classification_cache_key(merchant_name: str, keywords: str, link: str) -> str:
    return "\n".join(
        (
            normalize_space(merchant_name).casefold(),
            normalize_space(keywords).casefold(),
            safe_url(link).casefold(),
        )
    )


def validate_kb_fieldnames(path: Path, reader: csv.DictReader) -> None:
    fieldnames = list(reader.fieldnames or [])
    missing = [col for col in KB_FIELDNAMES if col not in fieldnames]
    if missing and missing != ["category_source"]:
        raise ValueError(
            f"Merchant KB schema mismatch in {path}. "
            f"Missing columns {missing}. Expected {KB_FIELDNAMES}, got {fieldnames}."
        )


def normalize_kb_row(row: dict[str, str]) -> dict[str, str]:
    category = clean_category(row.get("category", ""))
    category_source = clean_output_value(row.get("category_source", ""))
    if category and not category_source:
        category_source = "AI"
    return {
        "merchant_name": normalize_space(row.get("merchant_name", "")),
        "keywords": KEYWORD_SEPARATOR.join(split_kb_keywords(row.get("keywords", ""))),
        "link": safe_url(row.get("link", "")),
        "category": category,
        "category_source": category_source,
        "keyword_updated_at": normalize_space(row.get("keyword_updated_at", "")),
        "category_updated_at": normalize_space(row.get("category_updated_at", "")),
    }


def load_merchant_kb_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    reader = open_csv_dict_reader(path)
    validate_kb_fieldnames(path, reader)
    return [normalize_kb_row({key: value or "" for key, value in row.items()}) for row in reader]


def write_merchant_kb_rows(path: Path, rows: list[dict[str, str]]) -> Path:
    target_path = path.resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f"{target_path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=KB_FIELDNAMES)
            writer.writeheader()
            writer.writerows(normalize_kb_row(row) for row in rows)
        for attempt in range(1, 4):
            try:
                temp_path.replace(target_path)
                return target_path
            except PermissionError:
                if attempt >= 3:
                    break
                time.sleep(1)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        recovery_path = target_path.with_name(
            f"{target_path.stem}.recovery.{os.getpid()}.{timestamp}{target_path.suffix}"
        )
        temp_path.replace(recovery_path)
        return recovery_path
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@dataclass
class MerchantClassification:
    merchant_name: str
    category: str
    link: str = ""
    reason: str = ""

    @classmethod
    def empty(cls, merchant_name: str = "", reason: str = "") -> "MerchantClassification":
        return cls(merchant_name=merchant_name, category="", link="", reason=reason)

    @classmethod
    def from_model_payload(cls, merchant_name: str, payload: dict[str, Any]) -> "MerchantClassification":
        category = clean_category(str(payload.get("category", "")))
        link = safe_url(str(payload.get("link", "")))
        reason = clean_output_value(str(payload.get("reason", "")))
        return cls(merchant_name=merchant_name, category=category, link=link, reason=reason)

    @classmethod
    def from_cache_payload(cls, payload: dict[str, Any]) -> "MerchantClassification":
        merchant_name = clean_output_value(str(payload.get("merchant_name", "")))
        category = clean_category(str(payload.get("category", "")))
        link = safe_url(str(payload.get("link", "")))
        reason = clean_output_value(str(payload.get("reason", "")))
        return cls(merchant_name=merchant_name, category=category, link=link, reason=reason)

    def should_cache(self) -> bool:
        return self.reason != "missing_batch_result" and not self.reason.startswith(
            "classification_failed:"
        )


@dataclass(frozen=True)
class MerchantCategoryPromptConfig:
    system_message: str = "Return strict JSON and nothing else."

    def build_batch_user_prompt(self, items: list[dict[str, str]]) -> str:
        payload = [
            {
                "id": item["id"],
                "merchant_name": item.get("merchant_name", ""),
                "keywords": item.get("keywords", ""),
                "link": item.get("link", ""),
            }
            for item in items
        ]
        return (
            # 你正在从商户知识库中对商户进行分类。
            "You are classifying merchants from a merchant knowledge base.\n"
            # 使用所有可用证据识别商户的商品、服务或经营活动；关键词和链接可作为强信号。仅在需要时使用网络搜索。
            "Use all evidence to identify the merchant's goods, services, or activity; keywords and link can be strong signals. Use web search only when needed.\n"
            # 根据商户真实的、实际经营的业务来分类，而不是看名字表面意思。
            "Classify each merchant by its actual, real-world business activity, not by a superficial reading of its name. "
            # 不需要完美匹配；当某个类别明显更合理时选最接近的分类。
            "A perfect match is not required; choose the closest category when one is clearly more defensible than the others. "
            # 仅在证据太弱或冲突时返回空字符串。
            "Use an empty string only when evidence is too weak or conflicting.\n"
            # 分类指南：
            "Category guide:\n"
            # 汽车：燃油、车辆销售、维修、零部件、洗车、道路救援服务。
            "- Automotive: fuel, vehicles, repairs, parts, car washes, roadside services.\n"
            # 百货商店：大型综合零售、折扣店、大型超市、百货连锁。
            "- Department Stores: large mixed-retail, discount, supercentre, department-store chains.\n"
            # 餐饮：餐厅、咖啡馆、酒吧、快餐、外卖、餐饮配送、预制餐食。
            "- Dining Out: restaurants, cafes, bars, fast food, delivery, catering, prepared meals.\n"
            # 捐赠：慈善机构、非营利组织、筹款、宗教捐赠。
            "- Donations: charities, non-profits, fundraising, religious giving.\n"
            # 教育：托儿所、学校、大学、辅导、培训。
            "- Education: childcare, schools, universities, tutoring, training.\n"
            # 娱乐：电影院、剧院、博物馆、景点、活动、俱乐部、音乐、游戏。
            "- Entertainment: cinemas, theatres, museums, attractions, events, clubs, music, games.\n"
            # 金融机构：银行、贷款机构、支付、抵押贷款、投资、券商。
            "- Financial Institutions: banks, lenders, payments, mortgages, investments, brokers.\n"
            # 赌博：赌场、博彩、投注、彩票、博彩场所。
            "- Gambling: casinos, betting, wagering, lotteries, gaming venues.\n"
            # 食品杂货：超市、食品店、面包店、酒类、食品供应商、批发商、加工商。
            "- Groceries: supermarkets, food shops, bakeries, liquor, food suppliers, wholesalers, processors.\n"
            # 健身及会员：健身房、健身、瑜伽、普拉提、体育训练、会员俱乐部。
            "- Gyms and other memberships: gyms, fitness, yoga, pilates, sports training, member clubs.\n"
            # 健康：药房、牙医、验光师、诊所、医院、医疗服务。
            "- Health: pharmacies, dentists, optometrists, clinics, hospitals, healthcare.\n"
            # 家居装修：建筑、工程、五金、清洁、维修、维护、设施、安保、工业服务。
            "- Home Improvement: construction, trades, hardware, cleaning, repairs, maintenance, facilities, security, industrial services.\n"
            # 信息：软件、IT、计算机服务、在线平台、媒体、出版、数据。
            "- Information: software, IT, computer services, online platforms, media, publishing, data.\n"
            # 保险：保险公司、经纪、保单、理赔、保修。
            "- Insurance: insurers, brokers, policies, claims, warranties.\n"
            # 个人护理：美发、美容、美甲、水疗、美妆、洗衣、裁缝、摄影。
            "- Personal Care: hair, beauty, nails, spas, grooming, laundry, tailoring, photography.\n"
            # 宠物：兽医、动物医院、宠物店、宠物食品、美容、寄养。
            "- Pet Care: vets, animal hospitals, pet shops, pet food, grooming, boarding.\n"
            # 租金：租金、租赁、物业管理、房地产中介、租赁中介、仓储。
            "- Rent: rent, leases, property managers, real estate agencies, rental agencies, storage.\n"
            # 零售：服装、鞋、珠宝、书籍、花店、礼品、电子产品、特色商品。
            "- Retail: clothing, shoes, jewellery, books, florists, gifts, electronics, specialty goods.\n"
            # 付费电视：有线电视、卫星电视、流媒体电视套餐、付费电视服务。
            "- Subscription TV: cable, satellite, streaming TV, paid television.\n"
            # 电信：移动、电话、互联网、宽带、网络、电信供应商。
            "- Telecommunications: mobile, phone, internet, broadband, network, telecom providers.\n"
            # 交通：公共交通、出租车、网约车、停车、过路费、货运、物流、配送、快递、车辆注册。
            "- Transport: public transport, taxis, rideshare, parking, tolls, freight, delivery, couriers, registration.\n"
            # 旅行：酒店、度假租赁、航空公司、旅行社、旅游、邮轮、租车。
            "- Travel: hotels, holiday rentals, airlines, travel agencies, tours, cruises, car rental.\n"
            # 公用事业：电、燃气、水、垃圾处理、税务、市政费、政府收费、罚款、公共服务。
            "- Utilities: electricity, gas, water, waste, taxes, council rates, government fees, fines, public services.\n"
            # 有把握时返回商户官网链接；否则返回空字符串。
            "Return a link to the merchant's official website when confident; otherwise return an empty string.\n"
            # 仅返回 JSON 对象，key 为 results。results 必须是数组，每个输入 id 对应一个结果。
            "Return JSON only as an object with key results. results must be an array with one result per input id.\n"
            # 每个结果必须包含 id, category, link, reason 字段。
            "Each result must have keys: id, category, link, reason.\n"
            f"Allowed category enum: {json.dumps(MERCHANT_CATEGORIES)}.\n"
            "Examples:\n"
            '- { "merchant_name": "Walmart", "keywords": "walmart supercenter retail department store", "category": "Department Stores" }\n'
            '- { "merchant_name": "DoorDash", "keywords": "doordash food delivery", "category": "Dining Out" }\n'
            '- { "merchant_name": "Commonwealth Bank", "keywords": "commonwealth bank of australia cba", "category": "Financial Institutions" }\n'
            '- { "merchant_name": "Australian Taxation Office", "keywords": "ato australian taxation office", "category": "Utilities" }\n'
            '- { "merchant_name": "ABC Food Suppliers", "keywords": "food supplier wholesale bakery", "category": "Groceries" }\n'
            '- { "merchant_name": "Smith Property Management", "keywords": "property management real estate rentals", "category": "Rent" }\n'
            '- { "merchant_name": "Metro IT Services", "keywords": "computer service software support", "category": "Information" }\n'
            f"items: {json.dumps(payload, ensure_ascii=False)}"
        )


class MerchantCategoryResponseValidator:
    def parse_batch(self, items: list[dict[str, str]], message: str) -> list[MerchantClassification]:
        payload_json = extract_json_object(message)
        raw_results = payload_json.get("results")
        if not isinstance(raw_results, list):
            raise ValueError("Batch model response must contain a results array.")

        result_by_id: dict[str, dict[str, Any]] = {}
        for raw_result in raw_results:
            if not isinstance(raw_result, dict):
                continue
            item_id = str(raw_result.get("id", ""))
            if item_id:
                result_by_id[item_id] = raw_result

        classifications: list[MerchantClassification] = []
        for item in items:
            merchant_name = item.get("merchant_name", "")
            payload = result_by_id.get(item["id"])
            if payload is None:
                classifications.append(MerchantClassification.empty(merchant_name, "missing_batch_result"))
                continue
            classifications.append(MerchantClassification.from_model_payload(merchant_name, payload))
        return classifications


class DeepSeekMerchantClassifier:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int,
        max_retries: int,
        retry_delay_seconds: float,
        thinking_type: str,
        reasoning_effort: str,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.thinking_type = thinking_type
        self.reasoning_effort = reasoning_effort
        self.prompt_config = MerchantCategoryPromptConfig()
        self.response_validator = MerchantCategoryResponseValidator()

    def classify_merchant_batch(self, items: list[dict[str, str]]) -> list[MerchantClassification]:
        if not items:
            return []

        prompt = self.prompt_config.build_batch_user_prompt(items)
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                message = self._chat_completion(prompt)
                return self.response_validator.parse_batch(items, message)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt >= self.max_retries:
                break
            time.sleep(self.retry_delay_seconds * attempt)
        raise RuntimeError(
            f"Batch classification failed after {self.max_retries} retries: {last_error}"
        )

    def _chat_completion(self, prompt: str) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": self.prompt_config.system_message,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "enable_search": True,
            "search_enabled": True,
        }
        if self.thinking_type and self.thinking_type.casefold() != "none":
            body["thinking"] = {"type": self.thinking_type}
        if self.reasoning_effort and self.reasoning_effort.casefold() != "none":
            body["reasoning_effort"] = self.reasoning_effort

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response_json = post_json(self.api_key, self.base_url, "/chat/completions", body, self.timeout_seconds)
                return str(response_json["choices"][0]["message"]["content"])
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            if attempt >= self.max_retries:
                break
            time.sleep(self.retry_delay_seconds * attempt)

        raise RuntimeError(f"Chat completion failed after {self.max_retries} retries: {last_error}")


class CacheStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, dict[str, Any]] = {}

    def load(self) -> None:
        if not self.path.exists():
            self.records = {}
            return
        with self.path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict) and isinstance(payload.get("records"), dict):
            self.records = payload["records"]
        else:
            self.records = {}

    def save(self) -> None:
        target_path = self.path.resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_name(f"{target_path.name}.{os.getpid()}.tmp")
        payload = {"records": self.records}
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            temp_path.replace(target_path)
        except OSError as exc:
            print(f"Warning: failed to save cache path={target_path}: {exc}", flush=True)
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, cache_key: str) -> MerchantClassification | None:
        payload = self.records.get(cache_key)
        if not payload:
            return None
        classification = MerchantClassification.from_cache_payload(payload)
        if not classification.should_cache():
            return None
        return classification

    def set(self, cache_key: str, classification: MerchantClassification) -> None:
        self.records[cache_key] = asdict(classification)


def build_classification_items(
    rows: list[dict[str, str]],
    only_missing: bool = True,
    row_limit: int | None = None,
) -> list[tuple[int, dict[str, str]]]:
    items: list[tuple[int, dict[str, str]]] = []
    for idx, row in enumerate(rows):
        if row_limit is not None and len(items) >= row_limit:
            break
        merchant_name = normalize_space(row.get("merchant_name", ""))
        if not merchant_name:
            continue
        if only_missing and clean_category(row.get("category", "")):
            continue
        keywords = normalize_space(row.get("keywords", ""))
        link = safe_url(row.get("link", ""))
        items.append(
            (
                idx,
                {
                    "id": str(idx),
                    "merchant_name": merchant_name,
                    "keywords": keywords,
                    "link": link,
                    "cache_key": build_classification_cache_key(merchant_name, keywords, link),
                },
            )
        )
    return items


def classify_merchant_kb(
    path: Path = DEFAULT_MERCHANT_KB,
    output_path: Path | None = None,
    cache_path: Path = DEFAULT_CACHE,
    api_key: str = "",
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    batch_size: int = 5,
    timeout_seconds: int = 120,
    max_retries: int = 20,
    retry_delay_seconds: float = 15.0,
    thinking_type: str = DEFAULT_THINKING_TYPE,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    only_missing: bool = True,
    row_limit: int | None = None,
    dry_run: bool = False,
    save_every_batches: int = 50,
    cache_save_every_batches: int = 50,
    progress: bool = True,
    verbose: bool = False,
) -> dict[str, int]:
    if progress:
        print(f"Reading merchant KB path={path}", flush=True)
    rows = load_merchant_kb_rows(path)
    if progress:
        print(f"Read merchant KB rows_total={len(rows)}", flush=True)
        print(
            f"Selecting merchants for classification only_missing={only_missing} row_limit={row_limit}",
            flush=True,
        )
    indexed_items = build_classification_items(rows, only_missing=only_missing, row_limit=row_limit)
    cache_store = CacheStore(cache_path)
    if progress:
        print(f"Loading classification cache path={cache_path}", flush=True)
    cache_store.load()
    stats = {
        "rows_total": len(rows),
        "rows_selected": len(indexed_items),
        "rows_api_pending": 0,
        "rows_classified": 0,
        "rows_updated": 0,
        "api_calls": 0,
        "cache_hits": 0,
        "saves": 0,
        "recovery_saves": 0,
    }
    if not api_key:
        if not dry_run:
            raise ValueError("Missing DeepSeek API key.")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1.")
    if save_every_batches < 0:
        raise ValueError("save_every_batches must be 0 or greater.")
    if cache_save_every_batches < 0:
        raise ValueError("cache_save_every_batches must be 0 or greater.")

    final_output_path = output_path or path
    dirty = False
    cache_dirty = False
    last_saved_api_call = -1

    def save_rows(reason: str, force: bool = False) -> None:
        nonlocal dirty, final_output_path, last_saved_api_call
        if not force and save_every_batches <= 0:
            return
        if not force and not dirty:
            return
        if not force and last_saved_api_call == stats["api_calls"]:
            return
        if progress:
            print(
                f"Saving merchant KB reason={reason} path={final_output_path} "
                f"rows_updated={stats['rows_updated']}",
                flush=True,
            )
        requested_output_path = final_output_path
        saved_path = write_merchant_kb_rows(final_output_path, rows)
        if saved_path != requested_output_path.resolve():
            stats["recovery_saves"] += 1
            final_output_path = saved_path
            if progress:
                print(
                    f"Original output was locked; saved recovery file path={saved_path}",
                    flush=True,
                )
        stats["saves"] += 1
        last_saved_api_call = stats["api_calls"]
        dirty = False
        if progress:
            print(f"Save complete path={final_output_path} saves={stats['saves']}", flush=True)

    def print_exit_summary(reason: str) -> None:
        if stats["api_calls"] <= 0 and stats["cache_hits"] <= 0:
            return
        print(
            f"Classification interrupted reason={reason} "
            f"selected={stats['rows_selected']} "
            f"classified={stats['rows_classified']} "
            f"updated={stats['rows_updated']} "
            f"remaining={max(stats['rows_selected'] - stats['rows_classified'], 0)} "
            f"api_calls={stats['api_calls']} "
            f"saves={stats['saves']} "
            f"output={final_output_path}",
            flush=True,
        )

    def save_cache_if_dirty() -> None:
        nonlocal cache_dirty
        if not cache_dirty:
            return
        cache_store.save()
        cache_dirty = False

    def save_on_exit() -> None:
        if stats["api_calls"] <= 0 and stats["cache_hits"] <= 0:
            return
        save_cache_if_dirty()
        if dirty:
            save_rows("exit", force=True)
        print_exit_summary("exit")

    api_items = indexed_items
    if only_missing and indexed_items:
        api_items = []
        for row_index, item in indexed_items:
            classification = cache_store.get(item["cache_key"])
            if classification is None:
                api_items.append((row_index, item))
                continue
            stats["cache_hits"] += 1
            stats["rows_classified"] += 1
            cached_link = safe_url(classification.link)
            if cached_link and rows[row_index].get("link", "") != cached_link:
                rows[row_index]["link"] = cached_link
                dirty = True
            category = clean_category(classification.category)
            if not category:
                continue
            if rows[row_index].get("category", "") == category:
                continue
            rows[row_index]["category"] = category
            rows[row_index]["category_source"] = "AI"
            rows[row_index]["category_updated_at"] = china_timestamp_now()
            dirty = True
            stats["rows_updated"] += 1
            if verbose:
                print(
                    f"Cache hit merchant={item['merchant_name']!r} category={category!r}",
                    flush=True,
                )
    stats["rows_api_pending"] = len(api_items)
    if progress:
        print(
            f"Selected merchants rows_selected={len(indexed_items)} "
            f"cache_hits={stats['cache_hits']} api_pending={stats['rows_api_pending']}",
            flush=True,
        )
    if dry_run:
        if progress:
            print("Skipping classification reason=dry_run", flush=True)
        return stats
    if not api_items:
        if dirty or output_path is not None:
            save_rows("cache_only", force=True)
        if progress:
            print("Skipping classification reason=no_api_pending_rows", flush=True)
        return stats

    client = DeepSeekMerchantClassifier(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
        thinking_type=thinking_type,
        reasoning_effort=reasoning_effort,
    )
    batch_total = (len(api_items) + batch_size - 1) // batch_size
    if progress:
        print(
            f"Classifying merchants batches={batch_total} batch_size={batch_size} "
            f"model={model} thinking_type={thinking_type} reasoning_effort={reasoning_effort}",
            flush=True,
        )

    atexit.register(save_on_exit)
    for start in range(0, len(api_items), batch_size):
        batch = api_items[start : start + batch_size]
        batch_number = start // batch_size + 1
        if progress:
            print(
                f"Batch {batch_number}/{batch_total} start rows={len(batch)} "
                f"classified={stats['rows_classified']} updated={stats['rows_updated']}",
                flush=True,
            )
        classifications = client.classify_merchant_batch([item for _, item in batch])
        stats["api_calls"] += 1
        batch_failures = 0
        for (row_index, item), classification in zip(batch, classifications):
            stats["rows_classified"] += 1
            if classification.should_cache():
                cache_store.set(item["cache_key"], classification)
                cache_dirty = True
            resolved_link = safe_url(classification.link)
            if resolved_link and rows[row_index].get("link", "") != resolved_link:
                rows[row_index]["link"] = resolved_link
                dirty = True
            category = clean_category(classification.category)
            if not category:
                batch_failures += 1
                if classification.reason:
                    print(
                        f"Skip merchant={item['merchant_name']!r} reason={classification.reason!r}",
                        flush=True,
                    )
                continue
            if rows[row_index].get("category", "") == category:
                continue
            rows[row_index]["category"] = category
            rows[row_index]["category_source"] = "AI"
            rows[row_index]["category_updated_at"] = china_timestamp_now()
            dirty = True
            stats["rows_updated"] += 1
            if verbose:
                print(
                    f"Classified merchant={rows[row_index].get('merchant_name', '')!r} category={category!r}",
                    flush=True,
                )
        if cache_save_every_batches and stats["api_calls"] % cache_save_every_batches == 0:
            save_cache_if_dirty()
        if progress:
            print(
                f"Batch {batch_number}/{batch_total} done "
                f"classified={stats['rows_classified']} updated={stats['rows_updated']} "
                f"failures={batch_failures} api_calls={stats['api_calls']}",
                flush=True,
            )
        if save_every_batches and stats["api_calls"] % save_every_batches == 0:
            save_rows(f"batch_{batch_number}_of_{batch_total}")

    if dirty or output_path is not None:
        save_rows("final", force=True)
    save_cache_if_dirty()
    atexit.unregister(save_on_exit)
    return stats


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify merchants in merchant_kb.csv and fill the category column."
    )
    parser.add_argument("--merchant-kb", type=Path, default=DEFAULT_MERCHANT_KB)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help="Cache prior classification results, including empty categories, to avoid repeat API calls.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=20)
    parser.add_argument("--retry-delay-seconds", type=float, default=15.0)
    parser.add_argument(
        "--thinking-type",
        default=DEFAULT_THINKING_TYPE,
        help='Thinking mode sent as thinking.type. Defaults to none. Set "enabled" to include thinking.',
    )
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REASONING_EFFORT,
        help='Reasoning effort sent to the model. Defaults to none.',
    )
    parser.add_argument("--row-limit", type=int, default=None)
    parser.add_argument(
        "--save-every",
        type=int,
        default=10,
        help="Save merchant KB every N API batches. Default is 10. Use 0 to save only at the end or on exit.",
    )
    parser.add_argument(
        "--cache-save-every",
        type=int,
        default=20,
        help="Save classification cache every N API batches. Use 1 for safest writes or 0 to save only at the end/on exit.",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Reclassify rows that already have a valid category.",
    )
    parser.add_argument(
        "--dry-run-stats",
        action="store_true",
        help="Print selected row counts without calling DeepSeek or writing output.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if not args.api_key and not args.dry_run_stats:
        parser.error("Missing DeepSeek API key. Set DEEPSEEK_API_KEY or pass --api-key.")
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1.")
    if args.max_retries < 1:
        parser.error("--max-retries must be at least 1.")
    if args.save_every < 0:
        parser.error("--save-every must be 0 or greater.")
    if args.cache_save_every < 0:
        parser.error("--cache-save-every must be 0 or greater.")

    stats = classify_merchant_kb(
        path=args.merchant_kb,
        output_path=args.output,
        cache_path=args.cache,
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        batch_size=args.batch_size,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        retry_delay_seconds=args.retry_delay_seconds,
        thinking_type=args.thinking_type,
        reasoning_effort=args.reasoning_effort,
        only_missing=not args.include_existing,
        row_limit=args.row_limit,
        dry_run=args.dry_run_stats,
        save_every_batches=args.save_every,
        cache_save_every_batches=args.cache_save_every,
        verbose=args.verbose,
    )
    print(
        "Classification stats "
        f"rows_total={stats['rows_total']} "
        f"rows_selected={stats['rows_selected']} "
        f"rows_api_pending={stats['rows_api_pending']} "
        f"rows_classified={stats['rows_classified']} "
        f"rows_updated={stats['rows_updated']} "
        f"api_calls={stats['api_calls']} "
        f"cache_hits={stats['cache_hits']} "
        f"saves={stats['saves']} "
        f"recovery_saves={stats['recovery_saves']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
