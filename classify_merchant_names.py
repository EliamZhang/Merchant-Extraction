import argparse
import atexit
import csv
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request

DEFAULT_INPUT = Path("merchant_names.csv")
DEFAULT_OUTPUT = Path("output/merchant_names_categorized.csv")
DEFAULT_CACHE = Path("cache/merchant_names_category_cache.json")
CHINA_TIMEZONE = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))

MERCHANT_CATEGORIES = (
    "Automotive",
    "Debt Collection",
    "Debt Consolidation",
    "Department Stores",
    "Dining Out",
    "Donations",
    "Education",
    "Entertainment",
    "Financial Services",
    "Gambling",
    "Groceries",
    "Gyms and other memberships",
    "Health",
    "Home Improvement",
    "Information",
    "Insurance",
    "Personal Care",
    "Pet Care",
    "Professional Services",
    "Property and Strata",
    "Rent",
    "Retail",
    "Subscription TV",
    "Telecommunications",
    "Transport",
    "Travel",
    "Utilities",
)
CATEGORY_BY_CASEFOLD = {c.casefold(): c for c in MERCHANT_CATEGORIES}


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_category(value: str) -> str:
    raw = normalize_space(value).strip('"').strip("'")
    return CATEGORY_BY_CASEFOLD.get(raw.casefold(), "")


def china_timestamp_now() -> str:
    from datetime import datetime, timezone, timedelta

    return datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("Empty model response.")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"Model response does not contain JSON: {text[:200]}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Model JSON response is not an object.")
    return parsed


class CacheStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, str] = {}

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

    def get(self, name: str) -> str:
        return self.records.get(name.casefold(), "")

    def set(self, name: str, category: str) -> None:
        self.records[name.casefold()] = category


