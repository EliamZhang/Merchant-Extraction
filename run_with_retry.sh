#!/bin/bash
# Wrapper script: auto-restart a Python script on failure
# Keeps retrying with a 10-minute delay until it exits cleanly (code 0)
#
# Usage:
#   bash run_with_retry.sh verify_third_party_merchants.py --api-key "$DEEPSEEK_API_KEY"
#   bash run_with_retry.sh merchant_classifier.py --api-key "$DEEPSEEK_API_KEY" --merchant-kb merchant_kb.csv

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

RETRY_DELAY_SECONDS=600  # 10 minutes

if [ $# -eq 0 ]; then
    echo "Usage: bash run_with_retry.sh [--retry-delay N] <python_script> [args...]"
    echo "  --retry-delay N  wait N seconds between retries (default 600)"
    echo "  python_script: scripts/verify_third_party_merchants.py or scripts/merchant_classifier.py"
    exit 1
fi

if [ "$1" = "--retry-delay" ]; then
    RETRY_DELAY_SECONDS="$2"
    shift 2
fi

PYTHON_SCRIPT="$1"
shift  # remaining args pass through to the Python script
ATTEMPT=0

while true; do
    ATTEMPT=$((ATTEMPT + 1))
    echo "=== Attempt $ATTEMPT: $(date) ==="

    set +e
    python "$PYTHON_SCRIPT" "$@"
    EXIT_CODE=$?
    set -e
    if [ "$EXIT_CODE" -eq 0 ]; then
        echo "=== SUCCESS: $PYTHON_SCRIPT completed on attempt $ATTEMPT at $(date) ==="
        exit 0
    fi

    # 130 = SIGINT (Ctrl+C), 143 = SIGTERM — user-initiated, don't retry
    if [ "$EXIT_CODE" -eq 130 ] || [ "$EXIT_CODE" -eq 143 ]; then
        echo "=== STOPPED by signal (exit code $EXIT_CODE), not retrying ==="
        exit "$EXIT_CODE"
    fi
    echo "=== FAILED with exit code $EXIT_CODE at $(date), will retry in ${RETRY_DELAY_SECONDS}s ==="
    sleep "$RETRY_DELAY_SECONDS"
done
