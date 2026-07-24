from __future__ import annotations

import argparse
import atexit
import csv
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from utils import (
    china_timestamp_now,
    clean_output_value,
    extract_json_object,
    normalize_space,
    normalize_search_text,
    open_csv_dict_reader,
    post_json,
    safe_url,
    split_kb_keywords,
    KEYWORD_SEPARATOR,
)


DEFAULT_INPUT = Path("sample.csv")
DEFAULT_OUTPUT = Path("output/sample_verified.csv")
DEFAULT_CACHE = Path("cache/sample_verification_cache.json")
DEFAULT_MERCHANT_KB = Path("merchant_kb.csv")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEFAULT_THINKING_TYPE = os.environ.get("DEEPSEEK_THINKING_TYPE", "none")
DEFAULT_REASONING_EFFORT = os.environ.get("DEEPSEEK_REASONING_EFFORT", "none")
EMPTY_FIELDS = ("standardized", "keyword", "link")
TRACE_FIELDS = ("match_source", "matched_from_text")
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


class MatchSource(str, Enum):
    KNOWLEDGE_BASE_DIRECT = "knowledge_base_direct"
    KNOWLEDGE_BASE_KEYWORD = "knowledge_base_keyword"
    CACHE_DIRECT = "cache_direct"
    CACHE_KEYWORD = "cache_keyword"
    AI_DIRECT = "ai_direct"
    AI_KEYWORD = "ai_keyword"
    UNRESOLVED = "unresolved"


def alpha_count(value: str) -> int:
    return sum(1 for char in value if char.isalpha())


def digit_count(value: str) -> int:
    return sum(1 for char in value if char.isdigit())




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


def align_keyword_to_text(raw_value: str, keyword: str, standardized: str) -> str:
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


