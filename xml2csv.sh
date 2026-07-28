#!/usr/bin/env bash
# ============================================================
# xml2csv — Workflow A: ABR XML → 商户知识库
# ============================================================
# 依次执行：解析 → 过滤 → 合并 → 关键词清洗 → 分类
#
# 用法:
#   bash xml2csv                  # 增量模式
#   bash xml2csv --full           # 全量关键词清洗
#   bash xml2csv --skip-parse     # 跳过 XML 解析
#
# ============================================================
set -euo pipefail

FULL_CLEAN=false
SKIP_PARSE=false

for arg in "$@"; do
    case "$arg" in
        --full)       FULL_CLEAN=true ;;
        --skip-parse) SKIP_PARSE=true ;;
        *)            echo "Unknown option: $arg"; echo "Usage: bash xml2csv [--full] [--skip-parse]"; exit 1 ;;
    esac
done

if $FULL_CLEAN; then
    CLEAN_MODE="FULL"
else
    CLEAN_MODE="INCREMENTAL"
fi

echo ""
echo "======================================================================"
echo "  xml2csv — ABR XML → 商户知识库"
echo "  Mode: $CLEAN_MODE keyword clean"
echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================================================"

# ── Step 1: XML → CSV ──────────────────────────────────────────
if $SKIP_PARSE; then
    echo ""
    echo "[$(date '+%H:%M:%S')] Step 1/5: Parse XML → CSV  (SKIPPED)"
else
    echo ""
    echo "----------------------------------------------------------------------"
    echo "[$(date '+%H:%M:%S')] Step 1/5: Parse XML → CSV"
    echo "----------------------------------------------------------------------"
    python scripts/parse_abn.py
fi

# ── Step 2: Filter ─────────────────────────────────────────────
echo ""
echo "----------------------------------------------------------------------"
echo "[$(date '+%H:%M:%S')] Step 2/5: Filter Records"
echo "----------------------------------------------------------------------"
python scripts/filter_records.py

# ── Step 3: Merge ──────────────────────────────────────────────
echo ""
echo "----------------------------------------------------------------------"
echo "[$(date '+%H:%M:%S')] Step 3/5: Incremental Merge"
echo "----------------------------------------------------------------------"
python scripts/merge_update.py

# ── Step 4: Clean Keywords ─────────────────────────────────────
echo ""
echo "----------------------------------------------------------------------"
if $FULL_CLEAN; then
    echo "[$(date '+%H:%M:%S')] Step 4/5: Clean Keywords (FULL)"
    echo "----------------------------------------------------------------------"
    python scripts/clean_keywords.py --full
else
    echo "[$(date '+%H:%M:%S')] Step 4/5: Clean Keywords (INCREMENTAL)"
    echo "----------------------------------------------------------------------"
    python scripts/clean_keywords.py
fi

# ── Step 5: Categorize ─────────────────────────────────────────
echo ""
echo "----------------------------------------------------------------------"
echo "[$(date '+%H:%M:%S')] Step 5/5: Categorize"
echo "----------------------------------------------------------------------"
python scripts/categorize.py

# ── Done ───────────────────────────────────────────────────────
echo ""
echo "======================================================================"
echo "  Done! $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Output: data/kb_internal.csv → merchant_kb.csv"
echo "======================================================================"
