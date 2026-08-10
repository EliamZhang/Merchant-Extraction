from __future__ import annotations

import csv
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib import error, request

T = TypeVar("T")


class ApiError(RuntimeError):
    """API request failure. retryable=False means the request should not be retried."""

    def __init__(self, message: str, status_code: int | None = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable

CHINA_TIMEZONE = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# ServiFlow-AI 兼容：与 classification_core/text.py:clean_text 完全等价
# ---------------------------------------------------------------------------

_PRECLEAN_RE = re.compile(r"[^A-Z0-9]+")


def clean_keyword_text(value: str) -> str:
    """清洗关键词：大写 + 去除非字母数字字符 + 合并空格。

    与 ServiFlow-AI ``classification_core/text.py:clean_text`` 完全等价，
    确保关键词在 CSV 写入阶段就规范化，运行时无需再次清洗。
    """
    if not value:
        return ""
    text = value.upper()
    text = _PRECLEAN_RE.sub(" ", text)
    return " ".join(text.split())


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_search_text(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]+", " ", value or "")
    return normalize_space(cleaned).casefold()


KEYWORD_SEPARATOR = " | "


def split_kb_keywords(value: str) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for keyword in re.split(r"\s*\|\s*", normalize_space(value)):
        cleaned = normalize_space(keyword)
        normalized = normalize_search_text(cleaned)
        if not cleaned or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        keywords.append(cleaned)
    return keywords


def clean_output_value(value: str) -> str:
    return normalize_space(value).strip('"').strip("'")


def safe_url(value: str) -> str:
    url = normalize_space(value)
    if not url:
        return ""
    if re.match(r"^https?://", url, flags=re.IGNORECASE):
        return url
    return ""


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


def china_timestamp_now() -> str:
    return datetime.now(CHINA_TIMEZONE).replace(microsecond=0).isoformat()


def open_csv_dict_reader(path: Path) -> csv.DictReader:
    return csv.DictReader(path.open("r", encoding="utf-8-sig", errors="replace", newline=""))


def post_json(
    apikey, base_url: str, path: str, payload: dict[str, Any], timeout_seconds: int,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {apikey}",
        "Content-Type": "application/json",
    }
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        retryable = exc.code not in (400, 401, 403, 404, 405, 413)
        raise ApiError(f"HTTP {exc.code}: {body[:500]}", status_code=exc.code, retryable=retryable) from exc
    except error.URLError as exc:
        raise ApiError(f"Network error: {exc.reason}") from exc


def call_with_retry(
    func: Callable[[], T],
    *,
    max_retries: int = 20,
    initial_delay_seconds: float = 15.0,
    max_delay_seconds: float = 60.0,
) -> T:
    """Call func with retry. Retries on ApiError(retryable=True), retryable errors and timeouts.

    Backoff is exponential with jitter, capped at max_delay_seconds. Non-retryable
    ApiError (4xx client errors) and ValueError (unparseable model output) propagate
    immediately. Returns on first success, otherwise raises the last error.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except (ApiError, ValueError) as exc:
            if isinstance(exc, ApiError) and not exc.retryable:
                raise
            if isinstance(exc, ValueError):
                raise
            if attempt >= max_retries:
                raise
            delay = min(initial_delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
            time.sleep(delay + random.uniform(0, min(delay, 10)))
    raise AssertionError("unreachable")