@dataclass(frozen=True)
class MerchantPromptConfig:
    system_message: str = "Return strict JSON and nothing else."

    def build_user_prompt(self, text_value: str, prefix_hint: str = "") -> str:
        hint_line = ""
        if prefix_hint:
            hint_line = (
                f"Frequent leading phrase from similar text strings: {json.dumps(prefix_hint, ensure_ascii=False)}.\n"
                "Use this only as a hint if it is clearly part of the same merchant name.\n"
            )
        return (
            "You are verifying whether a bank-transaction counterparty string refers to a real merchant.\n"
            "Use only the provided text field. Do not rely on any other fields.\n"
            "These strings can contain noise such as bank-channel labels, person names, payroll text, refund codes, dates, or reference IDs.\n"
            "If a plausible merchant entity can be isolated from the text field alone, verify that merchant and return its standardized name.\n"
            "If your API/provider supports web search, use it to confirm the merchant exists online.\n"
            "If you cannot confidently confirm it is a real merchant, respond with is_real_merchant=false and leave fields blank.\n"
            "Return JSON only with keys: is_real_merchant, standardized, keyword, link, reason.\n"
            "Rules:\n"
            "- standardized: official or commonly accepted merchant name, without bank noise.\n"
            "- keyword: copy the shortest distinctive merchant phrase directly from the provided text field. Do not rewrite, normalize, translate, or invent it.\n"
            "- keyword must come from the text field itself, but standardized can be normalized.\n"
            "- keyword should keep only the merchant-identifying span needed for matching similar rows.\n"
            "- Exclude trailing or leading location text, suburb/state/country abbreviations, store numbers, terminal IDs, card numbers, dates, times, and reference IDs unless they are clearly part of the official merchant name.\n"
            "- link: best verification URL, prefer official website, otherwise Google Maps or another reliable directory.\n"
            "- If the value is empty, personal, invalid, generic, or not a merchant, set false and keep all fields blank.\n"
            "- Example: 'One Click Life OCNxtDy745U' can map to standardized='One Click Life' if that merchant is real.\n"
            "- Example: 'ACCESSABILITY WA LISA PITCHE' can map to standardized='Accessability WA' if that merchant is real.\n"
            "- Example: 'CASH CONVERTERS CCS Perth WA AUS C' should use keyword='CASH CONVERTERS' and standardized='Cash Converters'.\n"
            "- Example: 'EFTPOS DEBIT 20NOV20:43 Lmf Games & Amusementsnorthmead Nswau' should use keyword='Lmf Games & Amusements' and standardized='LMF Games & Amusements'.\n"
            f"{hint_line}"
            f"text: {json.dumps(text_value, ensure_ascii=False)}"
        )

    def build_batch_user_prompt(self, items: list[dict[str, str]]) -> str:
        payload = [
            {
                "id": item["id"],
                "text": item.get("text", ""),
                "prefix_hint": item.get("prefix_hint", ""),
            }
            for item in items
        ]
        return (
            "You are verifying whether bank-transaction counterparty strings refer to real merchants.\n"
            "Use only the provided text field. Do not rely on any other fields.\n"
            "These strings can contain noise such as bank-channel labels, person names, payroll text, refund codes, dates, or reference IDs.\n"
            "If a plausible merchant entity can be isolated from the text field alone, verify that merchant and return its standardized name.\n"
            "If your API/provider supports web search, use it to confirm the merchant exists online.\n"
            "If you cannot confidently confirm it is a real merchant, respond with is_real_merchant=false and leave fields blank.\n"
            "Return JSON only as an object with key results. results must be an array with one result per input id.\n"
            "Each result must have keys: id, is_real_merchant, standardized, keyword, link, reason.\n"
            "Rules:\n"
            "- standardized: official or commonly accepted merchant name, without bank noise.\n"
            "- keyword: copy the shortest distinctive merchant phrase directly from the provided text field. Do not rewrite, normalize, translate, or invent it.\n"
            "- keyword must come from the text field itself, but standardized can be normalized.\n"
            "- keyword should keep only the merchant-identifying span needed for matching similar rows.\n"
            "- Exclude trailing or leading location text, suburb/state/country abbreviations, store numbers, terminal IDs, card numbers, dates, times, and reference IDs unless they are clearly part of the official merchant name.\n"
            "- link: best verification URL, prefer official website, otherwise Google Maps or another reliable directory.\n"
            "- If the value is empty, personal, invalid, generic, or not a merchant, set false and keep all fields blank.\n"
            "- If a prefix_hint is provided, use it only as a hint when it is clearly part of the same merchant name.\n"
            f"items: {json.dumps(payload, ensure_ascii=False)}"
        )


class MerchantResponseValidator:
    def parse(self, text_value: str, message: str) -> MerchantDecision:
        payload_json = extract_json_object(message)
        decision = MerchantDecision.from_model_payload(payload_json)
        return self.finalize(text_value, decision)

    def parse_batch(self, items: list[dict[str, str]], message: str) -> dict[str, MerchantDecision]:
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

        decisions: dict[str, MerchantDecision] = {}
        for item in items:
            text_value = item.get("text", "")
            payload = result_by_id.get(item["id"])
            if payload is None:
                decisions[item["id"]] = MerchantDecision.empty(reason="missing_batch_result")
                continue
            decision = MerchantDecision.from_model_payload(payload)
            decisions[item["id"]] = self.finalize(text_value, decision)
        return decisions

    def finalize(self, text_value: str, decision: MerchantDecision) -> MerchantDecision:
        if not decision.is_real_merchant:
            return decision
        literal_keyword = align_keyword_to_text(
            raw_value=text_value,
            keyword=decision.keyword,
            standardized=decision.standardized,
        )
        if not literal_keyword:
            return MerchantDecision.empty(reason="keyword_not_in_text")
        return MerchantDecision(
            is_real_merchant=True,
            standardized=decision.standardized,
            keyword=literal_keyword,
            link=decision.link,
            reason=decision.reason,
        )


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int,
        max_retries: int,
        retry_delay_seconds: float,
        thinking_type: str = "",
        reasoning_effort: str = "",
        prompt_config: MerchantPromptConfig | None = None,
        response_validator: MerchantResponseValidator | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.thinking_type = thinking_type
        self.reasoning_effort = reasoning_effort
        self.prompt_config = prompt_config or MerchantPromptConfig()
        self.response_validator = response_validator or MerchantResponseValidator()

    def verify_merchant(
        self,
        text_value: str,
        prefix_hint: str = "",
    ) -> MerchantDecision:
        prompt = self.prompt_config.build_user_prompt(text_value, prefix_hint)
        message = self._chat_completion(prompt)
        return self.response_validator.parse(text_value, message)

    def verify_merchant_batch(self, items: list[dict[str, str]]) -> dict[str, MerchantDecision]:
        if not items:
            return {}
        prompt = self.prompt_config.build_batch_user_prompt(items)
        message = self._chat_completion(prompt)
        return self.response_validator.parse_batch(items, message)

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


