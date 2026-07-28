"""
Category Classification Script
==============================
Rule-based merchant category classification using keyword matching.
Reads kb_internal.csv, assigns categories, and writes back.

Usage:
  python scripts/categorize.py                     # classify all records
  python scripts/categorize.py --input data/my.csv # classify a specific file
"""

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import INTERNAL_FILE, KB_INTERNAL_COLUMNS
from category_rules import RULES


def classify_merchant(name, keywords, link, original_category):
    """
    Score all categories based on keyword matches.
    Return highest scoring category, with tie-breaking and safe fallback.
    """
    combined = f"{name} {keywords} {link}".lower()

    scores = Counter()
    for category, keyword_list in RULES:
        for kw in keyword_list:
            if kw in combined:
                scores[category] += 1

    if not scores:
        if original_category:
            return original_category
        return 'Information'

    top = scores.most_common()
    best_cat, best_score = top[0]

    if not original_category:
        return best_cat

    if best_cat == original_category:
        return original_category

    original_score = scores.get(original_category, 0)

    if original_score == 0 and best_score > 0:
        return best_cat

    if best_score >= original_score + 2:
        return best_cat

    return original_category


def process_categories(internal_path: Path = INTERNAL_FILE):
    """Classify all records in kb_internal.csv, updating in place."""
    internal_path = Path(internal_path)
    if not internal_path.exists():
        print(f"[categorize] {internal_path} not found")
        return None

    print(f"[categorize] Loading {internal_path}...")
    with open(internal_path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"[categorize] Loaded {len(rows):,} rows  |  {len(RULES)} category rule groups")

    stats = Counter()
    empty_filled = 0
    category_changed = 0
    unchanged = 0

    for row in rows:
        name = row.get("merchant_name", "")
        keywords = row.get("keywords", "")
        link = row.get("link", "")
        original = row.get("category", "").strip()
        new_category = classify_merchant(name, keywords, link, original)

        if not original and new_category:
            empty_filled += 1
        elif original and original != new_category:
            category_changed += 1
        else:
            unchanged += 1

        row["category"] = new_category

    print(f"[categorize] Writing back to {internal_path}...")
    with open(internal_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=KB_INTERNAL_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    print(f"\n[categorize] {'='*50}")
    print(f"  Total:              {total:>10,}")
    print(f"  Unchanged:          {unchanged:>10,}")
    print(f"  Empty filled:       {empty_filled:>10,}")
    print(f"  Category changed:   {category_changed:>10,}")

    cat_counts = Counter(row["category"] for row in rows)
    print(f"\n[categorize] Category distribution:")
    for cat, cnt in cat_counts.most_common():
        bar = "#" * max(1, cnt * 30 // max(total, 1))
        print(f"  {cat:<30} {cnt:>8,}  {bar}")

    return {
        "total": total,
        "unchanged": unchanged,
        "empty_filled": empty_filled,
        "category_changed": category_changed,
    }


def main():
    parser = argparse.ArgumentParser(description="Rule-based merchant category classification")
    parser.add_argument("--input", type=Path, default=INTERNAL_FILE,
                        help="Path to kb_internal.csv (default from config)")
    args = parser.parse_args()
    process_categories(args.input)


if __name__ == "__main__":
    main()
