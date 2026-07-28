#!/usr/bin/env bash
# ============================================================
# xml2csv — 官方企业库合并：ABR XML → 商户知识库
# ============================================================
# 依次执行：构建知识库 → 关键词清洗 → 导出
#
# 用法:
#   bash xml2csv.sh                  # 增量模式
#   bash xml2csv.sh --full           # 全量关键词清洗
#   bash xml2csv.sh --skip-parse     # 跳过 XML 解析
#
# ============================================================
set -euo pipefail

FULL_CLEAN=false
SKIP_PARSE=false

for arg in "$@"; do
    case "$arg" in
        --full)       FULL_CLEAN=true ;;
        --skip-parse) SKIP_PARSE=true ;;
        *)            echo "Unknown option: $arg"; echo "Usage: bash xml2csv.sh [--full] [--skip-parse]"; exit 1 ;;
    esac
done

if $FULL_CLEAN; then
    CLEAN_MODE="FULL"
else
    CLEAN_MODE="INCREMENTAL"
fi

echo ""
echo "======================================================================"
echo "  xml2csv — 官方企业库合并"
echo "  Mode: $CLEAN_MODE keyword clean"
echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================================================"

# ── Step 1: Build KB (parse + filter + merge + categorize) ─────
SKIP_FLAG=""
if $SKIP_PARSE; then
    SKIP_FLAG="--skip-parse"
fi
echo ""
echo "----------------------------------------------------------------------"
echo "[$(date '+%H:%M:%S')] Step 1/3: Build KB (parse + filter + merge + categorize)"
echo "----------------------------------------------------------------------"
python scripts/build_kb.py $SKIP_FLAG

# ── Step 2: Clean Keywords ────────────────────────────────────
echo ""
echo "----------------------------------------------------------------------"
if $FULL_CLEAN; then
    echo "[$(date '+%H:%M:%S')] Step 2/3: Clean Keywords (FULL)"
    echo "----------------------------------------------------------------------"
    python scripts/clean_keywords.py --input data/kb_internal.csv --full
else
    echo "[$(date '+%H:%M:%S')] Step 2/3: Clean Keywords (INCREMENTAL)"
    echo "----------------------------------------------------------------------"
    python scripts/clean_keywords.py --input data/kb_internal.csv
fi

# ── Step 3: Export (kb_internal.csv → merchant_kb.csv) ─────────
echo ""
echo "----------------------------------------------------------------------"
echo "[$(date '+%H:%M:%S')] Step 3/3: Export → merchant_kb.csv"
echo "----------------------------------------------------------------------"
python -c "from scripts.build_kb import export_final; export_final()"

# ── Done ───────────────────────────────────────────────────────
echo ""
echo "======================================================================"
echo "  Done! $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================================================"
