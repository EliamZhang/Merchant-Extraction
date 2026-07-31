#!/usr/bin/env python3
"""
将 merchant_kb_categorized.json 中的分类更新到 merchant_kb.csv。

用法:
    python update_category.py

输入:
    - knowledge-base-classify/merchant_kb_categorized.json  (分类数据)
    - merchant_kb.csv                                      (待更新的 CSV)

输出:
    - merchant_kb.csv  (原地更新，修改 category 和 category_updated_at 列)
"""

import json
import csv
from datetime import datetime, timezone, timedelta


def load_categorized(json_path: str) -> dict[str, str]:
    """读取 JSON 分类文件，返回 {merchant_name: category} 映射。"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return {
        item["merchant_name"].strip(): item["category"].strip()
        for item in data
        if item.get("category", "").strip()
    }


def update_csv(csv_path: str, cat_map: dict[str, str]) -> dict:
    """按 merchant_name 匹配并更新 CSV 的 category 列。"""
    now = datetime.now(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H:%M:%S+08:00"
    )

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    updated = 0
    skipped = 0

    for row in rows:
        name = row["merchant_name"].strip()
        if name in cat_map:
            new_cat = cat_map[name]
            old_cat = row.get("category", "").strip()
            if old_cat != new_cat:
                row["category"] = new_cat
                row["category_updated_at"] = now
                updated += 1
            else:
                skipped += 1

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return {
        "total_csv": len(rows),
        "in_map": len(cat_map),
        "matched": updated + skipped,
        "updated": updated,
        "skipped_unchanged": skipped,
    }


def main():
    base = r"C:\Users\zhangyuliang02\Desktop\Merchant Extraction"
    json_path = f"{base}\\knowledge-base-classify\\merchant_kb_categorized.json"
    csv_path = f"{base}\\merchant_kb.csv"

    cat_map = load_categorized(json_path)
    print(f"分类映射: {len(cat_map)} 条")

    result = update_csv(csv_path, cat_map)

    print(f"CSV 总行数: {result['total_csv']}")
    print(f"匹配到: {result['matched']} 行")
    print(f"已更新: {result['updated']} 行")
    print(f"未变化(跳过): {result['skipped_unchanged']} 行")


if __name__ == "__main__":
    main()