@dataclass
class KnowledgeBaseEntry:
    merchant_name: str
    keyword: str
    link: str
    normalized_keyword: str
    first_token: str


def validate_kb_fieldnames(path: Path, reader: csv.DictReader) -> None:
    fieldnames = list(reader.fieldnames or [])
    if fieldnames != KB_FIELDNAMES:
        raise ValueError(
            f"Merchant KB schema mismatch in {path}. "
            f"Expected columns {KB_FIELDNAMES}, got {fieldnames}."
        )


KB_FIELDNAMES = [
    "merchant_name",
    "keywords",
    "link",
    "category",
    "keyword_updated_at",
    "category_updated_at",
]


@dataclass
class MerchantKBCandidate:
    merchant_name: str
    keyword: str
    link: str


def normalize_kb_row(row: dict[str, str]) -> dict[str, str]:
    return {
        "merchant_name": normalize_space(row.get("merchant_name", "")),
        "keywords": KEYWORD_SEPARATOR.join(split_kb_keywords(row.get("keywords", ""))),
        "link": safe_url(row.get("link", "")),
        "category": clean_output_value(row.get("category", "")),
        "keyword_updated_at": normalize_space(row.get("keyword_updated_at", "")),
        "category_updated_at": normalize_space(row.get("category_updated_at", "")),
    }


