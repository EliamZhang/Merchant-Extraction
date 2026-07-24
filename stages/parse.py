"""
Stage 1: XML -> CSV 解析
=======================
逐条解析 raw/*.xml 中的 ABR 记录，提取全部字段，不做任何过滤。
输出: data/parsed/{filename}.csv
"""

import csv
import hashlib
import sys
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# 将项目根目录加入 sys.path（支持直接运行此文件）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import RAW_DIR, PARSED_DIR, MATCH_KEY_LENGTH


def get_text(elem, tag, default=""):
    """安全获取子元素文本."""
    child = elem.find(tag)
    return child.text if child is not None and child.text else default


def get_attrib(elem, tag, attr, default=""):
    """安全获取子元素属性."""
    child = elem.find(tag)
    if child is not None:
        return child.get(attr, default)
    return default


def normalize_mn_name(name: str) -> str:
    """规范化法定主体名称：大写 + 合并空格."""
    return " ".join(name.upper().split())


def compute_match_key(mn_name_text: str) -> str:
    """基于规范化法定主体名称生成匹配键."""
    if not mn_name_text or not mn_name_text.strip():
        return ""
    normalized = normalize_mn_name(mn_name_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:MATCH_KEY_LENGTH]


# parse.py 输出的 CSV 列
PARSED_COLUMNS = [
    "match_key",
    "abn",
    "mn_name_raw",           # MainEntity NonIndividualName 原始文本
    "mn_name_type",          # MN / LGL 等
    "entity_type_ind",       # PRV / PUB / IND / ...
    "entity_type_text",      # Australian Private Company / ...
    "abn_status",            # ACT / CAN
    "status_date",           # ABNStatusFromDate
    "state",
    "postcode",
    "record_updated",        # recordLastUpdatedDate
    "asic_number",
    "asic_type",
    "gst_status",
    "gst_from",
    "trading_names",         # TRD 类型 OtherEntity, " | " 分隔
    "business_names",        # BN 类型
    "other_names",           # OTN 类型
    "main_other_names",      # MN 类型 OtherEntity（非主名）
]


def extract_other_entities(abr):
    """提取所有 OtherEntity 的非个体名称，按类型分组."""
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
    """从单个 <ABR> 元素提取全部字段，返回平面字典."""
    # -- ABN --
    abn_elem = abr.find("ABN")
    if abn_elem is None:
        abn = ""
        abn_status = ""
        status_date = ""
    else:
        abn = abn_elem.text or ""
        abn_status = abn_elem.get("status", "")
        status_date = abn_elem.get("ABNStatusFromDate", "")

    # -- EntityType --
    entity_type_ind = get_text(abr, "EntityType/EntityTypeInd")
    entity_type_text = get_text(abr, "EntityType/EntityTypeText")

    # -- MainEntity --
    mn_name_type = get_attrib(abr, "MainEntity/NonIndividualName", "type")
    mn_name_raw = get_text(abr, "MainEntity/NonIndividualName/NonIndividualNameText")
    state = get_text(abr, "MainEntity/BusinessAddress/AddressDetails/State")
    postcode = get_text(abr, "MainEntity/BusinessAddress/AddressDetails/Postcode")

    # -- 法定个体（个人用） --
    if not mn_name_raw:
        le = abr.find("LegalEntity/IndividualName")
        if le is not None:
            givens = [gn.text for gn in le.findall("GivenName") if gn is not None and gn.text]
            family = get_text(le, "FamilyName")
            mn_name_raw = " ".join(givens + [family]).strip()
            mn_name_type = le.get("type", "LGL")
            state = state or get_text(abr, "LegalEntity/BusinessAddress/AddressDetails/State")
            postcode = postcode or get_text(abr, "LegalEntity/BusinessAddress/AddressDetails/Postcode")

    # -- Match Key --
    match_key = compute_match_key(mn_name_raw)

    # -- ASIC --
    asic_elem = abr.find("ASICNumber")
    if asic_elem is not None:
        asic_number = asic_elem.text or ""
        asic_type = asic_elem.get("ASICNumberType", "")
    else:
        asic_number = ""
        asic_type = ""

    # -- GST --
    gst_elem = abr.find("GST")
    if gst_elem is not None:
        gst_status = gst_elem.get("status", "")
        gst_from = gst_elem.get("GSTStatusFromDate", "")
    else:
        gst_status = ""
        gst_from = ""

    # -- recordLastUpdatedDate --
    record_updated = abr.get("recordLastUpdatedDate", "")

    # -- OtherEntity --
    other = extract_other_entities(abr)

    return {
        "match_key": match_key,
        "abn": abn,
        "mn_name_raw": mn_name_raw,
        "mn_name_type": mn_name_type,
        "entity_type_ind": entity_type_ind,
        "entity_type_text": entity_type_text,
        "abn_status": abn_status,
        "status_date": status_date,
        "state": state,
        "postcode": postcode,
        "record_updated": record_updated,
        "asic_number": asic_number,
        "asic_type": asic_type,
        "gst_status": gst_status,
        "gst_from": gst_from,
        "trading_names": " | ".join(other["TRD"]),
        "business_names": " | ".join(other["BN"]),
        "other_names": " | ".join(other["OTN"]),
        "main_other_names": " | ".join(other["MN"]),
    }


def parse_xml_file(xml_path: Path, out_path: Path) -> int:
    """解析单个 XML 文件，写入 CSV。返回记录数。"""
    print(f"  Parsing: {xml_path.name}  ({xml_path.stat().st_size / (1024**2):.0f} MB)")

    count = 0
    error_count = 0

    with open(out_path, "w", encoding="utf-8", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=PARSED_COLUMNS)
        writer.writeheader()

        try:
            # 使用 iterparse 增量解析（内存友好）
            context = ET.iterparse(str(xml_path), events=("end",))
            _, root = next(context)  # root element

            for event, elem in context:
                if event != "end" or elem.tag != "ABR":
                    continue

                try:
                    row = extract_record(elem)
                    writer.writerow(row)
                    count += 1
                except Exception:
                    error_count += 1
                    if error_count <= 5:
                        print(f"    [WARN] Record parse error (showing first 5): {traceback.format_exc().strip().split(chr(10))[-1]}")

                # 释放内存
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
    """解析 raw/ 下所有 XML 文件。返回 {filename: record_count}。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    xml_files = sorted(raw_dir.glob("*.xml"))
    if not xml_files:
        print(f"[parse] No XML files found in {raw_dir}")
        return {}

    print(f"[parse] Found {len(xml_files)} XML file(s) in {raw_dir}")
    print(f"[parse] Output: {out_dir}/")
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


# --- 独立运行 ---
if __name__ == "__main__":
    parse_all()
