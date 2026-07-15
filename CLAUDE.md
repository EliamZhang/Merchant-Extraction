# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This project classifies merchant names from `merchant_kb.csv` into industry categories using DeepSeek's API for AI decisions.

**Data flow:**
`merchant_kb.csv` (merchant names + keywords, some or all missing `category`) → `merchant_classifier.py` → `merchant_kb.csv` (with `category` filled)

## Scripts

### `merchant_classifier.py`

Classifies merchants from `merchant_kb.csv` into one of 27 predefined categories. Calls DeepSeek in batches (default size 50) with JSON-mode responses and web search enabled.

```bash
python merchant_classifier.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --merchant-kb merchant_kb.csv \
  --cache cache/merchant_category_cache.json
```

Key flags: `--batch-size N`, `--row-limit N`, `--include-existing` (reclassify already-categorized rows), `--dry-run-stats`, `--save-every N`, `--thinking-type`, `--reasoning-effort`, `--timeout-seconds`, `--max-retries`

Default env vars: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_THINKING_TYPE`, `DEEPSEEK_REASONING_EFFORT`

### `utils.py`

Shared utilities imported by `merchant_classifier.py`:
- **`CacheStore`** — JSON file cache keyed by canonical text; atomic write via temp file + rename (merchant_classifier.py has its own subclass with domain-specific `get`/`set`)
- **`post_json()`** — raw `urllib.request` POST with error handling
- **`extract_json_object()`** — tries `json.loads`, falls back to regex `{.*}` extraction
- **`normalize_space()`** / **`clean_output_value()`** — string normalization
- **`safe_url()`** — only returns URLs starting with `http://` or `https://`
- **`china_timestamp_now()`** — ISO timestamp in UTC+8
- **`open_csv_dict_reader()`** — reads CSV with `utf-8-sig` encoding

### Category enum (hardcoded in `merchant_classifier.py`)

`Automotive`, `Debt Collection`, `Debt Consolidation`, `Department Stores`, `Dining Out`, `Donations`, `Education`, `Entertainment`, `Financial Services`, `Gambling`, `Groceries`, `Gyms and other memberships`, `Health`, `Home Improvement`, `Information`, `Insurance`, `Personal Care`, `Pet Care`, `Professional Services`, `Property and Strata`, `Rent`, `Retail`, `Subscription TV`, `Telecommunications`, `Transport`, `Travel`, `Utilities`

## Important notes

- All CSV and JSON files are in `.gitignore` — they are large data files, not source
- `merchant_kb.csv` is ~3.6M rows; use `--row-limit` for testing
- Cache JSON files are critical for cost control — DeepSeek API calls are not free
- The classifier uses `atexit` to save progress on interruption
- No external dependencies beyond Python stdlib
- API calls include `enable_search: True` and `search_enabled: True` for web search
- Atomic saves: write to `{filename}.{pid}.tmp`, then `replace()` to target
