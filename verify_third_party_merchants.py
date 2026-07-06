from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_INPUT = Path("third_party_dedup.csv")
DEFAULT_OUTPUT = Path("output/third_party_dedup_verified.csv")
DEFAULT_CACHE = Path("cache/deepseek_third_party_cache_v3.json")
EMPTY_FIELDS = ("standardized", "keyword", "link")
GENERIC_TRANSFER_PREFIXES = (
    "osko payment",
    "osko direct",
    "online pymt",
    "pending payment",
    "pending mts",
    "direct cba",
    "npp transfer",
    "fast pymt",
    "fast mr",
    "fast mrs",
    "payment mr",
    "payment mrs",
    "payment miss",
    "direct cr from",
    "cba netbank",
    "anz internet",
    "anz internet banking",
    "bank of",
    "bank of qld",
    "bank of queensla",
    "the trustee",
)
SPREADSHEET_ERRORS = {
    "#name?",
    "#value!",
    "#ref!",
    "#div/0!",
    "#n/a",
    "#num!",
    "#null!",
}
DATE_PATTERNS = (
    re.compile(r"^\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}$", flags=re.IGNORECASE),
    re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$"),
    re.compile(r"^[A-Za-z]{3}[-/]\d{1,2}[-/]\d{2,4}$", flags=re.IGNORECASE),
)
NOISE_TERMS = {
    "refund",
    "payment",
    "pay",
    "payroll",
    "salary",
    "cheque",
    "chq",
    "value",
    "date",
}
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_search_text(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]+", " ", value or "")
    return normalize_space(cleaned).casefold()


def alpha_count(value: str) -> int:
    return sum(1 for char in value if char.isalpha())


def digit_count(value: str) -> int:
    return sum(1 for char in value if char.isdigit())


def safe_url(value: str) -> str:
    url = normalize_space(value)
    if not url:
        return ""
    if re.match(r"^https?://", url, flags=re.IGNORECASE):
        return url
    return ""


def clean_output_value(value: str) -> str:
    return normalize_space(value).strip('"').strip("'")


def shorten_text(value: str, max_len: int = 72) -> str:
    text = normalize_space(value)
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3]}..."


