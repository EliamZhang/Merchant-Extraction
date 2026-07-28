"""
Clean merchant keyword lists in merchant_kb.csv.

The script streams the CSV and writes through a temporary file, so it can handle
large knowledge bases without loading every row into memory.
"""

import argparse
import csv
import heapq
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    FINAL_OUTPUT,
    FINAL_OUTPUT_COLUMNS,
    KNOWN_ABBREVIATIONS,
    MIN_DISTINCTIVE_KEYWORD_TOKENS,
    MIN_KEYWORD_LEN,
    PAYMENT_PREFIX_WORDS,
    STOPWORDS,
)


KEYWORD_SEPARATOR = " | "
REPORT_LIMIT = 500
TOKEN_RE = re.compile(r"[A-Za-z0-9&']+")


def split_keywords(keywords_raw: str) -> list[str]:
    return [" ".join(part.split()) for part in (keywords_raw or "").split("|") if part.strip()]


def keyword_identity(keyword: str) -> str:
    return " ".join(TOKEN_RE.findall(keyword or "")).casefold()


def keyword_tokens(keyword: str) -> list[str]:
    return [token.upper() for token in TOKEN_RE.findall(keyword or "")]


def is_known_short_keyword(keyword: str) -> bool:
    return keyword.upper() in KNOWN_ABBREVIATIONS


def has_enough_distinctive_tokens(keyword: str) -> bool:
    tokens = keyword_tokens(keyword)
    distinctive = [
        token
        for token in tokens
        if token not in STOPWORDS
        and token not in PAYMENT_PREFIX_WORDS
    ]
    return len(distinctive) >= MIN_DISTINCTIVE_KEYWORD_TOKENS


def clean_keywords(keywords_raw: str, merchant_name: str) -> tuple[str, list[str], list[str]]:
    """Return (cleaned keywords string, removed detail list, kept keyword list)."""
    if not keywords_raw or not keywords_raw.strip():
        return "", [], []

    kept: list[str] = []
    removed: list[str] = []
    seen: set[str] = set()

    for keyword in split_keywords(keywords_raw):
        keyword_upper = keyword.upper()
        keyword_key = keyword_identity(keyword)
        is_single_token = " " not in keyword

        if len(keyword) < MIN_KEYWORD_LEN and not is_known_short_keyword(keyword):
            removed.append(f"[LEN<{MIN_KEYWORD_LEN}] {keyword}")
            continue

        if is_single_token and keyword_upper in STOPWORDS:
            removed.append(f"[STOPWORD] {keyword}")
            continue

        if not has_enough_distinctive_tokens(keyword) and not is_known_short_keyword(keyword):
            removed.append(f"[GENERIC] {keyword}")
            continue

        if keyword_key in seen:
            removed.append(f"[DUP] {keyword}")
            continue

        seen.add(keyword_key)
        kept.append(keyword)

    if not kept and merchant_name:
        fallback = " ".join(merchant_name.split())
        if fallback:
            kept.append(fallback)

    return KEYWORD_SEPARATOR.join(kept), removed, kept


def should_process_row(row: dict[str, str], full_clean: bool, changed_since: str) -> bool:
    if full_clean or not changed_since:
        return True
    return row.get("keyword_created_at", "").strip() >= changed_since


def push_report_detail(
    report_heap: list[tuple[int, int, dict[str, str]]],
    sequence: int,
    merchant_name: str,
    removed: list[str],
) -> None:
    detail = {
        "merchant_name": merchant_name,
        "removed_count": str(len(removed)),
        "removed": " || ".join(removed),
    }
    item = (len(removed), sequence, detail)
    if len(report_heap) < REPORT_LIMIT:
        heapq.heappush(report_heap, item)
    elif item[0] > report_heap[0][0]:
        heapq.heapreplace(report_heap, item)


def write_report(report_path: Path, report_heap: list[tuple[int, int, dict[str, str]]]) -> None:
    if not report_heap:
        return

    report_path.parent.mkdir(parents=True, exist_ok=True)
    details = [item[2] for item in sorted(report_heap, reverse=True)]
    with report_path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=["merchant_name", "removed_count", "removed"])
        writer.writeheader()
        writer.writerows(details)
    print(f"  report: {report_path}")


