"""
Stage 2: 过滤 & 名称处理
========================
读取 data/parsed/*.csv -> 过滤 -> 生成 merchant_name + keywords -> data/filtered.csv

过滤规则:
  1. 只保留 EntityTypeInd IN ('PRV', 'PUB')
  2. 排除 status='CAN' AND status_date < CANCEL_CUTOFF_DATE (2023-01-01)

名称处理: merchant_name = 原始 MN 名称（不做任何清洗），keywords = 所有原始名称用 " | " 拼接
"""

import csv
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    PARSED_DIR, FILTERED_FILE,
    KEEP_ENTITY_TYPES, CANCEL_CUTOFF_DATE,
    KB_INTERNAL_COLUMNS,
)


# --- 过滤逻辑 ---

def should_keep(row: dict) -> tuple[bool, str]:
    """判断是否保留该记录。返回 (keep, reason)。"""
    entity_type = row.get("entity_type_ind", "").strip()

    # 1. 只保留 PRV / PUB
    if entity_type not in KEEP_ENTITY_TYPES:
        return False, f"entity_type={entity_type}"

    # 2. 排除 2023 年前注销的
    status = row.get("abn_status", "").strip()
    status_date = row.get("status_date", "").strip()
    if status == "CAN" and status_date < CANCEL_CUTOFF_DATE:
        return False, f"cancelled_before={CANCEL_CUTOFF_DATE}"

    return True, "ok"


def collect_raw_names(row: dict) -> list[str]:
    """收集所有原始名称（MN + TRD + BN + OTN），去重保持顺序."""
    seen = set()
    names = []

    # MN 名称（主名）
    mn = row.get("mn_name_raw", "").strip()
    if mn and mn not in seen:
        seen.add(mn)
        names.append(mn)

    # 各类别名
    for field in ["trading_names", "business_names", "other_names"]:
        raw = row.get(field, "")
        if raw:
            for name in raw.split(" | "):
                name = name.strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)

    return names


# --- 主流程 ---

def filter_and_transform(parsed_dir: Path = PARSED_DIR, output_path: Path = FILTERED_FILE):
    """读取 parsed/*.csv，过滤并转换为 filtered.csv。"""
    csv_files = sorted(parsed_dir.glob("*.csv"))
    if not csv_files:
        print(f"[filter] No parsed CSV files found in {parsed_dir}")
        return None

    print(f"[filter] Processing {len(csv_files)} parsed CSV file(s)")
    start = datetime.now()

    # -- 第一遍：统计原始 MN 名称冲突（用于追加 ABN 消歧） --
    print("[filter] Pass 1: counting merchant names...")
    name_counts = Counter()
    total_scanned = 0
    total_filtered = Counter()

    for cf in csv_files:
        with open(cf, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_scanned += 1
                keep, reason = should_keep(row)
                if not keep:
                    total_filtered[reason] += 1
                    continue
                mn_raw = row.get("mn_name_raw", "").strip()
                if mn_raw:
                    name_counts[mn_raw] += 1

    print(f"  Scanned: {total_scanned:,}  |  Kept: {total_scanned - sum(total_filtered.values()):,}")
    for reason, cnt in total_filtered.most_common():
        print(f"    Filtered out ({reason}): {cnt:,}")

    # -- 第二遍：生成最终记录 --
    print("[filter] Pass 2: writing filtered.csv...")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with open(output_path, "w", encoding="utf-8", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=KB_INTERNAL_COLUMNS)
        writer.writeheader()

        for cf in csv_files:
            with open(cf, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    keep, reason = should_keep(row)
                    if not keep:
                        continue

                    mn_raw = row.get("mn_name_raw", "").strip()
                    abn = row.get("abn", "").strip()

                    # merchant_name = 原始 MN 名称
                    merchant_name = mn_raw

                    # 同名去重：追加 ABN
                    if abn and name_counts.get(mn_raw, 0) > 1:
                        merchant_name = f"{merchant_name} [ABN {abn}]"

                    # keywords = 所有原始名称用 " | " 拼接
                    all_names = collect_raw_names(row)
                    keywords = " | ".join(all_names)

                    out_row = {
                        "match_key": row.get("match_key", ""),
                        "merchant_name": merchant_name,
                        "keywords": keywords,
                        "link": "",
                        "category": "",
                        "entity_type": row.get("entity_type_ind", ""),
                        "abn_status": row.get("abn_status", ""),
                        "status_date": row.get("status_date", ""),
                        "state": row.get("state", ""),
                        "record_updated": row.get("record_updated", ""),
                        "keyword_created_at": "",
                        "in_kb_since": "",
                    }
                    writer.writerow(out_row)
                    written += 1

    elapsed = (datetime.now() - start).total_seconds()
    print(f"[filter] Done! {written:,} records written to {output_path} in {elapsed:.0f}s")
    return {"scanned": total_scanned, "written": written, "filtered": dict(total_filtered)}


# --- 独立运行 ---
if __name__ == "__main__":
    filter_and_transform()
