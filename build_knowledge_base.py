"""
build_knowledge_base — 官方企业库构建
=========================
ABR XML → 商户知识库：解析 → 过滤 → 合并 → 分类

用法:
  python build_knowledge_base.py              # 全流程
  python build_knowledge_base.py --skip-parse # 跳过 XML 解析
"""

import argparse, csv, hashlib, sys, traceback, xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    RAW_DIR, PARSED_DIR, FILTERED_FILE, MATCH_KEY_LENGTH,
    KEEP_ENTITY_TYPES, CANCEL_CUTOFF_DATE, KB_INTERNAL_COLUMNS,
    INTERNAL_FILE, CHANGELOG_FILE, STATUS_GONE,
    FINAL_OUTPUT, FINAL_OUTPUT_COLUMNS,
)

# ═══════════════════════════════════════════════════════════
# XML 解析
# ═══════════════════════════════════════════════════════════

def get_text(elem, tag, default=""):
    child = elem.find(tag)
    return child.text if child is not None and child.text else default


def get_attrib(elem, tag, attr, default=""):
    child = elem.find(tag)
    if child is not None:
        return child.get(attr, default)
    return default


def normalize_mn_name(name: str) -> str:
    return " ".join(name.upper().split())


def compute_match_key(mn_name_text: str) -> str:
    if not mn_name_text or not mn_name_text.strip():
        return ""
    normalized = normalize_mn_name(mn_name_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:MATCH_KEY_LENGTH]


PARSED_COLUMNS = [
    "match_key", "abn", "mn_name_raw", "mn_name_type",
    "entity_type_ind", "entity_type_text", "abn_status", "status_date",
    "state", "postcode", "record_updated", "asic_number", "asic_type",
    "gst_status", "gst_from",
    "trading_names", "business_names", "other_names", "main_other_names",
]


def extract_other_entities(abr):
    result = {"TRD": [], "BN": [], "OTN": [], "MN": []}
    for oe in abr.findall("OtherEntity"):
        ni = oe.find("NonIndividualName")
        if ni is None:
            continue
        name_type = ni.get("type", "???")
        name_text = get_text(oe, "NonIndividualName/NonIndividualNameText")
        if name_text:
            bucket = result.get(name_type)
            if bucket is not None:
                bucket.append(name_text)
            else:
                result.setdefault(name_type, []).append(name_text)
    return result


def extract_record(abr):
    abn_elem = abr.find("ABN")
    if abn_elem is None:
        abn = abn_status = status_date = ""
    else:
        abn = abn_elem.text or ""
        abn_status = abn_elem.get("status", "")
        status_date = abn_elem.get("ABNStatusFromDate", "")

    entity_type_ind = get_text(abr, "EntityType/EntityTypeInd")
    entity_type_text = get_text(abr, "EntityType/EntityTypeText")
    mn_name_type = get_attrib(abr, "MainEntity/NonIndividualName", "type")
    mn_name_raw = get_text(abr, "MainEntity/NonIndividualName/NonIndividualNameText")
    state = get_text(abr, "MainEntity/BusinessAddress/AddressDetails/State")
    postcode = get_text(abr, "MainEntity/BusinessAddress/AddressDetails/Postcode")

    if not mn_name_raw:
        le = abr.find("LegalEntity/IndividualName")
        if le is not None:
            givens = [gn.text for gn in le.findall("GivenName") if gn is not None and gn.text]
            family = get_text(le, "FamilyName")
            mn_name_raw = " ".join(givens + [family]).strip()
            mn_name_type = le.get("type", "LGL")
            state = state or get_text(abr, "LegalEntity/BusinessAddress/AddressDetails/State")
            postcode = postcode or get_text(abr, "LegalEntity/BusinessAddress/AddressDetails/Postcode")

    match_key = compute_match_key(mn_name_raw)

    asic_elem = abr.find("ASICNumber")
    asic_number = asic_elem.text or "" if asic_elem is not None else ""
    asic_type = asic_elem.get("ASICNumberType", "") if asic_elem is not None else ""

    gst_elem = abr.find("GST")
    gst_status = gst_elem.get("status", "") if gst_elem is not None else ""
    gst_from = gst_elem.get("GSTStatusFromDate", "") if gst_elem is not None else ""

    record_updated = abr.get("recordLastUpdatedDate", "")
    other = extract_other_entities(abr)

    return {
        "match_key": match_key, "abn": abn, "mn_name_raw": mn_name_raw,
        "mn_name_type": mn_name_type, "entity_type_ind": entity_type_ind,
        "entity_type_text": entity_type_text, "abn_status": abn_status,
        "status_date": status_date, "state": state, "postcode": postcode,
        "record_updated": record_updated, "asic_number": asic_number,
        "asic_type": asic_type, "gst_status": gst_status, "gst_from": gst_from,
        "trading_names": " | ".join(other["TRD"]),
        "business_names": " | ".join(other["BN"]),
        "other_names": " | ".join(other["OTN"]),
        "main_other_names": " | ".join(other["MN"]),
    }


def parse_xml_file(xml_path: Path, out_path: Path) -> int:
    print(f"  Parsing: {xml_path.name}  ({xml_path.stat().st_size / (1024**2):.0f} MB)")
    count = 0
    error_count = 0
    with open(out_path, "w", encoding="utf-8", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=PARSED_COLUMNS)
        writer.writeheader()
        try:
            context = ET.iterparse(str(xml_path), events=("end",))
            _, root = next(context)
            for event, elem in context:
                if event != "end" or elem.tag != "ABR":
                    continue
                try:
                    writer.writerow(extract_record(elem))
                    count += 1
                except Exception:
                    error_count += 1
                    if error_count <= 5:
                        print(f"    [WARN] {traceback.format_exc().strip().split(chr(10))[-1]}")
                elem.clear()
                if count % 200000 == 0:
                    root.clear()
                    print(f"    {count:,} records...")
        except ET.ParseError as e:
            print(f"  [ERROR] XML Parse Error in {xml_path.name}: {e}")
            return count
    print(f"  [OK] {count:,} records written  (errors: {error_count})")
    return count