class MerchantKBUpdater:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append_ai_results(self, candidates: list[MerchantKBCandidate]) -> None:
        if not candidates:
            return

        rows: list[dict[str, str]] = []
        row_index_by_normalized_name: dict[str, int] = {}
        keyword_norms_by_normalized_name: dict[str, set[str]] = {}

        if self.path.exists():
            with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                validate_kb_fieldnames(self.path, reader)
                for row in reader:
                    normalized_row = normalize_kb_row({key: value or "" for key, value in row.items()})
                    normalized_name = normalize_search_text(normalized_row["merchant_name"])
                    if not normalized_name:
                        continue
                    keyword_norms = {
                        normalize_search_text(keyword)
                        for keyword in split_kb_keywords(normalized_row["keywords"])
                    }
                    if normalized_name in row_index_by_normalized_name:
                        existing_row = rows[row_index_by_normalized_name[normalized_name]]
                        merged_keywords = split_kb_keywords(
                            KEYWORD_SEPARATOR.join([existing_row["keywords"], normalized_row["keywords"]])
                        )
                        existing_row["keywords"] = KEYWORD_SEPARATOR.join(merged_keywords)
                        if not existing_row["link"] and normalized_row["link"]:
                            existing_row["link"] = normalized_row["link"]
                        if not existing_row["category"] and normalized_row["category"]:
                            existing_row["category"] = normalized_row["category"]
                        if not existing_row["keyword_updated_at"] and normalized_row["keyword_updated_at"]:
                            existing_row["keyword_updated_at"] = normalized_row["keyword_updated_at"]
                        if not existing_row["category_updated_at"] and normalized_row["category_updated_at"]:
                            existing_row["category_updated_at"] = normalized_row["category_updated_at"]
                        keyword_norms_by_normalized_name[normalized_name].update(keyword_norms)
                        continue
                    row_index_by_normalized_name[normalized_name] = len(rows)
                    keyword_norms_by_normalized_name[normalized_name] = set(keyword_norms)
                    rows.append(normalized_row)

        timestamp = china_timestamp_now()
        changed = False
        for candidate in candidates:
            merchant_name = normalize_space(candidate.merchant_name)
            keyword = normalize_space(candidate.keyword)
            link = safe_url(candidate.link)
            normalized_name = normalize_search_text(merchant_name)
            if not merchant_name or not keyword or not normalized_name:
                continue

            keywords_to_add: list[tuple[str, str]] = []
            seen_keywords_for_candidate: set[str] = set()
            for candidate_keyword in (keyword, merchant_name):
                normalized_keyword = normalize_search_text(candidate_keyword)
                if not normalized_keyword or normalized_keyword in seen_keywords_for_candidate:
                    continue
                seen_keywords_for_candidate.add(normalized_keyword)
                keywords_to_add.append((normalize_space(candidate_keyword), normalized_keyword))

            if not keywords_to_add:
                continue

            if normalized_name not in row_index_by_normalized_name:
                row_index_by_normalized_name[normalized_name] = len(rows)
                keyword_norms_by_normalized_name[normalized_name] = set()
                rows.append(
                    {
                        "merchant_name": merchant_name,
                        "keywords": "",
                        "link": link,
                        "category": "",
                        "keyword_updated_at": "",
                        "category_updated_at": "",
                    }
                )
            row = rows[row_index_by_normalized_name[normalized_name]]
            existing_keyword_norms = keyword_norms_by_normalized_name[normalized_name]
            existing_keywords = split_kb_keywords(row["keywords"])
            row_changed = False
            for keyword_value, normalized_keyword in keywords_to_add:
                if normalized_keyword in existing_keyword_norms:
                    continue
                existing_keyword_norms.add(normalized_keyword)
                existing_keywords.append(keyword_value)
                row_changed = True
            if link and not row["link"]:
                row["link"] = link
                row_changed = True
            if row_changed:
                row["keywords"] = KEYWORD_SEPARATOR.join(existing_keywords)
                row["keyword_updated_at"] = timestamp
                changed = True

        if not changed:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        target_path = self.path.resolve()
        temp_path = target_path.with_name(f"{target_path.name}.{os.getpid()}.tmp")
        with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=KB_FIELDNAMES)
            writer.writeheader()
            writer.writerows(normalize_kb_row(row) for row in rows)
        temp_path.replace(target_path)


class KeywordRowIndex:
    def __init__(self, searchable_rows: list[str], tokenized_rows: list[list[str]]) -> None:
        self.searchable_rows = searchable_rows
        self.token_to_indices: dict[str, set[int]] = {}
        for idx, tokens in enumerate(tokenized_rows):
            for token in set(tokens):
                self.token_to_indices.setdefault(token, set()).add(idx)

    def find_containing(
        self,
        keyword_search: str,
        processed: list[bool],
        excluded_indices: set[int],
    ) -> list[int]:
        tokens = keyword_search.split()
        if not keyword_search or not tokens:
            return []

        candidate_sets = [self.token_to_indices.get(token, set()) for token in set(tokens)]
        if not candidate_sets or any(not candidate_set for candidate_set in candidate_sets):
            return []

        candidate_indices = set(min(candidate_sets, key=len))
        for candidate_set in candidate_sets:
            if candidate_set is candidate_indices:
                continue
            candidate_indices.intersection_update(candidate_set)
            if not candidate_indices:
                return []

        return [
            idx
            for idx in sorted(candidate_indices)
            if not processed[idx]
            and idx not in excluded_indices
            and keyword_search in self.searchable_rows[idx]
        ]