def format_seconds(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def extract_literal_substring(raw_value: str, candidate: str) -> str:
    raw_text = normalize_space(raw_value)
    candidate_text = normalize_space(candidate)
    if not raw_text or not candidate_text:
        return ""

    direct_start = raw_text.casefold().find(candidate_text.casefold())
    if direct_start != -1:
        return raw_text[direct_start : direct_start + len(candidate_text)]

    raw_tokens = [(match.group(0).casefold(), match.start(), match.end()) for match in TOKEN_PATTERN.finditer(raw_text)]
    candidate_tokens = [match.group(0).casefold() for match in TOKEN_PATTERN.finditer(candidate_text)]
    if not raw_tokens or not candidate_tokens or len(candidate_tokens) > len(raw_tokens):
        return ""

    raw_token_values = [token for token, _, _ in raw_tokens]
    window_size = len(candidate_tokens)
    for start_index in range(len(raw_token_values) - window_size + 1):
        if raw_token_values[start_index : start_index + window_size] == candidate_tokens:
            start_offset = raw_tokens[start_index][1]
            end_offset = raw_tokens[start_index + window_size - 1][2]
            return raw_text[start_offset:end_offset]
    return ""


def align_keyword_to_third_party(raw_value: str, keyword: str, standardized: str) -> str:
    for candidate in (keyword, standardized):
        literal = extract_literal_substring(raw_value, candidate)
        if literal:
            return literal
    return ""


def build_candidate_score(
    raw_value: str,
    frequency: int,
    repeated_prefix_support: int,
) -> tuple[int, int, int, int, int, int]:
    search_text = normalize_search_text(raw_value)
    tokens = search_text.split()
    noise_penalty = sum(1 for token in tokens if token in NOISE_TERMS)
    return (
        repeated_prefix_support,
        -noise_penalty,
        frequency,
        alpha_count(raw_value) - digit_count(raw_value),
        -len(tokens),
        -len(search_text),
    )


def should_skip_without_api(raw_value: str) -> bool:
    cleaned = normalize_space(raw_value)
    if not cleaned:
        return True
    lowered = cleaned.casefold()
    if lowered in SPREADSHEET_ERRORS:
        return True
    return any(pattern.match(cleaned) for pattern in DATE_PATTERNS)


def is_generic_transfer_prefix(prefix_text: str) -> bool:
    return any(prefix_text.startswith(prefix) for prefix in GENERIC_TRANSFER_PREFIXES)


def best_repeated_prefix(tokens: list[str], prefix_counts: dict[str, int]) -> tuple[str, int]:
    best_prefix = ""
    best_support = 1
    upper = min(4, len(tokens))
    for size in range(upper, 1, -1):
        prefix_text = " ".join(tokens[:size])
        if is_generic_transfer_prefix(prefix_text):
            continue
        count = prefix_counts.get(prefix_text, 0)
        if count >= 2 and (count > best_support or (count == best_support and size > len(best_prefix.split()))):
            best_prefix = prefix_text
            best_support = count
    return best_prefix, best_support


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


@dataclass
class MerchantDecision:
    is_real_merchant: bool
    standardized: str
    keyword: str
    link: str
    reason: str = ""

    @classmethod
    def empty(cls, reason: str = "") -> "MerchantDecision":
        return cls(
            is_real_merchant=False,
            standardized="",
            keyword="",
            link="",
            reason=reason,
        )

    @classmethod
    def from_model_payload(cls, payload: dict[str, Any]) -> "MerchantDecision":
        is_real = bool(payload.get("is_real_merchant"))
        standardized = clean_output_value(str(payload.get("standardized", "")))
        keyword = clean_output_value(str(payload.get("keyword", "")))
        link = safe_url(str(payload.get("link", "")))
        reason = clean_output_value(str(payload.get("reason", "")))

        if is_real and standardized and link:
            if not keyword:
                keyword = standardized
            return cls(
                is_real_merchant=True,
                standardized=standardized,
                keyword=keyword,
                link=link,
                reason=reason,
            )
        return cls.empty(reason=reason)

    def should_cache(self) -> bool:
        return not self.reason.startswith("verification_failed:")


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int,
        max_retries: int,
        retry_delay_seconds: float,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.extra_body = extra_body or {}

    def verify_merchant(self, third_party_value: str, prefix_hint: str = "") -> MerchantDecision:
        hint_line = ""
        if prefix_hint:
            hint_line = (
                f"Frequent leading phrase from similar third_party strings: {json.dumps(prefix_hint, ensure_ascii=False)}.\n"
                "Use this only as a hint if it is clearly part of the same merchant name.\n"
            )
        prompt = (
            "You are verifying whether a bank-transaction counterparty string refers to a real merchant.\n"
            "Use only the provided third_party text. Do not rely on any other fields.\n"
            "These strings can contain noise such as bank-channel labels, person names, payroll text, refund codes, dates, or reference IDs.\n"
            "If a plausible merchant entity can be isolated from the third_party text alone, verify that merchant and return its standardized name.\n"
            "If your API/provider supports web search, use it to confirm the merchant exists online.\n"
            "If you cannot confidently confirm it is a real merchant, respond with is_real_merchant=false and leave fields blank.\n"
            "Return JSON only with keys: is_real_merchant, standardized, keyword, link, reason.\n"
            "Rules:\n"
            "- standardized: official or commonly accepted merchant name, without bank noise.\n"
            "- keyword: copy a literal phrase directly from the provided third_party text. Do not rewrite, normalize, translate, or invent it.\n"
            "- keyword must come from third_party itself, but standardized can be normalized.\n"
            "- link: best verification URL, prefer official website, otherwise Google Maps or another reliable directory.\n"
            "- If the value is empty, personal, invalid, generic, or not a merchant, set false and keep all fields blank.\n"
            "- Example: 'One Click Life OCNxtDy745U' can map to standardized='One Click Life' if that merchant is real.\n"
            "- Example: 'ACCESSABILITY WA LISA PITCHE' can map to standardized='Accessability WA' if that merchant is real.\n"
            "- Example: 'CASH CONVERTERS CCS Perth WA AUS C' should use keyword='CASH CONVERTERS' and standardized='Cash Converters'.\n"
            f"{hint_line}"
            f"third_party: {json.dumps(third_party_value, ensure_ascii=False)}"
        )

        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "Return strict JSON and nothing else.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        body.update(self.extra_body)

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            for payload in self._request_variants(body):
                try:
                    response_json = self._post_json("/chat/completions", payload)
                    message = response_json["choices"][0]["message"]["content"]
                    payload_json = extract_json_object(message)
                    decision = MerchantDecision.from_model_payload(payload_json)
                    return self._finalize_decision(third_party_value, decision)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if not self._should_try_fallback_variant(exc, payload):
                        break
            if attempt >= self.max_retries:
                break
            time.sleep(self.retry_delay_seconds * attempt)

        return MerchantDecision.empty(reason=f"verification_failed: {last_error}")

    def _finalize_decision(self, third_party_value: str, decision: MerchantDecision) -> MerchantDecision:
        if not decision.is_real_merchant:
            return decision
        literal_keyword = align_keyword_to_third_party(
            raw_value=third_party_value,
            keyword=decision.keyword,
            standardized=decision.standardized,
        )
        if not literal_keyword:
            return MerchantDecision.empty(reason="keyword_not_in_third_party")
        return MerchantDecision(
            is_real_merchant=True,
            standardized=decision.standardized,
            keyword=literal_keyword,
            link=decision.link,
            reason=decision.reason,
        )

    def _request_variants(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        variants = [body]
        if "response_format" in body:
            fallback = dict(body)
            fallback.pop("response_format", None)
            variants.append(fallback)
        return variants

    def _should_try_fallback_variant(self, exc: Exception, payload: dict[str, Any]) -> bool:
        if "response_format" not in payload:
            return False
        message = str(exc).casefold()
        return "response_format" in message or "unsupported" in message or "invalid" in message

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"records": self.records}
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def get(self, canonical_value: str) -> MerchantDecision | None:
        payload = self.records.get(canonical_value)
        if not payload:
            return None
        decision = MerchantDecision.from_model_payload(payload)
        if not decision.should_cache():
            return None
        return decision

    def set(self, canonical_value: str, decision: MerchantDecision) -> None:
        self.records[canonical_value] = asdict(decision)


def load_rows(path: Path, row_limit: int | None = None) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file has no header: {path}")
        rows: list[dict[str, str]] = []
        for idx, row in enumerate(reader):
            if row_limit is not None and idx >= row_limit:
                break
            rows.append({key: value or "" for key, value in row.items()})
        return rows, list(reader.fieldnames)


def write_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def apply_decision_to_row(row: dict[str, str], decision: MerchantDecision) -> None:
    row["standardized"] = decision.standardized
    row["keyword"] = decision.keyword
    row["link"] = decision.link


def exact_fill_blank(row: dict[str, str]) -> None:
    for field in EMPTY_FIELDS:
        row[field] = ""


def should_apply_keyword(keyword: str) -> bool:
    return bool(normalize_search_text(keyword))


def process_file(args: argparse.Namespace) -> None:
    started_at = time.time()
    rows, input_fieldnames = load_rows(args.input, args.row_limit)
    if "third_party" not in input_fieldnames:
        raise ValueError("Input CSV must contain a third_party column.")

    print(
        f"Start. input={args.input} rows={len(rows)} output={args.output} cache={args.cache}",
        flush=True,
    )

    fieldnames = list(input_fieldnames)
    for field in EMPTY_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)

    for row in rows:
        for field in EMPTY_FIELDS:
            row.setdefault(field, "")

    searchable_rows: list[str] = []
    tokenized_rows: list[list[str]] = []
    exact_groups: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        raw_value = normalize_space(row.get("third_party", ""))
        normalized_search = normalize_search_text(raw_value)
        searchable_rows.append(normalized_search)
        tokenized_rows.append(normalized_search.split())
        canonical = normalize_space(raw_value).casefold()
        exact_groups.setdefault(canonical, []).append(idx)

    prefix_counts: dict[str, int] = {}
    for tokens in tokenized_rows:
        upper = min(4, len(tokens))
        for size in range(2, upper + 1):
            prefix_text = " ".join(tokens[:size])
            prefix_counts[prefix_text] = prefix_counts.get(prefix_text, 0) + 1

    best_prefix_by_canonical: dict[str, str] = {}
    best_prefix_support_by_canonical: dict[str, int] = {}
    for canonical_value, indices in exact_groups.items():
        best_prefix, best_support = best_repeated_prefix(tokenized_rows[indices[0]], prefix_counts)
        best_prefix_by_canonical[canonical_value] = best_prefix
        best_prefix_support_by_canonical[canonical_value] = best_support

    processed = [False] * len(rows)
    cache_store = CacheStore(args.cache)
    cache_store.load()
    print(f"Cache loaded. entries={len(cache_store.records)}", flush=True)

    stats = {
        "rows_total": len(rows),
        "rows_processed": 0,
        "api_calls": 0,
        "keyword_batches": 0,
        "cached_hits": 0,
    }

    def mark_processed(index: int, decision: MerchantDecision | None = None) -> None:
        if processed[index]:
            return
        processed[index] = True
        stats["rows_processed"] += 1
        if decision and decision.is_real_merchant:
            apply_decision_to_row(rows[index], decision)
        else:
            exact_fill_blank(rows[index])

    def apply_decision(canonical_value: str, decision: MerchantDecision) -> int:
        matched = 0
        exact_indices = exact_groups.get(canonical_value, [])
        if decision.is_real_merchant and should_apply_keyword(decision.keyword):
            keyword_search = normalize_search_text(decision.keyword)
            for idx, search_text in enumerate(searchable_rows):
                if processed[idx]:
                    continue
                if keyword_search and keyword_search in search_text:
                    processed[idx] = True
                    stats["rows_processed"] += 1
                    apply_decision_to_row(rows[idx], decision)
                    matched += 1
            for idx in exact_indices:
                if not processed[idx]:
                    processed[idx] = True
                    stats["rows_processed"] += 1
                    apply_decision_to_row(rows[idx], decision)
                    matched += 1
            if matched:
                stats["keyword_batches"] += 1
        else:
            for idx in exact_indices:
                mark_processed(idx, None)
                matched += 1
        return matched

    for canonical_value in list(cache_store.records):
        decision = cache_store.get(canonical_value)
        if decision is None:
            continue
        matched = apply_decision(canonical_value, decision)
        if matched:
            stats["cached_hits"] += 1
    if stats["cached_hits"]:
        print(
            f"Cache applied. cached_hits={stats['cached_hits']} rows_processed={stats['rows_processed']}/{stats['rows_total']}",
            flush=True,
        )

    candidates: list[tuple[tuple[int, int, int, int, int], str, str]] = []
    for canonical_value, indices in exact_groups.items():
        if not canonical_value:
            for idx in indices:
                mark_processed(idx, None)
            continue
        raw_value = normalize_space(rows[indices[0]].get("third_party", ""))
        if should_skip_without_api(raw_value):
            for idx in indices:
                mark_processed(idx, None)
            continue
        repeated_prefix_support = best_prefix_support_by_canonical[canonical_value]
        candidates.append(
            (
                build_candidate_score(raw_value, len(indices), repeated_prefix_support),
                canonical_value,
                raw_value,
            )
        )
    candidates.sort(reverse=True)
    print(
        f"Prepared candidates. candidates={len(candidates)} rows_processed={stats['rows_processed']}/{stats['rows_total']}",
        flush=True,
    )

    client = DeepSeekClient(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        retry_delay_seconds=args.retry_delay_seconds,
        extra_body=args.extra_body_json,
    )

    for candidate_index, (_, canonical_value, raw_value) in enumerate(candidates, start=1):
        if all(processed[idx] for idx in exact_groups[canonical_value]):
            continue
        if args.max_api_calls is not None and stats["api_calls"] >= args.max_api_calls:
            break

        next_call = stats["api_calls"] + 1
        print(
            f"[call {next_call}] checking {shorten_text(raw_value)}",
            flush=True,
        )
        decision = client.verify_merchant(raw_value, prefix_hint=best_prefix_by_canonical.get(canonical_value, ""))
        stats["api_calls"] += 1
        if decision.should_cache():
            cache_store.set(canonical_value, decision)
        matched = apply_decision(canonical_value, decision)

        if decision.is_real_merchant:
            print(
                f"[call {stats['api_calls']}] merchant={shorten_text(decision.standardized, 40)} "
                f"matched={matched} keyword={shorten_text(decision.keyword, 40)}",
                flush=True,
            )
        else:
            print(
                f"[call {stats['api_calls']}] no-merchant matched={matched}",
                flush=True,
            )

        if stats["api_calls"] % args.cache_save_every == 0:
            cache_store.save()

        if args.progress_every and stats["api_calls"] % args.progress_every == 0:
            elapsed = time.time() - started_at
            calls_per_minute = (stats["api_calls"] / elapsed * 60) if elapsed > 0 else 0.0
            estimated_remaining = len(candidates) - candidate_index
            eta_seconds = (estimated_remaining / stats["api_calls"] * elapsed) if stats["api_calls"] else 0.0
            print(
                f"api_calls={stats['api_calls']} rows_processed={stats['rows_processed']}/{stats['rows_total']} "
                f"keyword_batches={stats['keyword_batches']} cache_size={len(cache_store.records)} "
                f"elapsed={format_seconds(elapsed)} rate={calls_per_minute:.1f}/min eta~={format_seconds(eta_seconds)}",
                flush=True,
            )

    for idx, done in enumerate(processed):
        if not done:
            mark_processed(idx, None)

    cache_store.save()
    write_rows(args.output, rows, fieldnames)
    elapsed = time.time() - started_at
    print(
        f"Finished. rows={stats['rows_total']} processed={stats['rows_processed']} "
        f"api_calls={stats['api_calls']} keyword_batches={stats['keyword_batches']} "
        f"cached_hits={stats['cached_hits']} elapsed={format_seconds(elapsed)} output={args.output}",
        flush=True,
    )


def parse_extra_body(raw_json: str | None) -> dict[str, Any]:
    if not raw_json:
        return {}
    payload = json.loads(raw_json)
    if not isinstance(payload, dict):
        raise ValueError("--extra-body-json must be a JSON object.")
    return payload


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify merchants from third_party_dedup.csv with DeepSeek and batch-fill matched rows."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    parser.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=float, default=2.0)
    parser.add_argument("--cache-save-every", type=int, default=1)
    parser.add_argument("--progress-every", type=int, default=20)
    parser.add_argument("--max-api-calls", type=int, default=None)
    parser.add_argument("--row-limit", type=int, default=None)
    parser.add_argument(
        "--extra-body-json",
        type=parse_extra_body,
        default=parse_extra_body(os.environ.get("DEEPSEEK_EXTRA_BODY_JSON")),
        help="Provider-specific JSON object merged into the DeepSeek request body.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if not args.api_key:
        parser.error("Missing DeepSeek API key. Set DEEPSEEK_API_KEY or pass --api-key.")
    if args.max_retries < 1:
        parser.error("--max-retries must be at least 1.")
    if args.cache_save_every < 1:
        parser.error("--cache-save-every must be at least 1.")
    process_file(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
