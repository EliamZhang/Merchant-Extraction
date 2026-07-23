import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

CHINA_TIMEZONE = timezone(timedelta(hours=8))


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
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return csv.DictReader(io.StringIO(text, newline=""))


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
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc
