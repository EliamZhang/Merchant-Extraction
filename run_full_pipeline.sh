#!/usr/bin/env bash
set -euo pipefail

API_KEY="${DEEPSEEK_API_KEY:?DEEPSEEK_API_KEY must be set}"

echo "=========================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 1/3: merchant_classifier.py"
echo "=========================================="
bash run_with_retry.sh python merchant_classifier.py \
  --api-key "$API_KEY" \
  --merchant-kb merchant_kb.csv \
  --cache cache/merchant_category_cache.json

echo ""
echo "=========================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 2/3: verify_third_party_merchants.py"
echo "=========================================="
bash run_with_retry.sh python verify_third_party_merchants.py \
  --api-key "$API_KEY"

echo ""
echo "=========================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 3/3: merchant_classifier.py"
echo "=========================================="
bash run_with_retry.sh python merchant_classifier.py \
  --api-key "$API_KEY" \
  --merchant-kb merchant_kb.csv \
  --cache cache/merchant_category_cache.json

echo ""
echo "=========================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] All done!"
echo "=========================================="
