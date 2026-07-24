"""
Stage 3: 增量合并
=================
将新的 filtered.csv 与旧的 kb_internal.csv 按 match_key 做增量合并。

输入:  data/filtered.csv + data/kb_internal.csv（如果存在）
输出:  data/kb_internal.csv（更新后）+ data/changelog.csv
"""

import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    FILTERED_FILE, INTERNAL_FILE, CHANGELOG_FILE,
    KB_INTERNAL_COLUMNS, STATUS_GONE,
)


def load_csv_as_dict(path: Path, key_column: str = "match_key") -> dict[str, dict]:
    """加载 CSV 为 {key: row_dict} 索引."""
    if not path.exists():
        return {}
    index = {}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            k = row.get(key_column, "").strip()
            if k:
                index[k] = row
    return index


def load_csv_rows(path: Path) -> list[dict]:
    """加载 CSV 所有行."""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def keywords_equal(kw1: str, kw2: str) -> bool:
    """比较两个 keywords 字符串是否语义相同（忽略空格差异）."""
    def normalize(s):
        return " | ".join(sorted(
            k.strip() for k in s.split("|") if k.strip()
        ))
    return normalize(kw1) == normalize(kw2)


def aggregate_new_records(rows: list[dict]) -> dict[str, dict]:
    """聚合同一 match_key 的多条记录。冲突时: ACT > CAN, 最近 record_updated > 旧."""
    groups = defaultdict(list)
    for row in rows:
        mk = row.get("match_key", "").strip()
        if mk:
            groups[mk].append(row)

    result = {}
    for mk, recs in groups.items():
        if len(recs) == 1:
            result[mk] = recs[0]
        else:
            # 优先级: ACT > CAN, 然后 record_updated 最新
            def sort_key(r):
                status = r.get("abn_status", "")
                updated = r.get("record_updated", "")
                return (0 if status == "ACT" else 1, updated)
            recs.sort(key=sort_key)
            result[mk] = recs[0]
    return result


def incremental_merge(
    filtered_path: Path = FILTERED_FILE,
    internal_path: Path = INTERNAL_FILE,
    changelog_path: Path = CHANGELOG_FILE,
    now: str | None = None,
):
    """执行增量合并，返回统计字典。"""
    if now is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    internal_path.parent.mkdir(parents=True, exist_ok=True)

    # -- 加载数据 --
    print("[merge] Loading existing kb_internal.csv...")
    old_kb = load_csv_as_dict(internal_path)
    is_first_run = len(old_kb) == 0
    print(f"  Existing records: {len(old_kb):,}")

    print("[merge] Loading filtered.csv...")
    new_rows = load_csv_rows(filtered_path)
    print(f"  New (filtered) records: {len(new_rows):,}")

    new_index = aggregate_new_records(new_rows)
    print(f"  Unique match_keys in new data: {len(new_index):,}")

    # -- 合并 --
    stats = {
        "new": 0,
        "updated": 0,
        "gone": 0,
        "unchanged": 0,
        "keyword_changed": 0,
    }

    merged = []
    changelog = []

    new_keys = set(new_index.keys())
    old_keys = set(old_kb.keys())

    # 新增 & 更新
    for mk in new_keys:
        new_row = new_index[mk]

        if mk in old_kb:
            old_row = old_kb[mk]

            # 更新元数据（用新数据覆盖）
            new_row["entity_type"] = new_row.get("entity_type", old_row.get("entity_type", ""))
            new_row["abn_status"] = new_row.get("abn_status", old_row.get("abn_status", ""))
            new_row["status_date"] = new_row.get("status_date", old_row.get("status_date", ""))
            new_row["state"] = new_row.get("state", old_row.get("state", ""))
            new_row["record_updated"] = new_row.get("record_updated", old_row.get("record_updated", ""))

            # keywords 比较
            old_kw = old_row.get("keywords", "")
            new_kw = new_row.get("keywords", "")
            if keywords_equal(old_kw, new_kw):
                # 没变化 -> 保留旧的（已清洗的）
                new_row["keywords"] = old_kw
                new_row["keyword_created_at"] = old_row.get("keyword_created_at", "")
                stats["unchanged"] += 1
            else:
                # 变了 -> 标记为需清洗
                new_row["keyword_created_at"] = now
                stats["keyword_changed"] += 1
                stats["updated"] += 1
                changelog.append({"match_key": mk, "change": "keywords_updated",
                                  "merchant_name": new_row.get("merchant_name", ""),
                                  "old_kw_count": len(old_kw.split("|")),
                                  "new_kw_count": len(new_kw.split("|"))})

            # 保留旧的 in_kb_since 和 category
            new_row["in_kb_since"] = old_row.get("in_kb_since", now)
            new_row["link"] = old_row.get("link", "")
            cat_old = old_row.get("category", "")
            cat_new = new_row.get("category", "")
            new_row["category"] = cat_old if cat_old else cat_new

        else:
            # 全新记录
            new_row["keyword_created_at"] = now
            new_row["in_kb_since"] = now
            new_row["link"] = ""
            new_row["category"] = ""
            stats["new"] += 1
            changelog.append({"match_key": mk, "change": "new",
                              "merchant_name": new_row.get("merchant_name", "")})

        merged.append(new_row)

    # 消失的记录 -> 标记 GONE
    gone_keys = old_keys - new_keys
    for mk in gone_keys:
        old_row = old_kb[mk]
        old_status = old_row.get("abn_status", "")
        if old_status != STATUS_GONE:
            old_row["abn_status"] = STATUS_GONE
            stats["gone"] += 1
            changelog.append({"match_key": mk, "change": "gone",
                              "merchant_name": old_row.get("merchant_name", "")})
        merged.append(old_row)

    # 如果首次运行，全部标记为新增
    if is_first_run:
        stats["new"] = len(merged)
        stats["keyword_changed"] = len(merged)

    # -- 写入 kb_internal.csv --
    print(f"\n[merge] Writing {len(merged):,} records to {internal_path}...")
    with open(internal_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=KB_INTERNAL_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(merged)

    # -- 写入 changelog.csv --
    if changelog:
        print(f"[merge] Writing changelog ({len(changelog)} entries)...")
        with open(changelog_path, "w", encoding="utf-8", newline="") as f:
            fieldnames = ["match_key", "change", "merchant_name", "old_kw_count", "new_kw_count"]
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(changelog)

    # -- 打印统计 --
    print(f"\n[merge] {'='*50}")
    print(f"[merge] {'First Run -- all records are new' if is_first_run else 'Incremental Update'}")
    print(f"[merge] {'='*50}")
    print(f"  New:              {stats['new']:>10,}")
    print(f"  Updated:          {stats['updated']:>10,}")
    print(f"  Keywords changed: {stats['keyword_changed']:>10,}")
    print(f"  Unchanged:        {stats['unchanged']:>10,}")
    print(f"  Gone (marked):    {stats['gone']:>10,}")
    print(f"  Total in KB:      {len(merged):>10,}")

    return stats


# --- 独立运行 ---
if __name__ == "__main__":
    incremental_merge()
