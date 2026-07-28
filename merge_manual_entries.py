"""Merge manually supplied merchant CSV files into the final knowledge base.

Usage:
  python merge_manual_entries.py --add-dir manual_entries/ --target merchant_kb.csv
"""

import argparse
import csv
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ADD_DIR, FINAL_OUTPUT


TARGET_COLUMNS = [
    "merchant_name",
    "keywords",
    "link",
    "category",
    "keyword_updated_at",
    "category_updated_at",
]
SUPPLEMENT_COLUMNS = [
    "keywords",
    "link",
    "category",
    "keyword_updated_at",
    "category_updated_at",
]


def normalize_merchant_name(value: str) -> str:
    """Normalize a merchant name for case- and whitespace-insensitive matching."""
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def load_add_rows(add_dir: Path) -> tuple[list[dict[str, str]], int]:
    """Load direct-child CSV files from an add directory in filename order."""
    if not add_dir.exists():
        return [], 0

    paths = sorted(add_dir.glob("*.csv"))
    rows: list[dict[str, str]] = []
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            rows.extend(csv.DictReader(source))
    return rows, len(paths)


def _source_value(row: dict[str, str], column: str) -> str:
    if column == "keyword_updated_at":
        return (row.get("keyword_updated_at") or row.get("keyword_created_at") or "").strip()
    return (row.get(column) or "").strip()


def _target_row(source_row: dict[str, str]) -> dict[str, str]:
    return {
        "merchant_name": (source_row.get("merchant_name") or "").strip(),
        **{
            column: _source_value(source_row, column)
            for column in SUPPLEMENT_COLUMNS
        },
    }


def _matching_target_names(target_path: Path, add_by_name: dict[str, dict[str, str]]) -> set[str]:
    if not target_path.exists():
        return set()

    matched_names: set[str] = set()
    with target_path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            name = normalize_merchant_name(row.get("merchant_name", ""))
            if name in add_by_name:
                matched_names.add(name)
    return matched_names


def merge_add_files(add_dir: Path, target_path: Path) -> dict[str, int]:
    """Supplement target records and prepend merchants absent from the target."""
    add_rows, files = load_add_rows(add_dir)
    stats = {
        "files": files,
        "source_rows": len(add_rows),
        "updated_rows": 0,
        "inserted_rows": 0,
    }
    if not add_rows:
        return stats

    add_by_name: dict[str, dict[str, str]] = {}
    ordered_names: list[str] = []
    for row in add_rows:
        name = normalize_merchant_name(row.get("merchant_name", ""))
        if name and name not in add_by_name:
            add_by_name[name] = row
            ordered_names.append(name)

    matched_names = _matching_target_names(target_path, add_by_name)
    new_rows = [_target_row(add_by_name[name]) for name in ordered_names if name not in matched_names]
    stats["inserted_rows"] = len(new_rows)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            delete=False,
            dir=target_path.parent,
        ) as temporary:
            temporary_path = Path(temporary.name)
            writer = csv.DictWriter(temporary, fieldnames=TARGET_COLUMNS)
            writer.writeheader()
            writer.writerows(new_rows)

            if target_path.exists():
                with target_path.open("r", encoding="utf-8-sig", newline="") as source:
                    for row in csv.DictReader(source):
                        add_row = add_by_name.get(normalize_merchant_name(row.get("merchant_name", "")))
                        if add_row:
                            changed = False
                            for column in SUPPLEMENT_COLUMNS:
                                if not (row.get(column) or "").strip():
                                    value = _source_value(add_row, column)
                                    if value:
                                        row[column] = value
                                        changed = True
                            if changed:
                                stats["updated_rows"] += 1
                        writer.writerow({column: row.get(column, "") for column in TARGET_COLUMNS})

        os.replace(temporary_path, target_path)
        return stats
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Merge manually supplied merchant CSV files into the knowledge base"
    )
    parser.add_argument("--add-dir", type=Path, default=ADD_DIR,
                        help="Directory containing source CSV files (default from config)")
    parser.add_argument("--target", type=Path, default=FINAL_OUTPUT,
                        help="Target merchant_kb.csv to merge into (default from config)")
    args = parser.parse_args()

    stats = merge_add_files(args.add_dir, args.target)
    print(
        f"[merge-add] "
        f"Files: {stats['files']:,}  |  "
        f"Source rows: {stats['source_rows']:,}  |  "
        f"Updated: {stats['updated_rows']:,}  |  "
        f"Inserted: {stats['inserted_rows']:,}"
    )


if __name__ == "__main__":
    main()
