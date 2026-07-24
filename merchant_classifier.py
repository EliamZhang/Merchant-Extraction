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
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEFAULT_THINKING_TYPE = os.environ.get("DEEPSEEK_THINKING_TYPE", "enabled")
DEFAULT_REASONING_EFFORT = os.environ.get("DEEPSEEK_REASONING_EFFORT", "xhigh")
MERCHANT_CATEGORIES = (
    "Automotive",
    "Department Stores",
    "Dining Out",
    "Donations",
    "Education",
    "Entertainment",
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
    if fieldnames != KB_FIELDNAMES:
        raise ValueError(
            f"Merchant KB schema mismatch in {path}. "
            f"Expected columns {KB_FIELDNAMES}, got {fieldnames}."
        )


def normalize_kb_row(row: dict[str, str]) -> dict[str, str]:
    return {
        "merchant_name": normalize_space(row.get("merchant_name", "")),
        "keywords": KEYWORD_SEPARATOR.join(split_kb_keywords(row.get("keywords", ""))),
        "link": safe_url(row.get("link", "")),
        "category": clean_category(row.get("category", "")),
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
            "You are classifying merchants from a merchant knowledge base.\n"
            "Use merchant_name as the primary evidence. Use keywords and link only as supporting evidence.\n"
            "Use web search to check the merchant's official website or reliable business listings before deciding the category.\n"
            "Choose exactly one category when the merchant's business type clearly fits the enum. "
            "Use an empty string when the category is unclear.\n"
            "Return a link to the merchant's official website when confident; otherwise return an empty string.\n"
            "Return JSON only as an object with key results. results must be an array with one result per input id.\n"
            "Each result must have keys: id, category, link, reason.\n"
            f"Allowed category enum: {json.dumps(MERCHANT_CATEGORIES)}.\n"
            "Examples:\n"
            '- { "merchant_name": "Walmart", "keywords": "walmart supercenter retail department store", "category": "Department Stores" }\n'
            '- { "merchant_name": "Spotify", "keywords": "spotify music streaming", "category": "Entertainment" }\n'
            '- { "merchant_name": "Uber", "keywords": "uber ride rideshare", "category": "Transport" }\n'
            '- { "merchant_name": "CVS Pharmacy", "keywords": "cvs pharmacy drugstore health", "category": "Health" }\n'
            '- { "merchant_name": "DoorDash", "keywords": "doordash food delivery", "category": "Dining Out" }\n'
            '- { "merchant_name": "Hertz", "keywords": "hertz car rental", "category": "Travel" }\n'
            '- { "merchant_name": "Sephora", "keywords": "sephora cosmetics beauty makeup", "category": "Personal Care" }\n'
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
        try:
            message = self._chat_completion(prompt)
            return self.response_validator.parse_batch(items, message)
        except Exception as exc:
            return [
                MerchantClassification.empty(item.get("merchant_name", ""), f"classification_failed: {exc}")
                for item in items
            ]

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
    max_retries: int = 5,
    retry_delay_seconds: float = 2.0,
    thinking_type: str = DEFAULT_THINKING_TYPE,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    only_missing: bool = True,
    row_limit: int | None = None,
    dry_run: bool = False,
    save_every_batches: int = 10,
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

    def save_on_exit() -> None:
        nonlocal cache_dirty
        if stats["api_calls"] <= 0 and stats["cache_hits"] <= 0:
            return
        if cache_dirty:
            cache_store.save()
            cache_dirty = False
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
            rows[row_index]["category_updated_at"] = china_timestamp_now()
            dirty = True
            stats["rows_updated"] += 1
            if verbose:
                print(
                    f"Classified merchant={rows[row_index].get('merchant_name', '')!r} category={category!r}",
                    flush=True,
                )
        if cache_dirty:
            cache_store.save()
            cache_dirty = False
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
    if cache_dirty:
        cache_store.save()
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
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--retry-delay-seconds", type=float, default=3.0)
    parser.add_argument(
        "--thinking-type",
        default=DEFAULT_THINKING_TYPE,
        help='Thinking mode sent as thinking.type. Defaults to enabled. Use "none" to omit.',
    )
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REASONING_EFFORT,
        help='Reasoning effort sent to the model. Defaults to xhigh. Use "none" to omit.',
    )
    parser.add_argument("--row-limit", type=int, default=None)
    parser.add_argument(
        "--save-every",
        type=int,
        default=10,
        help="Save merchant KB every N API batches. Default is 10. Use 0 to save only at the end or on exit.",
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