def load_old_cache_mapping(path: Path) -> dict[str, str]:
    """Extract name→category from the old merchant_category_cache.json."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    records = payload.get("records", {}) if isinstance(payload, dict) else {}
    mapping: dict[str, str] = {}
    for key, value in records.items():
        category = clean_category(str(value.get("category", "")))
        if not category:
            continue
        name = normalize_space(value.get("merchant_name", ""))
        if name:
            mapping[name.casefold()] = category
    return mapping


class DeepSeekClassifier:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int,
        max_retries: int,
        retry_delay_seconds: float,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds

    def classify_batch(self, names: list[str]) -> list[str]:
        if not names:
            return []
        prompt = self._build_prompt(names)
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                message = self._chat_completion(prompt)
                return self._parse_batch(names, message)
            except Exception as exc:
                last_error = exc
            if attempt >= self.max_retries:
                break
            time.sleep(self.retry_delay_seconds * attempt)
        return [""] * len(names)

    def _build_prompt(self, names: list[str]) -> str:
        items = [{"id": str(i), "merchant_name": name} for i, name in enumerate(names)]
        return (
            "You are classifying merchants into industry categories.\n"
            "Use only the merchant_name field to decide the category.\n"
            "If your API supports web search, verify the merchant's business type online.\n"
            "Choose exactly one category from the enum when the business type clearly fits.\n"
            "Use an empty string when the category is unclear.\n"
            "Return JSON only as an object with key results. results must be an array with one result per input id.\n"
            "Each result must have keys: id, category.\n"
            f"Allowed category enum: {json.dumps(MERCHANT_CATEGORIES)}.\n"
            f"items: {json.dumps(items, ensure_ascii=False)}"
        )

    def _parse_batch(self, names: list[str], message: str) -> list[str]:
        payload = extract_json_object(message)
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ValueError("Batch model response must contain a results array.")
        result_by_id: dict[str, str] = {}
        for r in raw_results:
            if isinstance(r, dict):
                rid = str(r.get("id", ""))
                cat = clean_category(str(r.get("category", "")))
                if rid:
                    result_by_id[rid] = cat
        return [result_by_id.get(str(i), "") for i in range(len(names))]

    def _chat_completion(self, prompt: str) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "Return strict JSON and nothing else."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "enable_search": True,
            "search_enabled": True,
        }
        response_json = self._post_json("/chat/completions", body)
        return str(response_json["choices"][0]["message"]["content"])

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        req = request.Request(url, data=data, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Network error: {exc.reason}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify merchants in merchant_names.csv")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--old-cache", type=Path, default=Path("cache/merchant_category_cache.json"),
        help="Reuse categories from the old merchant_category_cache.json",
    )
    parser.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=float, default=2.0)
    parser.add_argument("--row-limit", type=int, default=None)
    parser.add_argument("--save-every", type=int, default=10, help="Save output every N API batches")
    parser.add_argument("--max-api-calls", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("Missing DeepSeek API key. Set DEEPSEEK_API_KEY or pass --api-key.")

    print(f"Loading old cache path={args.old_cache}", flush=True)
    old_mapping = load_old_cache_mapping(args.old_cache)
    print(f"Old cache loaded entries_with_category={len(old_mapping)}", flush=True)

    print(f"Reading input path={args.input}", flush=True)
    raw_text = args.input.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw_text, newline=""))
    all_names: list[str] = []
    for row in reader:
        name = normalize_space(row.get("merchant_name", ""))
        if name:
            all_names.append(name)
        if args.row_limit and len(all_names) >= args.row_limit:
            break
    print(f"Read merchants total={len(all_names)}", flush=True)

    cache = CacheStore(args.cache)
    cache.load()
    print(f"New cache loaded entries={len(cache.records)}", flush=True)

    # merge old cache into new cache for names not yet covered
    old_cache_used = 0
    for name in all_names:
        key = name.casefold()
        if key not in cache.records and key in old_mapping:
            cache.records[key] = old_mapping[key]
            old_cache_used += 1
    if old_cache_used:
        print(f"Old cache entries applied to new cache count={old_cache_used}", flush=True)

    stats = {
        "rows_total": len(all_names),
        "rows_classified": 0,
        "api_calls": 0,
        "cache_hits": 0,
    }

    pending: list[tuple[int, str]] = []
    for idx, name in enumerate(all_names):
        cat = cache.get(name)
        if cat:
            stats["cache_hits"] += 1
            continue
        pending.append((idx, name))

    print(
        f"Classification pending rows_total={stats['rows_total']} "
        f"cache_hits={stats['cache_hits']} api_pending={len(pending)}",
        flush=True,
    )
    categories = [cache.get(name) for name in all_names]

    if not pending:
        print("All merchants already categorized.", flush=True)
        return _write_output(args.output, all_names, categories, stats)

    client = DeepSeekClassifier(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        retry_delay_seconds=args.retry_delay_seconds,
    )
    batch_total = (len(pending) + args.batch_size - 1) // args.batch_size
    print(
        f"Classifying batches={batch_total} batch_size={args.batch_size} model={args.model}",
        flush=True,
    )

    output_path = args.output.resolve()
    dirty = False

    def save_output() -> None:
        nonlocal dirty
        if not dirty:
            return
        _write_output(output_path, all_names, categories, stats)
        dirty = False

    def on_exit() -> None:
        if stats["api_calls"] <= 0:
            return
        cache.save()
        save_output()

    atexit.register(on_exit)

    for batch_num in range(batch_total):
        start = batch_num * args.batch_size
        batch = pending[start : start + args.batch_size]

        if args.max_api_calls and stats["api_calls"] >= args.max_api_calls:
            print(f"Reached max_api_calls={args.max_api_calls}, stopping.", flush=True)
            break

        if args.verbose:
            names_preview = ", ".join(n[:30] for _, n in batch[:5])
            print(
                f"Batch {batch_num + 1}/{batch_total} size={len(batch)} names=[{names_preview}...]",
                flush=True,
            )

        results = client.classify_batch([name for _, name in batch])
        stats["api_calls"] += 1

        for (idx, name), category in zip(batch, results):
            stats["rows_classified"] += 1
            categories[idx] = category
            if category:
                cache.set(name, category)
            else:
                cache.set(name, "")  # cache empty to avoid re-query

        cache.save()
        dirty = True

        if args.verbose:
            for (_, name), cat in zip(batch[:5], results[:5]):
                print(f"  {name[:50]} → {cat or '(unclear)'}", flush=True)

        classified_total = stats["cache_hits"] + stats["rows_classified"]
        print(
            f"Batch {batch_num + 1}/{batch_total} done "
            f"api_calls={stats['api_calls']} "
            f"classified={classified_total}/{stats['rows_total']} "
            f"({classified_total * 100 // stats['rows_total']}%)",
            flush=True,
        )

        if args.save_every and stats["api_calls"] % args.save_every == 0:
            save_output()

    save_output()
    cache.save()
    atexit.unregister(on_exit)

    classified_total = stats["cache_hits"] + stats["rows_classified"]
    print(
        f"Done. rows_total={stats['rows_total']} cache_hits={stats['cache_hits']} "
        f"api_classified={stats['rows_classified']} api_calls={stats['api_calls']} "
        f"total_classified={classified_total} output={output_path}",
        flush=True,
    )
    return 0


def _write_output(path: Path, names: list[str], categories: list[str], stats: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["merchant_name", "category"])
        for name, cat in zip(names, categories):
            writer.writerow([name, cat])


if __name__ == "__main__":
    sys.exit(main())
