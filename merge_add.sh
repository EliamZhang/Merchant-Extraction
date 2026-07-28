#!/usr/bin/env bash
# ============================================================
# merge_add — 手工补充合并
# ============================================================
# 将 add/ 目录下的手工维护 CSV 合并到 merchant_kb.csv。
# 按商户名称（大小写和空格不敏感）匹配：
#   - 已有商户：填补空白字段（keywords、link、category）
#   - 新商户：插入到文件顶部
#
# 用法:
#   bash merge_add.sh
#   bash merge_add.sh --add-dir add/ --target merchant_kb.csv
#
# ============================================================
set -euo pipefail

ADD_DIR="add"
TARGET="merchant_kb.csv"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --add-dir) ADD_DIR="$2"; shift 2 ;;
        --target)  TARGET="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; echo "Usage: bash merge_add.sh [--add-dir DIR] [--target FILE]"; exit 1 ;;
    esac
done

echo ""
echo "======================================================================"
echo "  merge_add — 手工补充合并"
echo "  Source: $ADD_DIR/"
echo "  Target: $TARGET"
echo "  Start:  $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================================================"

python scripts/merge_add.py --add-dir "$ADD_DIR" --target "$TARGET"

echo ""
echo "======================================================================"
echo "  Done! $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================================================"
