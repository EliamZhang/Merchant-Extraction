#!/usr/bin/env python3
"""Extract empty-category merchants from merchant_kb.csv and split into 10 JSON files."""

import csv
import json
import math
import os
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "merchant_kb.csv")
OUT_DIR = os.path.join(BASE_DIR, "knowledge-base-split")
NUM_PARTS = 20


def main():
    if not os.path.exists(CSV_PATH):
        print(f"Error: {CSV_PATH} not found")
        return

    # Clear output dir
    if os.path.exists(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR)

    # Extract empty-category merchants
    empty = []
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not row.get("category", "").strip():
                empty.append({
                    "merchant_name": row["merchant_name"],
                    "category": "",
                })

    total = len(empty)
    chunk = math.ceil(total / NUM_PARTS)
    print(f"Empty-category merchants: {total}")

    for i in range(NUM_PARTS):
        start = i * chunk
        end = min((i + 1) * chunk, total)
        part = empty[start:end]
        fname = f"merchant_kb_part_{(i + 1):02d}.json"
        fpath = os.path.join(OUT_DIR, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(part, f, ensure_ascii=False)
        print(f"  {fname}: {len(part)} records")

    print("Done.")


if __name__ == "__main__":
    main()
