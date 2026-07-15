# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This project extracts real merchant names from bank transaction text and classifies them into industry categories, using DeepSeek's API for AI decisions.

**Data flow:**
1. `third_party_dedup.csv` (deduplicated transaction `text` strings) → `verify_third_party_merchants.py` → `merchant_kb.csv` (grows with newly discovered merchants)
2. `merchant_kb.csv` (merchant names + keywords, missing categories) → `merchant_classifier.py` → `merchant_kb.csv` (with `category` filled)
3. `data_process.ipynb` — ad-hoc inspection and extraction of `merchant_names.csv` from the KB

## Scripts

### `verify_third_party_merchants.py`

Verifies whether a bank transaction `text` string contains a real merchant. Uses a three-tier matching strategy (in order):

- **Knowledge base match** — scans `merchant_kb.csv` keywords against each row's text; longest keyword wins
- **Cache match** — prior DeepSeek results keyed by canonical text
- **AI verification** — calls DeepSeek (model: `deepseek-chat`) with a prompt that extracts `standardized` name, verbatim `keyword` from text, `link`, and `is_real_merchant` boolean

When AI confirms a merchant, it also fans out the keyword across all *other* rows containing that keyword (not just exact-match rows). New AI-verified merchants are appended to `merchant_kb.csv`.

```bash
python verify_third_party_merchants.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --input third_party_dedup.csv \
  --output output/third_party_dedup_verified.csv \
  --cache cache/third_party_merchant_verification_cache.json \
  --merchant-kb merchant_kb.csv
```

Key flags: `--max-api-calls N`, `--row-limit N`, `--skip-merchant-kb-update`, `--checkpoint-every N`

### `merchant_classifier.py`

Classifies merchants from `merchant_kb.csv` into one of 22 predefined categories. Calls DeepSeek in batches (default size 20) with JSON-mode responses.

```bash
python merchant_classifier.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --merchant-kb merchant_kb.csv \
  --cache cache/merchant_category_cache.json \
  --model deepseek-v4-pro
```

Key flags: `--batch-size N`, `--row-limit N`, `--include-existing` (reclassify already-categorized rows), `--dry-run-stats`, `--save-every N`

Default env vars: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_THINKING_TYPE`, `DEEPSEEK_REASONING_EFFORT`

### Category enum (hardcoded in `merchant_classifier.py`)

`Automotive`, `Department Stores`, `Dining Out`, `Donations`, `Education`, `Entertainment`, `Gambling`, `Groceries`, `Gyms and other memberships`, `Health`, `Home Improvement`, `Information`, `Insurance`, `Personal Care`, `Pet Care`, `Rent`, `Retail`, `Subscription TV`, `Telecommunications`, `Transport`, `Travel`, `Utilities`

## Shared patterns across both scripts

Both scripts are standalone (no shared module), deliberately duplicating patterns:

- **`CacheStore`** — JSON file cache keyed by canonical text; atomic write via temp file + rename
- **DeepSeek client** — raw `urllib.request` POST to `/chat/completions` with `response_format: {"type": "json_object"}` and `temperature: 0`
- **`extract_json_object()`** — tries `json.loads`, falls back to regex `{.*}` extraction
- **CSV encoding** — all CSV reads use `utf-8-sig` (BOM) with `errors="replace"`; writes use `utf-8-sig`
- **`normalize_space()` / `normalize_search_text()`** — whitespace normalization and alphanumeric-only search normalization
- **`safe_url()`** — only returns URLs starting with `http://` or `https://`
- **Atomic saves** — write to `{filename}.{pid}.tmp`, then `replace()` to target

## Important notes

- All CSV and JSON files are in `.gitignore` — they are large data files, not source
- `merchant_kb.csv` is ~3.6M rows; both scripts support `--row-limit` for testing
- Cache JSON files are critical for cost control — DeepSeek API calls are not free
- Both scripts use `atexit` to save progress on interruption; checkpoint files provide additional safety
- No external dependencies beyond Python stdlib (the notebook uses `pandas`)
