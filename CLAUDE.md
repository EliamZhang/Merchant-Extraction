# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This project has two pipelines that process merchant/transaction data using DeepSeek's API for AI decisions:

1. **Merchant classification** — classifies merchant names from `merchant_kb.csv` into industry categories
2. **Third-party merchant verification** — verifies whether bank-transaction counterparty strings refer to real merchants, extracting standardized names and keywords

## Scripts

### `merchant_classifier.py`

Classifies merchants from `merchant_kb.csv` into one of 27 predefined categories. Calls DeepSeek in batches (default size 50) with JSON-mode responses and web search enabled.

**Data flow:** `merchant_kb.csv` (merchant names + keywords, some or all missing `category`) → `merchant_classifier.py` → `merchant_kb.csv` (with `category` filled)

```bash
python merchant_classifier.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --merchant-kb merchant_kb.csv \
  --cache cache/merchant_category_cache.json
```

Key flags: `--batch-size N`, `--row-limit N`, `--include-existing` (reclassify already-categorized rows), `--dry-run-stats`, `--save-every N`, `--thinking-type`, `--reasoning-effort`, `--timeout-seconds`, `--max-retries`

### `verify_third_party_merchants.py`

Verifies bank-transaction counterparty strings against real merchants using DeepSeek batch API. Extracts standardized merchant names, keywords (literal text spans from the input), and verification URLs. Uses a multi-layered matching strategy: knowledge base → cache → AI API. Batch-matches keyword hits across all rows.

**Data flow:** `sample.csv` (bank transaction text rows) → `verify_third_party_merchants.py` → `output/sample_verified.csv` (with `standardized`, `keyword`, `link`, `match_source`, `matched_from_text` filled), also appends new entries to `merchant_kb.csv`

```bash
python verify_third_party_merchants.py
```

Key flags: `--batch-size N` (default 5, candidates per API call; reduce if hitting 504 timeouts), `--max-api-calls N`, `--row-limit N`, `--skip-merchant-kb-update`, `--merchant-kb-save-every N`, `--checkpoint-every N`, `--progress-every N`, `--thinking-type`, `--reasoning-effort`, `--timeout-seconds`, `--max-retries`

Default paths: `--input sample.csv`, `--output output/sample_verified.csv`, `--cache cache/sample_verification_cache.json`, `--merchant-kb merchant_kb.csv`

Matching sources (recorded in `match_source` column): `knowledge_base_direct`, `knowledge_base_keyword`, `cache_direct`, `cache_keyword`, `ai_direct`, `ai_keyword`, `unresolved`

### `utils.py`

Shared utilities used by both scripts:
- **`post_json()`** — raw `urllib.request` POST with error handling
- **`extract_json_object()`** — tries `json.loads`, falls back to regex `{.*}` extraction
- **`normalize_space()`** / **`clean_output_value()`** — string normalization
- **`safe_url()`** — only returns URLs starting with `http://` or `https://`
- **`china_timestamp_now()`** — ISO timestamp in UTC+8
- **`open_csv_dict_reader()`** — reads CSV with `utf-8-sig` encoding

Note: `CacheStore` is defined separately in each script (not shared via utils.py) — each has domain-specific cache logic.

### Category enum (hardcoded in `merchant_classifier.py`)

`Automotive`, `Debt Collection`, `Debt Consolidation`, `Department Stores`, `Dining Out`, `Donations`, `Education`, `Entertainment`, `Financial Services`, `Gambling`, `Groceries`, `Gyms and other memberships`, `Health`, `Home Improvement`, `Information`, `Insurance`, `Personal Care`, `Pet Care`, `Professional Services`, `Property and Strata`, `Rent`, `Retail`, `Subscription TV`, `Telecommunications`, `Transport`, `Travel`, `Utilities`

## Important notes

- All CSV and JSON files are in `.gitignore` — they are large data files, not source
- `merchant_kb.csv` is ~3.6M rows; use `--row-limit` for testing
- `sample.csv` is ~48K rows
- Cache JSON files are critical for cost control — DeepSeek API calls are not free
- Both scripts use `atexit` to save progress on interruption
- No external dependencies beyond Python stdlib
- API calls include `enable_search: True` and `search_enabled: True` for web search
- Atomic saves: write to `{filename}.{pid}.tmp`, then `replace()` to target
- Default env vars for both scripts: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_THINKING_TYPE`, `DEEPSEEK_REASONING_EFFORT`