DEFAULT_REPORT_PATH = FINAL_OUTPUT.parent / "output" / "clean_report.csv"


def process_keywords(
    input_path: Path = FINAL_OUTPUT,
    full_clean: bool = True,
    now: str = "",
    report_path: Path | None = DEFAULT_REPORT_PATH,
    changed_since: str = "",
) -> dict[str, int] | None:
    """
    Clean the keywords column in a CSV file.

    By default all rows are processed. Pass changed_since to only process rows
    whose keyword_created_at value is greater than or equal to that timestamp.
    The now argument is kept for backward-compatible callers and is not used.
    """
    del now

    if not input_path.exists():
        print(f"[clean_keywords] {input_path} not found -- nothing to clean")
        return None

    mode = "FULL" if full_clean or not changed_since else f"CHANGED SINCE {changed_since}"

    stats: Counter = Counter()
    report_heap: list[tuple[int, int, dict[str, str]]] = []
    temporary_path: Path | None = None

    try:
        with input_path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            input_columns = reader.fieldnames or FINAL_OUTPUT_COLUMNS

            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="",
                delete=False,
                dir=input_path.parent,
            ) as temporary:
                temporary_path = Path(temporary.name)
                writer = csv.DictWriter(temporary, fieldnames=input_columns, extrasaction="ignore")
                writer.writeheader()

                for row in reader:
                    stats["total_rows"] += 1
                    if should_process_row(row, full_clean=full_clean, changed_since=changed_since):
                        stats["rows_processed"] += 1
                        keywords_raw = row.get("keywords", "")
                        merchant_name = row.get("merchant_name", "")
                        original_count = len(split_keywords(keywords_raw))
                        clean_str, removed, kept = clean_keywords(keywords_raw, merchant_name)

                        if clean_str != keywords_raw:
                            row["keywords"] = clean_str
                            stats["rows_changed"] += 1
                            stats["total_kw_before"] += original_count
                            stats["total_kw_after"] += len(kept)

                        if removed:
                            stats["rows_with_removed_keywords"] += 1
                            for item in removed:
                                if item.startswith("[LEN"):
                                    stats["removed_len"] += 1
                                    stats["total_removed"] += 1
                                elif item.startswith("[STOPWORD]"):
                                    stats["removed_stopword"] += 1
                                    stats["total_removed"] += 1
                                elif item.startswith("[DUP]"):
                                    stats["removed_dup"] += 1
                                    stats["total_removed"] += 1
                                elif item.startswith("[GENERIC]"):
                                    stats["removed_generic"] += 1
                                    stats["total_removed"] += 1
                            if report_path:
                                push_report_detail(
                                    report_heap,
                                    stats["rows_with_removed_keywords"],
                                    merchant_name,
                                    removed,
                                )

                    writer.writerow({column: row.get(column, "") for column in input_columns})

        os.replace(temporary_path, input_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

    print(f"[clean_keywords] {mode} | rows: {stats['total_rows']:,} processed: {stats['rows_processed']:,} changed: {stats['rows_changed']:,} | removed: {stats['total_removed']:,} (len:{stats['removed_len']:,} stop:{stats['removed_stopword']:,} dup:{stats['removed_dup']:,} generic:{stats['removed_generic']:,})")

    if report_path:
        write_report(report_path, report_heap)

    return dict(stats)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean keywords in merchant_kb.csv")
    parser.add_argument("--input", type=Path, default=FINAL_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--changed-since",
        default="",
        help="Only clean rows with keyword_created_at >= this timestamp",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Clean all rows. This is the default when --changed-since is omitted.",
    )
    args = parser.parse_args()

    process_keywords(
        input_path=args.input,
        full_clean=args.full or not args.changed_since,
        report_path=args.report,
        changed_since=args.changed_since,
    )


if __name__ == "__main__":
    main()
