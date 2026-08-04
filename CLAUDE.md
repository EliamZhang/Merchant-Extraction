# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Australian merchant knowledge-base data pipeline. Parses ABR (Australian Business Register) XML bulletins into `merchant_kb.csv` (~2.5M rows), classifies merchants via web search, and verifies bank transaction counterparties against the knowledge base.

## Key commands

```bash
# Build knowledge base from ABR XML
python build_knowledge_base.py

# Deduplicate and clean keywords
python dedup_keywords.py --input merchant_kb.csv --full
python dedup_keywords.py --input merchant_kb.csv --changed-since 2026-07-28

# Merge manual entries
python merge_manual_entries.py --add-dir manual_entries/ --target merchant_kb.csv

# Classify KB merchants via DeepSeek (batch mode, no web search)
python label_merchants.py --api-key "$DEEPSEEK_API_KEY"

# Verify bank transaction counterparties (three-tier matching)
python verify_merchants.py --api-key "$DEEPSEEK_API_KEY"
python verify_merchants.py --api-key "$DEEPSEEK_API_KEY" --row-limit 100 --batch-size 5
```

## Merchant classification (primary workflow)

Use `.claude/skills/classify-merchants/SKILL.md` — a self-contained skill that reads `merchant_kb.csv`, web-searches each uncategorized merchant, and writes results back. Tracks searched merchants in `cache/web_classify_tracking.json` so none are re-processed.

## Architecture

```
xml_input/*.xml  →  build_knowledge_base.py  →  merchant_kb.csv
                                                 ├── classify-merchants skill (web search, self-contained)
                                                 ├── label_merchants.py (DeepSeek batch, no web search)
                                                 └── verify_merchants.py (three-tier: KB → cache → DeepSeek)
```

- **`settings.py`** — All configuration: entity-type filters (PRV/PUB), cancel cutoff date, stopwords (200+), known abbreviations (40+), keyword length thresholds, payment prefix words, CSV column definitions
- **`utils.py`** — Shared helpers: keyword splitting/cleaning, JSON extraction from LLM responses, DeepSeek API client (`post_json`), safe URL validation
- **`build_knowledge_base.py`** — Parses ABR XML, filters to PRV/PUB entities active after 2023, merges into `merchant_kb.csv` (updates existing by match_key, appends new)
- **`dedup_keywords.py`** — Cleans keyword fields: removes short tokens, stopwords, case-duplicates, keywords with zero token overlap with the merchant name
- **`merge_manual_entries.py`** — Merges hand-curated `manual_entries/*.csv` into `merchant_kb.csv` by merchant name
- **`verify_merchants.py`** — Three-tier verification: KB keyword match → API cache → DeepSeek batch. Propagates verified keywords across the CSV. Uses atomic saves (`*.tmp` + `replace`) with `atexit` for crash safety
- **`label_merchants.py`** — Direct DeepSeek batch classification (no web search), uses API cache
- **`split_uncategorized.py`** / **`update_category.py`** — Legacy batch workflow helpers

## Data files (gitignored — too large)

- `merchant_kb.csv` — Main knowledge base, ~2.5M rows, 13 internal columns (see `KB_INTERNAL_COLUMNS` in settings.py)
- `sample.csv` — Bank transaction sample for verification
- `xml_input/*.xml` — ABR bulletins
- `cache/` — API call caches + `web_classify_tracking.json`
