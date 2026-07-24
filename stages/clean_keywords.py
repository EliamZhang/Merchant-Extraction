"""
Stage 4: 关键词清洗
===================
清洗 kb_internal.csv 中的 keywords 字段。

模式:
  - 增量（默认）：只清洗 keyword_created_at == 本次运行时间 的记录
  - 全量（--full）：清洗全部记录

清洗规则:
  1. 长度 <= MIN_KEYWORD_LEN -> 移除
  2. 单 token 且在 STOPWORDS 中 -> 移除
  3. 大小写去重
"""

import csv
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    INTERNAL_FILE, KB_INTERNAL_COLUMNS,
    MIN_KEYWORD_LEN, STOPWORDS,
)


def clean_keywords(keywords_raw: str, merchant_name: str) -> tuple[str, list[str], list[str]]:
    """
    清理关键词字符串。
    返回: (clean_str, removed_list, kept_list)
    """
    if not keywords_raw or not keywords_raw.strip():
        return "", [], []

    kws = [kw.strip() for kw in keywords_raw.split(" | ")]
    kept = []
    removed = []
    seen = set()

    for kw in kws:
        if not kw:
            continue

        kw_upper = kw.upper()
        is_single_token = " " not in kw

        # 长度过滤
        if len(kw) < MIN_KEYWORD_LEN:
            removed.append(f"[LEN<{MIN_KEYWORD_LEN}] {kw}")
            continue

        # STOPWORDS 过滤（仅单 token）
        if is_single_token and kw_upper in STOPWORDS:
            removed.append(f"[STOPWORD] {kw}")
            continue

        # 去重
        if kw_upper in seen:
            removed.append(f"[DUP] {kw}")
            continue

        seen.add(kw_upper)
        kept.append(kw)

    # 如果全部被移除，保留 merchant_name 作为兜底
    if not kept and merchant_name:
        kept.append(merchant_name.strip())

    clean_str = " | ".join(kept)
    return clean_str, removed, kept


def process_keywords(internal_path: Path = INTERNAL_FILE, full_clean: bool = False,
                     now: str = "", report_path: Path = None):
    """
    清洗 kb_internal.csv 的 keywords 列。

    full_clean=False: 只清洗 keyword_created_at == now 的记录
    full_clean=True:  清洗全部记录

    返回统计字典。
    """
    if not now:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    if not internal_path.exists():
        print(f"[clean_keywords] {internal_path} not found -- nothing to clean")
        return None

    print(f"[clean_keywords] Loading {internal_path}...")
    with open(internal_path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    mode = "FULL" if full_clean else "INCREMENTAL"
    print(f"[clean_keywords] Mode: {mode}  |  Total rows: {len(rows):,}")

    stats = Counter()
    cleaned_details = []  # for report

    for row in rows:
        kca = row.get("keyword_created_at", "").strip()

        # 增量模式：跳过不需要清洗的记录
        if not full_clean and kca != now:
            # 但如果有空的 keyword_created_at 也洗（兼容首次运行遗留）
            if kca:
                continue

        keywords_raw = row.get("keywords", "")
        merchant_name = row.get("merchant_name", "")

        original_count = len([k for k in keywords_raw.split(" | ") if k.strip()])
        clean_str, removed, kept = clean_keywords(keywords_raw, merchant_name)

        if removed:
            stats["rows_affected"] += 1
            stats["total_removed"] += len(removed)
            for r in removed:
                if r.startswith("[LEN"):
                    stats["removed_len"] += 1
                elif r.startswith("[STOPWORD]"):
                    stats["removed_stopword"] += 1
                elif r.startswith("[DUP]"):
                    stats["removed_dup"] += 1

            row["keywords"] = clean_str
            stats["total_kw_before"] += original_count
            stats["total_kw_after"] += len(kept)

            cleaned_details.append({
                "merchant_name": merchant_name,
                "removed_count": len(removed),
                "removed": " || ".join(removed),
            })

    stats["total_rows"] = len(rows)

    # -- 写回 --
    print(f"[clean_keywords] Writing back to {internal_path}...")
    with open(internal_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=KB_INTERNAL_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

    # -- 统计 --
    print(f"\n[clean_keywords] {'='*50}")
    print(f"  Mode:            {mode}")
    print(f"  Total rows:      {stats['total_rows']:>10,}")
    print(f"  Rows affected:   {stats['rows_affected']:>10,}")
    print(f"  Keywords removed:{stats['total_removed']:>10,}")
    print(f"    - Length:      {stats['removed_len']:>10,}")
    print(f"    - Stopword:    {stats['removed_stopword']:>10,}")
    print(f"    - Dedup:       {stats['removed_dup']:>10,}")

    # -- 写详细报告 --
    if report_path and cleaned_details:
        cleaned_details.sort(key=lambda x: x["removed_count"], reverse=True)
        with open(report_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["merchant_name", "removed_count", "removed"])
            writer.writeheader()
            for d in cleaned_details[:500]:
                writer.writerow(d)
        print(f"  Report:          {report_path}")

    return dict(stats)


# --- 独立运行 ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Clean keywords in kb_internal.csv")
    parser.add_argument("--full", action="store_true", help="Full clean (all rows)")
    parser.add_argument("--input", type=Path, default=INTERNAL_FILE)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    process_keywords(args.input, full_clean=args.full, report_path=args.report)