def load_merchant_kb(path: Path) -> list[KnowledgeBaseEntry]:
    if not path.exists():
        return []
    reader = open_csv_dict_reader(path)
    validate_kb_fieldnames(path, reader)
    entries: list[KnowledgeBaseEntry] = []
    seen: set[tuple[str, str]] = set()
    for row in reader:
        normalized_row = normalize_kb_row({key: value or "" for key, value in row.items()})
        merchant_name = normalized_row["merchant_name"]
        link = normalized_row["link"]
        keywords = split_kb_keywords(normalized_row["keywords"])
        if not merchant_name or not keywords:
            continue
        for keyword in keywords:
            normalized_keyword = normalize_search_text(keyword)
            if not normalized_keyword:
                continue
            tokens = normalized_keyword.split()
            if not tokens:
                continue
            dedup_key = (merchant_name.casefold(), normalized_keyword)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            entries.append(
                KnowledgeBaseEntry(
                    merchant_name=merchant_name,
                    keyword=keyword,
                    link=link,
                    normalized_keyword=normalized_keyword,
                    first_token=tokens[0],
                )
            )
    entries.sort(
        key=lambda entry: (
            len(entry.normalized_keyword.split()),
            len(entry.normalized_keyword),
            len(entry.merchant_name),
        ),
        reverse=True,
    )
    return entries


def load_rows(path: Path, row_limit: int | None = None) -> tuple[list[dict[str, str]], list[str]]:
    reader = open_csv_dict_reader(path)
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


def default_checkpoint_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.checkpoint{output_path.suffix}")


def apply_decision_to_row(row: dict[str, str], decision: MerchantDecision) -> None:
    row["standardized"] = decision.standardized
    row["keyword"] = decision.keyword
    row["link"] = decision.link


def set_trace_fields(row: dict[str, str], match_source: MatchSource, matched_from_text: str = "") -> None:
    row["match_source"] = match_source.value
    row["matched_from_text"] = normalize_space(matched_from_text)


def exact_fill_blank(row: dict[str, str]) -> None:
    for field in EMPTY_FIELDS:
        row[field] = ""


def should_apply_keyword(keyword: str) -> bool:
    return bool(normalize_search_text(keyword))