def parse_all(raw_dir: Path = RAW_DIR, out_dir: Path = PARSED_DIR) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    xml_files = sorted(raw_dir.glob("*.xml"))
    if not xml_files:
        print(f"[parse] No XML files found in {raw_dir}")
        return {}
    print(f"[parse] Found {len(xml_files)} XML file(s) in {raw_dir}")
    start = datetime.now()
    results = {}
    total = 0
    for xml_path in xml_files:
        out_path = out_dir / f"{xml_path.stem}.csv"
        n = parse_xml_file(xml_path, out_path)
        results[xml_path.name] = n
        total += n
    elapsed = (datetime.now() - start).total_seconds()
    print(f"\n[parse] Done! {total:,} total records in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    return results


# ═══════════════════════════════════════════════════════════
# 过滤 & 名称处理
# ═══════════════════════════════════════════════════════════

def should_keep(row: dict) -> tuple[bool, str]:
    entity_type = row.get("entity_type_ind", "").strip()
    if entity_type not in KEEP_ENTITY_TYPES:
        return False, f"entity_type={entity_type}"
    status = row.get("abn_status", "").strip()
    status_date = row.get("status_date", "").strip()
    if status == "CAN" and status_date < CANCEL_CUTOFF_DATE:
        return False, f"cancelled_before={CANCEL_CUTOFF_DATE}"
    return True, "ok"


def collect_raw_names(row: dict) -> list[str]:
    seen = set()
    names = []
    mn = row.get("mn_name_raw", "").strip()
    if mn and mn not in seen:
        seen.add(mn)
        names.append(mn)
    for field in ["trading_names", "business_names", "other_names"]:
        raw = row.get(field, "")
        if raw:
            for name in raw.split(" | "):
                name = name.strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
    return names


def filter_and_transform(parsed_dir: Path = PARSED_DIR, output_path: Path = FILTERED_FILE):
    csv_files = sorted(parsed_dir.glob("*.csv"))
    if not csv_files:
        print(f"[filter] No parsed CSV files found in {parsed_dir}")
        return None
    print(f"[filter] Processing {len(csv_files)} parsed CSV file(s)")
    start = datetime.now()

    # Pass 1: count merchant names for dedup
    print("[filter] Pass 1: counting merchant names...")
    name_counts = Counter()
    total_scanned = 0
    total_filtered = Counter()
    for cf in csv_files:
        with open(cf, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
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

    # Pass 2: write filtered.csv
    print("[filter] Pass 2: writing filtered.csv...")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(output_path, "w", encoding="utf-8", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=KB_INTERNAL_COLUMNS)
        writer.writeheader()
        for cf in csv_files:
            with open(cf, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    keep, reason = should_keep(row)
                    if not keep:
                        continue
                    mn_raw = row.get("mn_name_raw", "").strip()
                    abn = row.get("abn", "").strip()
                    merchant_name = mn_raw
                    if abn and name_counts.get(mn_raw, 0) > 1:
                        merchant_name = f"{merchant_name} [ABN {abn}]"
                    all_names = collect_raw_names(row)
                    writer.writerow({
                        "match_key": row.get("match_key", ""),
                        "merchant_name": merchant_name,
                        "keywords": " | ".join(all_names),
                        "link": "", "category": "",
                        "entity_type": row.get("entity_type_ind", ""),
                        "abn_status": row.get("abn_status", ""),
                        "status_date": row.get("status_date", ""),
                        "state": row.get("state", ""),
                        "record_updated": row.get("record_updated", ""),
                        "keyword_created_at": "", "in_kb_since": "",
                    })
                    written += 1

    elapsed = (datetime.now() - start).total_seconds()
    print(f"[filter] Done! {written:,} records written to {output_path} in {elapsed:.0f}s")
    return {"scanned": total_scanned, "written": written, "filtered": dict(total_filtered)}

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

# ═══════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════

def export_final(internal_path: Path = INTERNAL_FILE, output_path: Path = FINAL_OUTPUT):
    """从 kb_internal.csv 投影出 merchant_kb.csv（5 列），排除 GONE 记录."""
    if not internal_path.exists():
        print(f"[export] {internal_path} not found -- nothing to export")
        return
    print(f"[export] Writing {output_path} from {internal_path}...")
    count = 0
    gone_count = 0
    with open(internal_path, "r", encoding="utf-8") as fin, \
         open(output_path, "w", encoding="utf-8", newline="") as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=FINAL_OUTPUT_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        for row in reader:
            if row.get("abn_status", "") == "GONE":
                gone_count += 1
                continue
            writer.writerow(row)
            count += 1
    size_mb = output_path.stat().st_size / (1024**2)
    print(f"[export] {count:,} active records  |  {gone_count:,} GONE excluded  |  {size_mb:.0f} MB")


def main():
    parser = argparse.ArgumentParser(description="ABR XML → 商户知识库")
    parser.add_argument("--skip-parse", action="store_true",
                        help="Skip XML parsing, use existing data/parsed/*.csv")
    args = parser.parse_args()

    if not args.skip_parse:
        parse_all()
    filter_and_transform()
    incremental_merge()


if __name__ == "__main__":
    main()
