#!/usr/bin/env python3
"""
Helper to extract and merge merchant classification batches.

Usage:
    # Extract next N unclassified merchants from a split file
    python classify_batch.py extract <split_file.json> --count 50

    # Merge classified results back
    python classify_batch.py merge <split_file.json> <classified_batch.json>

    # Show progress for a split file
    python classify_batch.py status <split_file.json>
"""

import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SPLIT_DIR = BASE_DIR / "knowledge-base-split"
CLASSIFY_DIR = BASE_DIR / "knowledge-base-web-classify"
BATCH_DIR = CLASSIFY_DIR / "batches"
CLASSIFY_DIR.mkdir(parents=True, exist_ok=True)
BATCH_DIR.mkdir(parents=True, exist_ok=True)


def status(split_file: str) -> None:
    """Show classification progress for a split file."""
    path = Path(split_file)
    if not path.exists():
        print(f"File not found: {path}")
        return

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    total = len(data)
    classified = sum(1 for item in data if item.get("category", "").strip())
    processed_empty = sum(
        1 for item in data
        if not item.get("category", "").strip() and item.get("_processed")
    )
    remaining = total - classified - processed_empty
    print(f"File: {path.name}")
    print(f"  Total: {total}")
    print(f"  Classified: {classified} ({100*classified/total:.1f}%)")
    print(f"  Processed (empty): {processed_empty}")
    print(f"  Remaining: {remaining} ({100*remaining/total:.1f}%)")

    # Check for in-progress batches
    prefix = path.stem.replace(".json", "") + "_batch_"
    batches = sorted(BATCH_DIR.glob(f"{prefix}*.json"))
    if batches:
        print(f"  Pending batches: {len(batches)}")


def extract(split_file: str, count: int = 50) -> str:
    """Extract the next N unclassified and unprocessed merchants and write a batch file."""
    path = Path(split_file)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Find unclassified AND unprocessed indices
    unclassified = []
    for i, item in enumerate(data):
        if not item.get("category", "").strip() and not item.get("_processed"):
            unclassified.append((i, item))

    if not unclassified:
        print("All merchants are already classified or processed!")
        return ""

    batch = unclassified[:count]
    batch_data = [
        {"index": idx, "merchant_name": item["merchant_name"], "category": ""}
        for idx, item in batch
    ]

    # Find next batch number
    prefix = path.stem.replace(".json", "")
    existing_batches = sorted(BATCH_DIR.glob(f"{prefix}_batch_*.json"))
    next_num = len(existing_batches) + 1
    batch_name = f"{prefix}_batch_{next_num:03d}.json"
    batch_path = BATCH_DIR / batch_name

    output = {
        "source_file": str(path.name),
        "batch_name": batch_name,
        "total_in_source": len(data),
        "total_unclassified": len(unclassified),
        "batch_start": batch[0][0] if batch else 0,
        "items": batch_data,
    }

    with open(batch_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Extracted {len(batch_data)} merchants to: {batch_path}")
    print(f"Remaining in source: {len(unclassified) - len(batch_data)}")
    return str(batch_path)


def merge(split_file: str, classified_batch: str) -> None:
    """Merge classified batch results back into the split file."""
    split_path = Path(split_file)
    batch_path = Path(classified_batch)

    if not split_path.exists():
        print(f"Split file not found: {split_path}", file=sys.stderr)
        sys.exit(1)
    if not batch_path.exists():
        print(f"Batch file not found: {batch_path}", file=sys.stderr)
        sys.exit(1)

    with open(split_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    with open(batch_path, "r", encoding="utf-8") as f:
        batch = json.load(f)

    items = batch.get("items", batch) if isinstance(batch, dict) else batch
    if isinstance(items, dict):
        items = [items]

    updated = 0
    empty_count = 0
    for item in items:
        idx = item.get("index", -1)
        category = item.get("category", "").strip()
        if 0 <= idx < len(data):
            data[idx]["_processed"] = True
            if category:
                data[idx]["category"] = category
                updated += 1
            else:
                empty_count += 1

    # Write updated data back
    with open(split_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    # Also write/update the cumulative classified output file
    cumulative_name = split_path.stem + "_classified.json"
    cumulative_path = CLASSIFY_DIR / cumulative_name
    with open(cumulative_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    # Remove the batch file after successful merge
    batch_path.unlink()

    total = len(data)
    classified_total = sum(1 for item in data if item.get("category", "").strip())
    processed_total = sum(1 for item in data if item.get("_processed"))
    print(f"Merged {updated} classified + {empty_count} empty into: {split_path.name}")
    print(f"Also wrote cumulative: {cumulative_path.name}")
    print(f"Progress: {classified_total} classified, {processed_total} processed / {total} ({100*processed_total/total:.1f}%)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        target = sys.argv[2] if len(sys.argv) > 2 else None
        if target:
            status(target)
        else:
            for f in sorted(SPLIT_DIR.glob("*.json")):
                status(str(f))
                print()
    elif cmd == "extract":
        if len(sys.argv) < 3:
            print("Usage: python classify_batch.py extract <split_file.json> [--count N]", file=sys.stderr)
            sys.exit(1)
        count = 50
        args = sys.argv[2:]
        if "--count" in args:
            idx = args.index("--count")
            count = int(args[idx + 1])
        extract(sys.argv[2], count)
    elif cmd == "merge":
        if len(sys.argv) < 4:
            print("Usage: python classify_batch.py merge <split_file.json> <classified_batch.json>", file=sys.stderr)
            sys.exit(1)
        merge(sys.argv[2], sys.argv[3])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
