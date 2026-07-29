"""
Clean merchant keyword lists in merchant_kb.csv.

Performance-focused version of dedup_keywords.py.

The script keeps the original cleaning rules and output format, while reducing
repeated tokenisation, regex compilation, upper-casing, dictionary work and
expensive SequenceMatcher calls. It still streams the CSV through a temporary
file, so memory usage stays bounded for large knowledge bases.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import heapq
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from settings import (
    FINAL_OUTPUT,
    FINAL_OUTPUT_COLUMNS,
    KEYWORD_NAME_SIMILARITY_THRESHOLD,
    KNOWN_ABBREVIATIONS,
    MIN_DISTINCTIVE_KEYWORD_TOKENS,
    MIN_KEYWORD_LEN,
    PAYMENT_PREFIX_WORDS,
    STOPWORDS,
)


KEYWORD_SEPARATOR: Final = " | "
REPORT_LIMIT: Final = 500
IO_BUFFER_SIZE: Final = 1024 * 1024
RAW_STRING_SIMILARITY_THRESHOLD: Final = 0.55

TOKEN_RE: Final = re.compile(r"[A-Za-z0-9&']+")
CORE_PUNCT_RE: Final = re.compile(r"[.,&']")
ENTITY_SUFFIX_RE: Final = re.compile(
    r"(?:"
    r"\bPROPRIETARY\s+LIMITED\b|"
    r"\bPTY\.?\s*LTD\.?\b|"
    r"\bPTY\s+LIMITED\b|"
    r"\bPTY\b|"
    r"\bLIMITED\b|"
    r"\bLTD\b|"
    r"\bINC\b|"
    r"\bLLC\b|"
    r"\bPLC\b|"
    r"\bINCORPORATED\b"
    r")",
    flags=re.IGNORECASE,
)

# Convert once so all membership checks are O(1), even if settings.py uses lists.
STOPWORDS_SET: Final = frozenset(STOPWORDS)
PAYMENT_PREFIX_WORDS_SET: Final = frozenset(PAYMENT_PREFIX_WORDS)
KNOWN_ABBREVIATIONS_SET: Final = frozenset(KNOWN_ABBREVIATIONS)

# Tuple positions returned by _clean_keywords_impl.
REMOVED_LEN = 0
REMOVED_STOPWORD = 1
REMOVED_DUP = 2
REMOVED_GENERIC = 3
REMOVED_NAME_MISMATCH = 4



def split_keywords(keywords_raw: str) -> list[str]:
    """Split and normalize a pipe-delimited keyword string."""
    if not keywords_raw:
        return []
    return [
        normalized
        for part in keywords_raw.split("|")
        if (normalized := " ".join(part.split()))
    ]



def keyword_identity(keyword: str) -> str:
    """Return the same duplicate-comparison identity used by the original code."""
    return " ".join(TOKEN_RE.findall(keyword or "")).casefold()



def keyword_tokens(keyword: str) -> list[str]:
    """Return upper-case regex tokens."""
    return [token.upper() for token in TOKEN_RE.findall(keyword or "")]



def is_known_short_keyword(keyword: str) -> bool:
    return keyword.upper() in KNOWN_ABBREVIATIONS_SET



def has_enough_distinctive_tokens(keyword: str) -> bool:
    tokens_upper = keyword_tokens(keyword)
    distinctive_count = sum(
        token not in STOPWORDS_SET and token not in PAYMENT_PREFIX_WORDS_SET
        for token in tokens_upper
    )
    return distinctive_count >= MIN_DISTINCTIVE_KEYWORD_TOKENS



def _core_tokens(name: str) -> frozenset[str]:
    """
    Return normalized, deduplicated tokens after removing legal-entity suffixes.

    The original version called re.sub once for every suffix. This version uses
    one precompiled combined regex and one precompiled punctuation regex.
    """
    if not name:
        return frozenset()

    stripped = ENTITY_SUFFIX_RE.sub("", name)
    stripped = CORE_PUNCT_RE.sub("", stripped)
    return frozenset(
        token
        for token in TOKEN_RE.findall(stripped.upper())
        if token not in STOPWORDS_SET
    )



def _token_overlap(
    merchant_tokens: frozenset[str],
    keyword_tokens_set: frozenset[str],
) -> float:
    """Ratio of shared tokens to the larger token set (0.0-1.0)."""
    if not merchant_tokens or not keyword_tokens_set:
        return 0.0
    shared = len(merchant_tokens & keyword_tokens_set)
    return shared / max(len(merchant_tokens), len(keyword_tokens_set))



def _contains_known_abbreviation_tokens(tokens_upper: list[str]) -> bool:
    return any(token in KNOWN_ABBREVIATIONS_SET for token in tokens_upper)



def _raw_similarity_is_below_threshold(
    merchant_name_upper: str,
    keyword_upper: str,
) -> bool:
    """
    Return whether SequenceMatcher.ratio() is below 0.55, preserving the exact
    original decision.

    real_quick_ratio() and quick_ratio() are upper bounds on ratio(). When either
    upper bound is already below the threshold, the expensive ratio() calculation
    can be skipped safely without changing the result.
    """
    matcher = difflib.SequenceMatcher(None, merchant_name_upper, keyword_upper)

    if matcher.real_quick_ratio() < RAW_STRING_SIMILARITY_THRESHOLD:
        return True
    if matcher.quick_ratio() < RAW_STRING_SIMILARITY_THRESHOLD:
        return True
    return matcher.ratio() < RAW_STRING_SIMILARITY_THRESHOLD



def _clean_keywords_impl(
    keywords_raw: str,
    merchant_name: str,
    *,
    collect_removed_details: bool,
) -> tuple[str, list[str], list[str], int, tuple[int, int, int, int, int]]:
    """
    Internal high-performance cleaner.

    Returns:
        cleaned string,
        removed detail list,
        kept keyword list,
        original keyword count,
        removal counters in this order:
            length, stopword, duplicate, generic, name mismatch.
    """
    if not keywords_raw or not keywords_raw.strip():
        return "", [], [], 0, (0, 0, 0, 0, 0)

    keywords = split_keywords(keywords_raw)
    original_count = len(keywords)

    kept: list[str] = []
    removed: list[str] = []
    seen: set[str] = set()
    removal_counts = [0, 0, 0, 0, 0]

    merchant_name_upper = merchant_name.upper() if merchant_name else ""
    merchant_core = _core_tokens(merchant_name) if merchant_name else frozenset()

    # The original max(list, key=...) keeps the first keyword on a similarity tie.
    best_fuzzy_similarity = -1.0
    best_fuzzy_keyword = ""

    for keyword in keywords:
        keyword_upper = keyword.upper()
        tokens = TOKEN_RE.findall(keyword)
        tokens_upper = [token.upper() for token in tokens]
        keyword_key = " ".join(tokens).casefold()
        is_single_token = " " not in keyword
        is_known_short = keyword_upper in KNOWN_ABBREVIATIONS_SET

        if len(keyword) < MIN_KEYWORD_LEN and not is_known_short:
            removal_counts[REMOVED_LEN] += 1
            if collect_removed_details:
                removed.append(f"[LEN<{MIN_KEYWORD_LEN}] {keyword}")
            continue

        if is_single_token and keyword_upper in STOPWORDS_SET:
            removal_counts[REMOVED_STOPWORD] += 1
            if collect_removed_details:
                removed.append(f"[STOPWORD] {keyword}")
            continue

        distinctive_count = sum(
            token not in STOPWORDS_SET and token not in PAYMENT_PREFIX_WORDS_SET
            for token in tokens_upper
        )
        if distinctive_count < MIN_DISTINCTIVE_KEYWORD_TOKENS and not is_known_short:
            removal_counts[REMOVED_GENERIC] += 1
            if collect_removed_details:
                removed.append(f"[GENERIC] {keyword}")
            continue

        if keyword_key in seen:
            removal_counts[REMOVED_DUP] += 1
            if collect_removed_details:
                removed.append(f"[DUP] {keyword}")
            continue

        seen.add(keyword_key)

        if (
            merchant_core
            and not is_known_short
            and not _contains_known_abbreviation_tokens(tokens_upper)
        ):
            keyword_core = _core_tokens(keyword)
            if keyword_core:
                similarity = _token_overlap(merchant_core, keyword_core)
                if similarity <= KEYWORD_NAME_SIMILARITY_THRESHOLD:
                    if _raw_similarity_is_below_threshold(
                        merchant_name_upper,
                        keyword_upper,
                    ):
                        removal_counts[REMOVED_NAME_MISMATCH] += 1
                        if collect_removed_details:
                            removed.append(
                                f"[NAMEMISMATCH:{similarity:.2f}] {keyword}"
                            )
                        if similarity > best_fuzzy_similarity:
                            best_fuzzy_similarity = similarity
                            best_fuzzy_keyword = keyword
                        continue

        kept.append(keyword)

    if not kept:
        if best_fuzzy_keyword:
            kept.append(best_fuzzy_keyword)
        elif merchant_name:
            fallback = " ".join(merchant_name.split())
            if fallback:
                kept.append(fallback)

    return (
        KEYWORD_SEPARATOR.join(kept),
        removed,
        kept,
        original_count,
        tuple(removal_counts),
    )



def clean_keywords(
    keywords_raw: str,
    merchant_name: str,
) -> tuple[str, list[str], list[str]]:
    """
    Public backward-compatible API.

    Return (cleaned keywords string, removed detail list, kept keyword list).
    """
    cleaned, removed, kept, _, _ = _clean_keywords_impl(
        keywords_raw,
        merchant_name,
        collect_removed_details=True,
    )
    return cleaned, removed, kept



def should_process_row(
    row: dict[str, str],
    full_clean: bool,
    changed_since: str,
) -> bool:
    if full_clean or not changed_since:
        return True
    return row.get("keyword_updated_at", "").strip() >= changed_since



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



def write_report(
    report_path: Path,
    report_heap: list[tuple[int, int, dict[str, str]]],
) -> None:
    if not report_heap:
        return

    report_path.parent.mkdir(parents=True, exist_ok=True)
    details = [item[2] for item in sorted(report_heap, reverse=True)]
    with report_path.open(
        "w",
        encoding="utf-8",
        newline="",
        buffering=IO_BUFFER_SIZE,
    ) as target:
        writer = csv.DictWriter(
            target,
            fieldnames=["merchant_name", "removed_count", "removed"],
        )
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
    whose keyword_updated_at value is greater than or equal to that timestamp.
    The now argument is kept for backward-compatible callers and is not used.
    """
    del now

    if not input_path.exists():
        print(f"[dedup_keywords] {input_path} not found -- nothing to clean")
        return None

    mode = "FULL" if full_clean or not changed_since else f"CHANGED SINCE {changed_since}"
    collect_removed_details = report_path is not None

    report_heap: list[tuple[int, int, dict[str, str]]] = []
    temporary_path: Path | None = None

    # Local integer counters are substantially cheaper than Counter updates in a
    # tight row loop.
    total_rows = 0
    rows_processed = 0
    rows_changed = 0
    rows_with_removed_keywords = 0
    total_kw_before = 0
    total_kw_after = 0
    removed_len = 0
    removed_stopword = 0
    removed_dup = 0
    removed_generic = 0
    removed_namemismatch = 0

    process_all_rows = full_clean or not changed_since

    try:
        with input_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
            buffering=IO_BUFFER_SIZE,
        ) as source:
            reader = csv.DictReader(source)
            input_columns = reader.fieldnames or FINAL_OUTPUT_COLUMNS

            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="",
                buffering=IO_BUFFER_SIZE,
                delete=False,
                dir=input_path.parent,
            ) as temporary:
                temporary_path = Path(temporary.name)
                writer = csv.DictWriter(
                    temporary,
                    fieldnames=input_columns,
                    extrasaction="ignore",
                )
                writer.writeheader()

                for row in reader:
                    total_rows += 1

                    if process_all_rows or row.get(
                        "keyword_updated_at",
                        "",
                    ).strip() >= changed_since:
                        rows_processed += 1

                        keywords_raw = row.get("keywords", "")
                        merchant_name = row.get("merchant_name", "")

                        (
                            clean_str,
                            removed,
                            kept,
                            original_count,
                            removal_counts,
                        ) = _clean_keywords_impl(
                            keywords_raw,
                            merchant_name,
                            collect_removed_details=collect_removed_details,
                        )

                        if clean_str != keywords_raw:
                            row["keywords"] = clean_str
                            rows_changed += 1
                            total_kw_before += original_count
                            total_kw_after += len(kept)

                        row_removed_count = sum(removal_counts)
                        if row_removed_count:
                            rows_with_removed_keywords += 1
                            removed_len += removal_counts[REMOVED_LEN]
                            removed_stopword += removal_counts[REMOVED_STOPWORD]
                            removed_dup += removal_counts[REMOVED_DUP]
                            removed_generic += removal_counts[REMOVED_GENERIC]
                            removed_namemismatch += removal_counts[
                                REMOVED_NAME_MISMATCH
                            ]

                            if report_path is not None:
                                push_report_detail(
                                    report_heap,
                                    rows_with_removed_keywords,
                                    merchant_name,
                                    removed,
                                )

                    # DictWriter already selects fields in input_columns and ignores
                    # extras, so rebuilding a dictionary for every row is unnecessary.
                    writer.writerow(row)

        if temporary_path is None:
            raise RuntimeError("Temporary output file was not created")
        os.replace(temporary_path, input_path)

    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

    total_removed = (
        removed_len
        + removed_stopword
        + removed_dup
        + removed_generic
        + removed_namemismatch
    )

    # Match the original Counter -> dict behaviour: counters that never changed
    # are omitted, while total_kw_before/after are present whenever a row changed
    # because the original code performs += even when the added value is zero.
    stats: dict[str, int] = {}
    if total_rows:
        stats["total_rows"] = total_rows
    if rows_processed:
        stats["rows_processed"] = rows_processed
    if rows_changed:
        stats["rows_changed"] = rows_changed
        stats["total_kw_before"] = total_kw_before
        stats["total_kw_after"] = total_kw_after
    if rows_with_removed_keywords:
        stats["rows_with_removed_keywords"] = rows_with_removed_keywords
    if total_removed:
        stats["total_removed"] = total_removed
    if removed_len:
        stats["removed_len"] = removed_len
    if removed_stopword:
        stats["removed_stopword"] = removed_stopword
    if removed_dup:
        stats["removed_dup"] = removed_dup
    if removed_generic:
        stats["removed_generic"] = removed_generic
    if removed_namemismatch:
        stats["removed_namemismatch"] = removed_namemismatch

    print(
        f"[dedup_keywords] {mode} | "
        f"rows: {total_rows:,} "
        f"processed: {rows_processed:,} "
        f"changed: {rows_changed:,} | "
        f"removed: {total_removed:,} "
        f"(len:{removed_len:,} "
        f"stop:{removed_stopword:,} "
        f"dup:{removed_dup:,} "
        f"generic:{removed_generic:,} "
        f"mismatch:{removed_namemismatch:,})"
    )

    if report_path is not None:
        write_report(report_path, report_heap)

    return stats



def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean keywords in merchant_kb.csv",
    )
    parser.add_argument("--input", type=Path, default=FINAL_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Skip the detail report for a little more speed.",
    )
    parser.add_argument(
        "--changed-since",
        default="",
        help="Only clean rows with keyword_updated_at >= this timestamp",
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
        report_path=None if args.no_report else args.report,
        changed_since=args.changed_since,
    )


if __name__ == "__main__":
    main()