def process_file(args: argparse.Namespace) -> None:
    rows, input_fieldnames = load_rows(args.input, args.row_limit)
    if "text" not in input_fieldnames:
        raise ValueError("Input CSV must contain a text column.")

    checkpoint_path = args.checkpoint_output or default_checkpoint_path(args.output)

    fieldnames = list(input_fieldnames)
    for field in EMPTY_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)
    for field in TRACE_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)

    for row in rows:
        for field in EMPTY_FIELDS:
            row.setdefault(field, "")
        for field in TRACE_FIELDS:
            row.setdefault(field, "")

    searchable_rows: list[str] = []
    tokenized_rows: list[list[str]] = []
    exact_groups: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        raw_value = normalize_space(row.get("text", ""))
        normalized_search = normalize_search_text(raw_value)
        searchable_rows.append(normalized_search)
        tokenized_rows.append(normalized_search.split())
        canonical = normalize_space(raw_value).casefold()
        exact_groups.setdefault(canonical, []).append(idx)

    token_sets = [set(tokens) for tokens in tokenized_rows]
    row_search_index = KeywordRowIndex(searchable_rows, tokenized_rows)

    kb_entries = load_merchant_kb(args.merchant_kb) if args.merchant_kb else []
    kb_entries_by_first_token: dict[str, list[KnowledgeBaseEntry]] = {}
    for entry in kb_entries:
        kb_entries_by_first_token.setdefault(entry.first_token, []).append(entry)

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

    stats = {
        "rows_total": len(rows),
        "rows_processed": 0,
        "api_calls": 0,
    }
    identified_merchant_names: set[str] = set()
    pending_ai_kb_candidates: list[MerchantKBCandidate] = []
    checkpoint_written_at_call = -1
    cache_dirty = False

    def rows_waiting() -> int:
        return stats["rows_total"] - stats["rows_processed"]

    def print_stage(label: str) -> None:
        pct = stats["rows_processed"] / stats["rows_total"] * 100 if stats["rows_total"] else 0
        print(
            f"[{label}] done={stats['rows_processed']}/{stats['rows_total']} "
            f"({pct:.1f}%) waiting={rows_waiting()} merchants={len(identified_merchant_names)}",
            flush=True,
        )

    def print_batch_summary(batch_num: int, batch_resolved: int, batch_unresolved: int) -> None:
        pct = stats["rows_processed"] / stats["rows_total"] * 100 if stats["rows_total"] else 0
        print(
            f"Batch {batch_num} api_calls={stats['api_calls']} "
            f"resolved={batch_resolved} unresolved={batch_unresolved} "
            f"done={stats['rows_processed']}/{stats['rows_total']} ({pct:.1f}%) "
            f"merchants={len(identified_merchant_names)}",
            flush=True,
        )

    print_stage("start")

    def mark_processed(
        index: int,
        decision: MerchantDecision | None = None,
        match_source: MatchSource = MatchSource.UNRESOLVED,
        matched_from_text: str = "",
    ) -> None:
        if processed[index]:
            return
        processed[index] = True
        stats["rows_processed"] += 1
        if decision and decision.is_real_merchant:
            apply_decision_to_row(rows[index], decision)
            set_trace_fields(rows[index], match_source, matched_from_text)
        else:
            exact_fill_blank(rows[index])
            set_trace_fields(rows[index], match_source, matched_from_text)

    def apply_decision(
        canonical_value: str,
        decision: MerchantDecision,
        direct_match_source: MatchSource,
        keyword_match_source: MatchSource,
        matched_from_text: str,
    ) -> tuple[int, int, int]:
        matched = 0
        keyword_matched = 0
        direct_matched = 0
        exact_indices = exact_groups.get(canonical_value, [])
        exact_index_set = set(exact_indices)
        if decision.is_real_merchant and should_apply_keyword(decision.keyword):
            identified_merchant_names.add(normalize_search_text(decision.standardized))
            keyword_search = normalize_search_text(decision.keyword)
            for idx in row_search_index.find_containing(keyword_search, processed, exact_index_set):
                processed[idx] = True
                stats["rows_processed"] += 1
                apply_decision_to_row(rows[idx], decision)
                set_trace_fields(rows[idx], keyword_match_source, matched_from_text)
                matched += 1
                keyword_matched += 1
            for idx in exact_indices:
                if not processed[idx]:
                    processed[idx] = True
                    stats["rows_processed"] += 1
                    apply_decision_to_row(rows[idx], decision)
                    set_trace_fields(rows[idx], direct_match_source, matched_from_text)
                    matched += 1
                    direct_matched += 1
        else:
            for idx in exact_indices:
                mark_processed(idx, None, match_source=MatchSource.UNRESOLVED)
                matched += 1
                direct_matched += 1
        return matched, keyword_matched, direct_matched

    def write_checkpoint(force: bool = False) -> None:
        nonlocal checkpoint_written_at_call
        if args.checkpoint_every <= 0 and not force:
            return
        write_rows(checkpoint_path, rows, fieldnames)
        checkpoint_written_at_call = stats["api_calls"]

    def save_cache_if_dirty() -> None:
        nonlocal cache_dirty
        if not cache_dirty:
            return
        cache_store.save()
        cache_dirty = False

    merchant_kb_update_enabled = not args.skip_merchant_kb_update

    def flush_pending_merchant_kb() -> None:
        if not merchant_kb_update_enabled or not pending_ai_kb_candidates:
            return
        MerchantKBUpdater(args.merchant_kb).append_ai_results(pending_ai_kb_candidates)
        pending_ai_kb_candidates.clear()

    def flush_checkpoint_on_exit() -> None:
        if stats["api_calls"] <= 0:
            return
        flush_pending_merchant_kb()
        save_cache_if_dirty()
        if stats["api_calls"] == checkpoint_written_at_call:
            return
        write_checkpoint(force=True)

    atexit.register(flush_checkpoint_on_exit)

    if kb_entries:
        for idx, row in enumerate(rows):
            if processed[idx]:
                continue
            candidate_entries: list[KnowledgeBaseEntry] = []
            seen_entry_keys: set[tuple[str, str]] = set()
            for token in token_sets[idx]:
                for entry in kb_entries_by_first_token.get(token, []):
                    entry_key = (entry.merchant_name.casefold(), entry.normalized_keyword)
                    if entry_key in seen_entry_keys:
                        continue
                    seen_entry_keys.add(entry_key)
                    candidate_entries.append(entry)
            if not candidate_entries:
                continue
            search_text = searchable_rows[idx]
            best_entry: KnowledgeBaseEntry | None = None
            for entry in candidate_entries:
                if entry.normalized_keyword in search_text:
                    if best_entry is None or (
                        len(entry.normalized_keyword.split()),
                        len(entry.normalized_keyword),
                        len(entry.merchant_name),
                    ) > (
                        len(best_entry.normalized_keyword.split()),
                        len(best_entry.normalized_keyword),
                        len(best_entry.merchant_name),
                    ):
                        best_entry = entry
            if best_entry is None:
                continue
            literal_keyword = extract_literal_substring(row.get("text", ""), best_entry.keyword)
            if not literal_keyword:
                continue
            decision = MerchantDecision(
                is_real_merchant=True,
                standardized=best_entry.merchant_name,
                keyword=literal_keyword,
                link=best_entry.link,
                reason="matched_merchant_kb",
            )
            canonical_value = normalize_space(row.get("text", "")).casefold()
            apply_decision(
                canonical_value,
                decision,
                direct_match_source=MatchSource.KNOWLEDGE_BASE_DIRECT,
                keyword_match_source=MatchSource.KNOWLEDGE_BASE_KEYWORD,
                matched_from_text=row.get("text", ""),
            )
        print_stage("knowledge_base")

    cache_hit_found = False
    for canonical_value in list(cache_store.records):
        decision = cache_store.get(canonical_value)
        if decision is None:
            continue
        if canonical_value not in exact_groups:
            continue
        source_text = rows[exact_groups[canonical_value][0]].get("text", "")
        matched, _, _ = apply_decision(
            canonical_value,
            decision,
            direct_match_source=MatchSource.CACHE_DIRECT,
            keyword_match_source=MatchSource.CACHE_KEYWORD,
            matched_from_text=source_text,
        )
        if matched:
            cache_hit_found = True
    if cache_hit_found:
        print_stage("cache")

    candidates: list[tuple[tuple[int, int, int, int, int], str, str, int]] = []
    for canonical_value, indices in exact_groups.items():
        if not canonical_value:
            for idx in indices:
                mark_processed(idx, None, match_source=MatchSource.UNRESOLVED)
            continue
        raw_value = normalize_space(rows[indices[0]].get("text", ""))
        if should_skip_without_api(raw_value):
            for idx in indices:
                mark_processed(idx, None, match_source=MatchSource.UNRESOLVED)
            continue
        repeated_prefix_support = best_prefix_support_by_canonical[canonical_value]
        candidates.append(
            (
                build_candidate_score(raw_value, len(indices), repeated_prefix_support),
                canonical_value,
                raw_value,
                len(indices),
            )
        )
    candidates.sort(reverse=True)
    print_stage("candidates")

    client = DeepSeekClient(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        retry_delay_seconds=args.retry_delay_seconds,
        thinking_type=args.thinking_type,
        reasoning_effort=args.reasoning_effort,
    )

    batch_size = args.batch_size if args.batch_size > 0 else 1
    candidate_index = 0
    batch_num = 0
    while candidate_index < len(candidates):
        batch_items: list[dict[str, str]] = []
        batch_candidates: list[tuple[str, str, int]] = []
        for i in range(candidate_index, min(candidate_index + batch_size, len(candidates))):
            _, canonical_value, raw_value, frequency = candidates[i]
            if all(processed[idx] for idx in exact_groups[canonical_value]):
                continue
            if args.max_api_calls is not None and stats["api_calls"] >= args.max_api_calls:
                break
            batch_items.append({
                "id": canonical_value,
                "text": raw_value,
                "prefix_hint": best_prefix_by_canonical.get(canonical_value, ""),
            })
            batch_candidates.append((canonical_value, raw_value, frequency))

        if not batch_items:
            candidate_index += batch_size
            continue

        batch_num += 1
        stats["api_calls"] += 1
        decisions = client.verify_merchant_batch(batch_items)

        batch_resolved = 0
        batch_unresolved = 0
        for canonical_value, raw_value, frequency in batch_candidates:
            decision = decisions.get(canonical_value)
            if decision is None:
                decision = MerchantDecision.empty(reason="missing_batch_result")

            if decision.should_cache():
                cache_store.set(canonical_value, decision)
                cache_dirty = True
            matched, keyword_matched, direct_matched = apply_decision(
                canonical_value,
                decision,
                direct_match_source=MatchSource.AI_DIRECT,
                keyword_match_source=MatchSource.AI_KEYWORD,
                matched_from_text=raw_value,
            )
            if decision.is_real_merchant:
                batch_resolved += 1
                if matched:
                    pending_ai_kb_candidates.append(
                        MerchantKBCandidate(
                            merchant_name=decision.standardized,
                            keyword=decision.keyword,
                            link=decision.link,
                        )
                    )
            else:
                batch_unresolved += 1
        print_batch_summary(batch_num, batch_resolved, batch_unresolved)

        if args.cache_save_every and stats["api_calls"] % args.cache_save_every == 0:
            save_cache_if_dirty()

        if args.checkpoint_every and stats["api_calls"] % args.checkpoint_every == 0:
            write_checkpoint()

        if args.merchant_kb_save_every and stats["api_calls"] % args.merchant_kb_save_every == 0:
            flush_pending_merchant_kb()

        candidate_index += batch_size

    for idx, done in enumerate(processed):
        if not done:
            mark_processed(idx, None, match_source=MatchSource.UNRESOLVED)

    save_cache_if_dirty()
    flush_pending_merchant_kb()
    write_rows(args.output, rows, fieldnames)
    atexit.unregister(flush_checkpoint_on_exit)
    print_stage("finished")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify merchants from third_party_dedup.csv with DeepSeek and batch-fill matched rows from the text column."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--merchant-kb", type=Path, default=DEFAULT_MERCHANT_KB)
    parser.add_argument(
        "--skip-merchant-kb-update",
        action="store_true",
        help="Disable merchant_kb.csv updates for this run.",
    )
    parser.add_argument(
        "--merchant-kb-save-every",
        type=int,
        default=0,
        help="Merge pending AI-verified merchant keywords into merchant_kb.csv every N API calls. Use 0 to write only at the end.",
    )
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=20)
    parser.add_argument("--retry-delay-seconds", type=float, default=10.0)
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
    parser.add_argument(
        "--cache-save-every",
        type=int,
        default=10,
        help="Save verification cache every N API calls. Use 1 for safest writes or 0 to save only at the end/on exit.",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=20,
        help="Write a partial CSV every N API calls. Use 0 to disable periodic checkpoint files.",
    )
    parser.add_argument(
        "--checkpoint-output",
        type=Path,
        default=None,
        help="Partial CSV path used while the run is still in progress.",
    )
    parser.add_argument("--max-api-calls", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=5, help="Number of candidates per API call. Default 5.")
    parser.add_argument("--row-limit", type=int, default=None)
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if not args.api_key:
        parser.error("Missing DeepSeek API key. Set DEEPSEEK_API_KEY or pass --api-key.")
    if args.max_retries < 1:
        parser.error("--max-retries must be at least 1.")
    if args.merchant_kb_save_every < 0:
        parser.error("--merchant-kb-save-every must be 0 or greater.")
    if args.cache_save_every < 0:
        parser.error("--cache-save-every must be 0 or greater.")
    if args.checkpoint_every < 0:
        parser.error("--checkpoint-every must be 0 or greater.")
    process_file(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
