"""
build_knowledge_base - ABR XML -> merchant_kb.csv
=================================================

Read ABR XML files from xml_input/, extract company entities, and merge them
directly into merchant_kb.csv:

  - existing merchants are not duplicated;
  - new ABR names are appended;
  - keywords found in ABR are added to existing rows when missing.

No parsed, filtered, internal, or changelog CSV files are created.
"""

import argparse
import csv
import os
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    CANCEL_CUTOFF_DATE,
    FINAL_OUTPUT,
    FINAL_OUTPUT_COLUMNS,
    KEEP_ENTITY_TYPES,
    RAW_DIR,
)


AMBIGUOUS_OWNER = "\0"
KEYWORD_SEPARATOR = " | "
TARGET_COLUMNS = FINAL_OUTPUT_COLUMNS


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def get_text(elem: ET.Element, tag: str, default: str = "") -> str:
    child = elem.find(tag)
    return child.text.strip() if child is not None and child.text else default


def normalize_lookup(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def split_keywords(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split("|") if part.strip()]


def unique_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        clean = " ".join((name or "").split())
        key = normalize_lookup(clean)
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def add_keyword(bucket: OrderedDict[str, str], value: str) -> None:
    clean = " ".join((value or "").split())
    key = normalize_lookup(clean)
    if clean and key not in bucket:
        bucket[key] = clean


def add_owner(index: dict[str, str], lookup_key: str, owner: str) -> None:
    if not lookup_key:
        return
    existing = index.get(lookup_key)
    if existing is None:
        index[lookup_key] = owner
    elif existing != owner:
        index[lookup_key] = AMBIGUOUS_OWNER


def load_existing_index(target_path: Path) -> tuple[set[str], dict[str, str], int]:
    """Index existing merchant names and unique keywords for duplicate detection."""
    merchant_names: set[str] = set()
    keyword_owner: dict[str, str] = {}
    row_count = 0

    if not target_path.exists():
        return merchant_names, keyword_owner, row_count

    with target_path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            row_count += 1
            merchant_name = row.get("merchant_name", "")
            owner = normalize_lookup(merchant_name)
            if not owner:
                continue

            merchant_names.add(owner)
            add_owner(keyword_owner, owner, owner)
            for keyword in split_keywords(row.get("keywords", "")):
                add_owner(keyword_owner, normalize_lookup(keyword), owner)

    return merchant_names, keyword_owner, row_count


def other_entity_names(abr: ET.Element) -> list[str]:
    names: list[str] = []
    for other_entity in abr.findall("OtherEntity"):
        name = get_text(other_entity, "NonIndividualName/NonIndividualNameText")
        if name:
            names.append(name)
    return names


def extract_entity(abr: ET.Element) -> dict[str, str | list[str]]:
    abn_elem = abr.find("ABN")
    abn_status = abn_elem.get("status", "") if abn_elem is not None else ""
    status_date = abn_elem.get("ABNStatusFromDate", "") if abn_elem is not None else ""

    merchant_name = get_text(abr, "MainEntity/NonIndividualName/NonIndividualNameText")
    if not merchant_name:
        legal_entity = abr.find("LegalEntity/IndividualName")
        if legal_entity is not None:
            given_names = [
                given.text.strip()
                for given in legal_entity.findall("GivenName")
                if given is not None and given.text
            ]
            family_name = get_text(legal_entity, "FamilyName")
            merchant_name = " ".join(given_names + ([family_name] if family_name else [])).strip()

    names = unique_names([merchant_name, *other_entity_names(abr)])
    return {
        "merchant_name": merchant_name,
        "keywords": names,
        "entity_type": get_text(abr, "EntityType/EntityTypeInd"),
        "abn_status": abn_status,
        "status_date": status_date,
    }


def should_keep(entity: dict[str, str | list[str]]) -> tuple[bool, str]:
    entity_type = str(entity.get("entity_type", "")).strip()
    if entity_type not in KEEP_ENTITY_TYPES:
        return False, f"entity_type={entity_type or 'blank'}"

    status = str(entity.get("abn_status", "")).strip()
    status_date = str(entity.get("status_date", "")).strip()
    if status == "CAN" and status_date < CANCEL_CUTOFF_DATE:
        return False, f"cancelled_before={CANCEL_CUTOFF_DATE}"

    if not normalize_lookup(str(entity.get("merchant_name", ""))):
        return False, "missing_name"

    return True, "ok"


def find_existing_owner(
    entity: dict[str, str | list[str]],
    existing_names: set[str],
    keyword_owner: dict[str, str],
) -> str:
    merchant_key = normalize_lookup(str(entity.get("merchant_name", "")))
    if merchant_key in existing_names:
        return merchant_key

    for keyword in entity.get("keywords", []):
        owner = keyword_owner.get(normalize_lookup(str(keyword)))
        if owner and owner != AMBIGUOUS_OWNER:
            return owner

    return ""


def _progress_line(stats: Counter, suffix: str = "") -> str:
    scanned = stats.get("xml_records", 0)
    kept = stats.get("kept_entities", 0)
    matched = stats.get("matched_existing_entities", 0)
    new = stats.get("new_source_entities", 0)
    errors = stats.get("errors", 0) + stats.get("parse_errors", 0)
    parts = [f"scanned={scanned:,}", f"kept={kept:,}", f"matched={matched:,}", f"new={new:,}"]
    if errors:
        parts.append(f"errors={errors:,}")
    if suffix:
        parts.append(suffix)
    return "  " + "  ".join(parts)


def collect_xml_entities(
    raw_dir: Path,
    existing_names: set[str],
    keyword_owner: dict[str, str],
) -> tuple[dict[str, OrderedDict[str, str]], OrderedDict[str, dict[str, object]], Counter]:
    additions_by_owner: dict[str, OrderedDict[str, str]] = {}
    new_entities: OrderedDict[str, dict[str, object]] = OrderedDict()
    stats: Counter = Counter()

    xml_files = sorted(raw_dir.glob("*.xml"))
    if not xml_files:
        print(f"[xml] No XML files found in {raw_dir}")
        return additions_by_owner, new_entities, stats

    print(f"[xml] {len(xml_files)} file(s) to scan")
    for xml_path in xml_files:
        print(f"  {xml_path.name}")
        try:
            context = ET.iterparse(str(xml_path), events=("start", "end"))
            _, root = next(context)
            for event, elem in context:
                if event != "end" or elem.tag != "ABR":
                    continue

                stats["xml_records"] += 1
                try:
                    entity = extract_entity(elem)
                    keep, reason = should_keep(entity)
                    if not keep:
                        stats[f"filtered:{reason}"] += 1
                    else:
                        stats["kept_entities"] += 1
                        owner = find_existing_owner(entity, existing_names, keyword_owner)
                        keywords = entity["keywords"]
                        if owner:
                            bucket = additions_by_owner.setdefault(owner, OrderedDict())
                            for keyword in keywords:
                                add_keyword(bucket, str(keyword))
                            stats["matched_existing_entities"] += 1
                        else:
                            merchant_name = str(entity["merchant_name"])
                            merchant_key = normalize_lookup(merchant_name)
                            entry = new_entities.setdefault(
                                merchant_key,
                                {"merchant_name": merchant_name, "keywords": OrderedDict()},
                            )
                            for keyword in keywords:
                                add_keyword(entry["keywords"], str(keyword))
                            stats["new_source_entities"] += 1
                except Exception:
                    stats["errors"] += 1
                    if stats["errors"] <= 5:
                        last_line = traceback.format_exc().strip().splitlines()[-1]
                        print(f"    [WARN] {last_line}")

                elem.clear()
                if stats["xml_records"] % 200000 == 0:
                    root.clear()
                    print(_progress_line(stats, f"file {xml_path.name}"))

        except ET.ParseError as exc:
            stats["parse_errors"] += 1
            print(f"    [ERR] Parse error: {exc}")
        print(_progress_line(stats, f"done {xml_path.name}"))

    return additions_by_owner, new_entities, stats


def merge_keywords(existing: str, additions: OrderedDict[str, str]) -> tuple[str, bool]:
    merged = OrderedDict()
    for keyword in split_keywords(existing):
        add_keyword(merged, keyword)
    before = set(merged.keys())
    for keyword in additions.values():
        add_keyword(merged, keyword)
    return KEYWORD_SEPARATOR.join(merged.values()), set(merged.keys()) != before


def write_merged_kb(
    target_path: Path,
    additions_by_owner: dict[str, OrderedDict[str, str]],
    new_entities: OrderedDict[str, dict[str, object]],
    timestamp: str,
) -> Counter:
    stats: Counter = Counter()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    matched_owners: set[str] = set()

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            delete=False,
            dir=target_path.parent,
        ) as temporary:
            temporary_path = Path(temporary.name)
            writer = csv.DictWriter(temporary, fieldnames=TARGET_COLUMNS, extrasaction="ignore")
            writer.writeheader()

            if target_path.exists():
                with target_path.open("r", encoding="utf-8-sig", newline="") as source:
                    for row in csv.DictReader(source):
                        owner = normalize_lookup(row.get("merchant_name", ""))
                        additions = additions_by_owner.get(owner)
                        if additions:
                            row["keywords"], changed = merge_keywords(row.get("keywords", ""), additions)
                            matched_owners.add(owner)
                            if changed:
                                row["keyword_created_at"] = timestamp
                                stats["updated_existing_rows"] += 1
                            else:
                                stats["existing_rows_already_current"] += 1
                        writer.writerow({column: row.get(column, "") for column in TARGET_COLUMNS})
                        stats["existing_rows_written"] += 1

            for merchant_key, entry in new_entities.items():
                if merchant_key in matched_owners:
                    continue
                keywords = entry["keywords"]
                writer.writerow({
                    "merchant_name": entry["merchant_name"],
                    "keywords": KEYWORD_SEPARATOR.join(keywords.values()),
                    "link": "",
                    "category": "",
                    "keyword_created_at": timestamp,
                })
                stats["inserted_new_rows"] += 1

        os.replace(temporary_path, target_path)
        return stats
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def build_knowledge_base(raw_dir: Path = RAW_DIR, target_path: Path = FINAL_OUTPUT) -> Counter:
    timestamp = now_utc()

    print(f"[kb] Target: {target_path}")
    existing_names, keyword_owner, existing_count = load_existing_index(target_path)
    print(f"[kb] Existing rows: {existing_count:,}")

    additions_by_owner, new_entities, stats = collect_xml_entities(
        raw_dir=raw_dir,
        existing_names=existing_names,
        keyword_owner=keyword_owner,
    )
    if stats.get("xml_records", 0) == 0:
        print("[kb] No XML records processed")
        return stats

    write_stats = write_merged_kb(target_path, additions_by_owner, new_entities, timestamp)
    stats.update(write_stats)

    total_filtered = sum(v for k, v in stats.items() if k.startswith("filtered:"))
    parse_errors = stats.get("parse_errors", 0) + stats.get("errors", 0)

    print(f"\n[kb] Summary")
    print(f"  XML records:         {stats.get('xml_records', 0):>10,}")
    print(f"  Kept / filtered:     {stats.get('kept_entities', 0):>10,}  / {total_filtered:,}")
    print(f"  ── matched existing: {stats.get('matched_existing_entities', 0):>10,}")
    print(f"  ── new entities:     {stats.get('new_source_entities', 0):>10,}")
    print(f"  Knowledge base:")
    print(f"  ── rows updated:     {stats.get('updated_existing_rows', 0):>10,}")
    print(f"  ── rows inserted:    {stats.get('inserted_new_rows', 0):>10,}")
    if parse_errors:
        print(f"  Errors:              {parse_errors:>10,}")
    if total_filtered:
        print(f"  Filtered by:")
        for key in sorted(stats):
            if key.startswith("filtered:"):
                print(f"    {key.removeprefix('filtered:'):<24} {stats[key]:>10,}")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge ABR XML company entities directly into merchant_kb.csv"
    )
    parser.add_argument(
        "--xml-dir",
        type=Path,
        default=RAW_DIR,
        help="Directory containing ABR XML files (default: xml_input)",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=FINAL_OUTPUT,
        help="Knowledge base CSV to update (default: merchant_kb.csv)",
    )
    args = parser.parse_args()

    build_knowledge_base(raw_dir=args.xml_dir, target_path=args.target)


if __name__ == "__main__":
    main()
