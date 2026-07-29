# Merchant Extraction

## Project overview

Pipeline that parses Australian business registry data, builds a merchant knowledge base (`merchant_kb.csv`), and classifies each merchant into a predefined category enum. Classification is done in three tiers: keyword rules (fast/cheap), DeepSeek API (batch), and manual/web-research (this tool).

## Key files

| File | Purpose |
|---|---|
| `classify_by_rules.py` | High-confidence keyword-based classification, runs first |
| `classify_merchants.py` | DeepSeek API batch classification, runs second |
| `verify_merchants.py` | Verification of existing classifications |
| `build_knowledge_base.py` | Build/sync KB from new XML data |
| `config.py` | Global config (paths, stopwords, abbreviations) |
| `merchant_kb.csv` | Final output KB (~2.5M rows, gitignored) |

## Category enum

`Automotive`, `Department Stores`, `Dining Out`, `Donations`, `Education`, `Entertainment`, `Financial Institutions`, `Gambling`, `Groceries`, `Gyms and other memberships`, `Health`, `Home Improvement`, `Information`, `Insurance`, `Personal Care`, `Pet Care`, `Rent`, `Retail`, `Subscription TV`, `Telecommunications`, `Transport`, `Travel`, `Utilities`

## Web-research classification workflow

When asked to classify merchants with empty categories via web research:

1. Find empty-category rows with a Python one-liner: `python3 -c "import csv; f=open('merchant_kb.csv',encoding='utf-8-sig'); empty=[r for r in csv.DictReader(f) if not r['category'].strip()]; print(f'empty: {len(empty)}')"`. The `keywords` field may contain commas, so don't use grep/csvcut — always use Python's `csv` module.
2. Skip shell companies (PTY LTD/Pty. Ltd. with no keywords), family trusts, nominees, superannuation funds, generic holding companies. Prioritize merchants with real websites and descriptive keywords.
3. Search each merchant via WebSearch. If search returns empty (common for small AU businesses), fall back to name/keyword inference.
4. Only assign when confident. Leave empty if unclear.
5. Update CSV via a Python script that: reads with `utf-8-sig`, sets `category` and `category_updated_at` (format `2026-07-29T17:26:31+08:00`), writes with `lineterminator="\n"`.
6. Delete the script after running.
7. Report results in a table: merchant name, category, reasoning.
