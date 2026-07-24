"""
Business bd -- 商家知识库增量更新 Pipeline
=========================================
一键执行全流程：解析 -> 过滤 -> 增量合并 -> 关键词清洗 -> 分类标注 -> 导出

用法:
  python pipeline.py                  # 增量模式
  python pipeline.py --full-clean     # 全量关键词清洗
  python pipeline.py --skip-parse     # 跳过 XML 解析
  python pipeline.py --step filter    # 只运行指定阶段
  python pipeline.py --dry-run        # 只统计，不写入
"""

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

# 确保项目根目录在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    RAW_DIR, DATA_DIR, PARSED_DIR,
    FILTERED_FILE, INTERNAL_FILE, CHANGELOG_FILE,
    FINAL_OUTPUT, FINAL_OUTPUT_COLUMNS,
)


def ensure_directories():
    """创建必要的目录."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PARSED_DIR.mkdir(parents=True, exist_ok=True)


def export_final_csv(internal_path: Path = INTERNAL_FILE, output_path: Path = FINAL_OUTPUT):
    """从 kb_internal.csv 投影出最终的 merchant_kb.csv（5 列），排除 GONE 记录."""
    if not internal_path.exists():
        print(f"[export] {internal_path} not found -- nothing to export")
        return

    print(f"[export] Projecting {FINAL_OUTPUT} from {internal_path}...")

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
    print(f"[export] Done! {count:,} active records  |  {gone_count:,} GONE excluded  |  {size_mb:.0f} MB")


def run_pipeline(args: argparse.Namespace) -> int:
    """按顺序执行全流程."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    start_all = datetime.now()

    print("=" * 60)
    print(f"Business bd Pipeline -- {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("=" * 60)

    # -- Stage 1: Parse --
    if not args.skip_parse:
        print("\n" + "-" * 40)
        print("Stage 1/5: Parse XML -> CSV")
        print("-" * 40)
        if not args.dry_run:
            from stages.parse import parse_all
            xml_files = sorted(RAW_DIR.glob("*.xml"))
            if not xml_files:
                print("[pipeline] No XML files found in raw/ -- aborting")
                return 1
            parse_all()
        else:
            print("[dry-run] Would parse XML files from raw/")
    else:
        print("\n[Stage 1] SKIPPED (--skip-parse)")

    if args.step == "parse":
        print("\n[pipeline] Stopped after 'parse' stage.")
        return 0

    # -- Stage 2: Filter --
    print("\n" + "-" * 40)
    print("Stage 2/5: Filter (PRV/PUB only + date cutoff)")
    print("-" * 40)
    if not args.dry_run:
        from stages.filter import filter_and_transform
        filter_and_transform()
    else:
        print("[dry-run] Would filter parsed CSVs -> filtered.csv")

    if args.step == "filter":
        print("\n[pipeline] Stopped after 'filter' stage.")
        return 0

    # -- Stage 3: Merge --
    print("\n" + "-" * 40)
    print("Stage 3/5: Incremental Merge")
    print("-" * 40)
    if not args.dry_run:
        from stages.merge_update import incremental_merge
        incremental_merge(now=now)
    else:
        print("[dry-run] Would merge filtered.csv with existing kb_internal.csv")

    if args.step == "merge":
        print("\n[pipeline] Stopped after 'merge' stage.")
        return 0

    # -- Stage 4: Clean Keywords --
    print("\n" + "-" * 40)
    clean_mode = "FULL" if args.full_clean else "INCREMENTAL"
    print(f"Stage 4/5: Clean Keywords ({clean_mode})")
    print("-" * 40)
    if not args.dry_run:
        from stages.clean_keywords import process_keywords
        process_keywords(full_clean=args.full_clean, now=now)
    else:
        print(f"[dry-run] Would clean keywords ({clean_mode})")

    if args.step == "clean":
        print("\n[pipeline] Stopped after 'clean' stage.")
        return 0

    # -- Stage 5: Categorize --
    print("\n" + "-" * 40)
    print("Stage 5/5: Categorize")
    print("-" * 40)
    if not args.dry_run:
        from stages.categorize import process_categories
        process_categories()
    else:
        print("[dry-run] Would categorize records")

    if args.step == "categorize":
        print("\n[pipeline] Stopped after 'categorize' stage.")
        return 0

    # -- Export Final CSV --
    print("\n" + "-" * 40)
    print("Export: kb_internal.csv -> merchant_kb.csv (5 columns)")
    print("-" * 40)
    if not args.dry_run:
        export_final_csv()
    else:
        print("[dry-run] Would export final merchant_kb.csv")

    # -- Done --
    elapsed = (datetime.now() - start_all).total_seconds()
    print(f"\n{'=' * 60}")
    print(f"Pipeline complete! Total time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"{'=' * 60}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Business bd -- 商家知识库增量更新 Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline.py                    # 增量更新
  python pipeline.py --full-clean       # 全量关键词清洗
  python pipeline.py --skip-parse       # 跳过 XML 解析（使用已有 parsed CSV）
  python pipeline.py --step filter      # 只运行 filter 阶段
  python pipeline.py --dry-run          # 预览模式
        """,
    )
    parser.add_argument("--full-clean", action="store_true",
                        help="全量关键词清洗（默认只清洗增量）")
    parser.add_argument("--skip-parse", action="store_true",
                        help="跳过 Stage 1 XML 解析（使用已有 data/parsed/*.csv）")
    parser.add_argument("--step", choices=["parse", "filter", "merge", "clean", "categorize"],
                        help="只运行指定阶段后停止")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要执行的操作，不实际写入")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    ensure_directories()
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
