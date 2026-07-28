#!/usr/bin/env bash
# ============================================================
# clean_keywords — 关键词清洗
# ============================================================
# 对 merchant_kb.csv 的 keywords 字段进行清洗：
#   1. 移除长度 < 5 的关键词
#   2. 移除命中停用词的单 token 关键词
#   3. 大小写去重
#
# 用法:
#   bash clean_keywords.sh              # 增量清洗（只处理新增/变更的记录）
#   bash clean_keywords.sh --full       # 全量清洗
#   bash clean_keywords.sh --full --report report.csv  # 全量 + 清洗报告
#
# ============================================================
set -euo pipefail

FULL_FLAG=""
REPORT_FLAG=""
REPORT_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --full)
            FULL_FLAG="--full"
            shift ;;
        --report)
            REPORT_PATH="$2"
            REPORT_FLAG="--report $2"
            shift 2 ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: bash clean_keywords.sh [--full] [--report PATH]"
            exit 1 ;;
    esac
done

echo ""
echo "======================================================================"
echo "  clean_keywords — 关键词清洗"
if [ -n "$FULL_FLAG" ]; then
    echo "  Mode: FULL"
else
    echo "  Mode: INCREMENTAL"
fi
echo "  Target: merchant_kb.csv"
echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================================================"

python clean_keywords.py \
    --input merchant_kb.csv \
    $FULL_FLAG \
    $REPORT_FLAG

echo ""
echo "======================================================================"
echo "  Done! $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================================================"
