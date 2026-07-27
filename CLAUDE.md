# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This project has two pipelines that process merchant/transaction data using DeepSeek's API for AI decisions:

1. **Merchant classification** — classifies merchant names from `merchant_kb.csv` into industry categories
2. **Third-party merchant verification** — verifies whether bank-transaction counterparty strings refer to real merchants, extracting standardized names and keywords

## Scripts

### `merchant_classifier.py`

Classifies merchants from `merchant_kb.csv` into one of 23 predefined categories. Calls DeepSeek in batches (default size 5) with JSON-mode responses and web search enabled.

**Data flow:** `merchant_kb.csv` (merchant names + keywords, some or all missing `category`) → `merchant_classifier.py` → `merchant_kb.csv` (with `category` filled)

```bash
python merchant_classifier.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --merchant-kb merchant_kb.csv \
  --cache cache/merchant_category_cache.json
```

Key flags: `--batch-size N`, `--row-limit N`, `--include-existing` (reclassify already-categorized rows), `--dry-run-stats`, `--save-every N`, `--cache-save-every N`, `--thinking-type`, `--reasoning-effort`, `--timeout-seconds`, `--max-retries`

### `verify_third_party_merchants.py`

Verifies bank-transaction counterparty strings against real merchants using DeepSeek batch API. Extracts standardized merchant names, keywords (literal text spans from the input), and verification URLs. Uses a multi-layered matching strategy: knowledge base → cache → AI API. Batch-matches keyword hits across all rows.

**Data flow:** `sample.csv` (bank transaction text rows) → `verify_third_party_merchants.py` → `output/sample_verified.csv` (with `standardized`, `keyword`, `link`, `match_source`, `matched_from_text` filled), also appends new entries to `merchant_kb.csv`

```bash
python verify_third_party_merchants.py
```

Input CSV header is case-insensitive: `text` and `Text` are both accepted.

Key flags: `--batch-size N` (default 5, candidates per API call; reduce if hitting 504 timeouts), `--max-api-calls N`, `--row-limit N`, `--skip-merchant-kb-update`, `--merchant-kb-save-every N`, `--cache-save-every N`, `--checkpoint-every N`, `--thinking-type`, `--reasoning-effort`, `--timeout-seconds`, `--max-retries`

Default paths: `--input sample.csv`, `--output output/sample_verified.csv`, `--cache cache/sample_verification_cache.json`, `--merchant-kb merchant_kb.csv`

Matching sources (recorded in `match_source` column): `knowledge_base_direct`, `knowledge_base_keyword`, `cache_direct`, `cache_keyword`, `ai_direct`, `ai_keyword`, `unresolved`

### `run_with_retry.sh`

Wrapper script that auto-restarts a Python script on non-zero exit. Stops immediately on Ctrl+C (exit 130) or SIGTERM (143) without retrying. Supports `--retry-delay N` (default 600s) before the script name.

```bash
# Merchant verification with auto-restart
bash run_with_retry.sh verify_third_party_merchants.py --api-key "$DEEPSEEK_API_KEY"

# Merchant classification with auto-restart
bash run_with_retry.sh merchant_classifier.py --api-key "$DEEPSEEK_API_KEY" --merchant-kb merchant_kb.csv
```

### `utils.py`

Shared utilities used by both scripts:
- **`post_json()`** — raw `urllib.request` POST with error handling
- **`extract_json_object()`** — tries `json.loads`, falls back to regex `{.*}` extraction
- **`normalize_space()`** / **`clean_output_value()`** — string normalization
- **`safe_url()`** — only returns URLs starting with `http://` or `https://`
- **`china_timestamp_now()`** — ISO timestamp in UTC+8
- **`open_csv_dict_reader()`** — streaming CSV reader (`utf-8-sig` encoding, reads line-by-line via file handle, not full-file load)

Note: `CacheStore` is defined separately in each script (not shared via utils.py) — each has domain-specific cache logic.

### Category enum (hardcoded in `merchant_classifier.py`)

`Automotive`, `Department Stores`, `Dining Out`, `Donations`, `Education`, `Entertainment`, `Financial Institutions`, `Gambling`, `Groceries`, `Gyms and other memberships`, `Health`, `Home Improvement`, `Information`, `Insurance`, `Personal Care`, `Pet Care`, `Rent`, `Retail`, `Subscription TV`, `Telecommunications`, `Transport`, `Travel`, `Utilities`

## Retry architecture

Both scripts have two layers of retry built in:

| Layer | Scope | Retries | What it handles |
|-------|-------|---------|-----------------|
| `_chat_completion` | Per HTTP request | `--max-retries` (default 20) | Network errors, HTTP errors |
| Batch loop in `process_file` / `classify_merchant_kb` | Per batch parse | `--max-retries` (default 20) | JSON parse errors, missing results array |

A third layer (`run_with_retry.sh`) handles complete script crashes with infinite retries every 10 minutes (configurable via `--retry-delay`).

## Important notes

- All CSV and JSON files are in `.gitignore` — they are large data files, not source
- `merchant_kb.csv` is ~8.7K rows; use `--row-limit` for testing
- `sample.csv` is ~173K rows; use `--row-limit` for testing
- Cache JSON files are critical for cost control — DeepSeek API calls are not free
- Both scripts use `atexit` to save progress on interruption (cache, checkpoint, merchant KB)
- No external dependencies beyond Python stdlib
- API calls include `enable_search: True` and `search_enabled: True` for web search
- Atomic saves: write to `{filename}.{pid}.tmp`, then `replace()` to target
- Default env vars for both scripts: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_THINKING_TYPE`, `DEEPSEEK_REASONING_EFFORT`
- Speed-oriented defaults: both scripts omit `thinking` and `reasoning_effort` unless `DEEPSEEK_THINKING_TYPE` / `DEEPSEEK_REASONING_EFFORT` or CLI flags override them.
- `verify_third_party_merchants.py` normalizes CSV header to lowercase `text` if the column is named `Text`
- Checkpoint, cache, and merchant KB are saved every 20 API calls by default (`--cache-save-every 20`, `--checkpoint-every 20`, `--merchant-kb-save-every 20`)
- The `load_rows()` function loads all input rows into memory; for very large files this may cause `MemoryError` — split the file or use `--row-limit`
