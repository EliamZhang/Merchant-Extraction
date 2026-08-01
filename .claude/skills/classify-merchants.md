---
name: classify-merchants
description: Fast, evidence-based web-search classification of uncategorized merchants in merchant_kb.csv. Self-contained: reads CSV directly, tracks searched merchants, and writes results back. No external script dependencies.
arguments:
  - name: batch_size
    description: Number of merchants to process per invocation (default 10)
    required: false
  - name: max_batches
    description: Maximum batches to run before stopping (default 1). Set to 0 for unlimited.
    required: false
---

# Classify Merchants via Web Search

You are an autonomous batch processor. Every invocation selects merchants, searches the web, writes results, then stops with a summary. Do not ask questions; use conservative judgment and run.

## Core Principle

This skill is self-contained. It reads `merchant_kb.csv` directly, uses `cache/web_classify_tracking.json` to remember merchants already searched, and writes classification results directly back to `merchant_kb.csv`. Do not call project scripts.

Optimize for:
- Fast: one strong search round for most merchants, second round only with a real lead.
- Accurate: classify only when web evidence confirms the real-world business activity.
- Conservative: return `""` when the activity or legal-entity-to-brand link is unclear.

## Tracking File

`cache/web_classify_tracking.json`:
```json
{
  "searched": ["merchant name 1", "merchant name 2"],
  "last_updated": "2026-08-01T12:00:00+08:00"
}
```

Merchants in `searched` are skipped forever because they were web-searched and either received a category or were confirmed unfindable.

## Workflow

### Step 0: Parse Arguments

`$batch_size` defaults to 10. `$max_batches` defaults to 1. If `$max_batches` is 0, keep going until there are no uncategorized and unsearched merchants. If `$max_batches` is omitted, default to 1.

Run the steps below as a loop, up to `$max_batches` batches.

### Step 1: Find Next Batch

First, ensure the tracking file exists:

```bash
python -c "import json, os; os.makedirs('cache', exist_ok=True); p='cache/web_classify_tracking.json'; (not os.path.exists(p)) and json.dump({'searched':[],'last_updated':''}, open(p,'w',encoding='utf-8'), ensure_ascii=False, indent=2)"
```

Then find uncategorized and unsearched merchants:

```bash
python -c "
import csv, json, sys

csv_path = 'merchant_kb.csv'
tracking_path = 'cache/web_classify_tracking.json'
batch_size = int(sys.argv[1])

def norm(value):
    return ' '.join((value or '').strip().split()).casefold()

with open(tracking_path, encoding='utf-8') as f:
    searched = {norm(name) for name in json.load(f).get('searched', [])}

candidates = []
seen = set()
with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
    for row in csv.DictReader(f):
        name = ' '.join((row.get('merchant_name') or '').strip().split())
        key = norm(name)
        category = (row.get('category') or '').strip()
        if name and not category and key not in searched and key not in seen:
            candidates.append(name)
            seen.add(key)
            if len(candidates) >= batch_size:
                break

if not candidates:
    print('ALL_DONE')
else:
    for name in candidates:
        print(name)
" $batch_size
```

If output is `ALL_DONE`, stop and report: `All done - every merchant has a category or has been searched.`

### Step 2: Search and Classify

Process the batch as a single classification unit. Search queries for different merchants and query variants should be run in parallel whenever the environment supports it.

#### Search Budget

Default budget per merchant:
- Round 1: one parallel search set.
- Round 2: only if Round 1 produced a plausible lead but not enough evidence.
- Stop after Round 2. Do not keep exploring.

Fast-skip names still require one search round before returning `""`, but they never get Round 2 unless a strong public-facing lead appears.

#### Round 1 Query Plan

For each merchant, build a compact set of high-signal queries:
- Exact legal name: `"{merchant_name}"`
- Australia-biased exact name when Australian context is likely: `"{merchant_name}" Australia`
- If the name contains a legal suffix, also search the suffix-stripped version in the same round.
- If the CSV `keywords` or `link` field is available in context, use it only as supporting context, not as proof.

Legal suffixes to strip for variant searches:
- `Pty Ltd`, `Proprietary Limited`, `Ltd`, `Limited`, `No Liability`, `NL`
- `Trading Pty Ltd`, `Holdings Pty Ltd`, `Group Pty Ltd`, `Nominees Pty Ltd`
- punctuation-only suffix noise after stripping

Australian context is likely when the name contains:
- `Pty`, `ABN`, `ACN`, Australian state abbreviations, or Australian place names.

#### Fast-Skip Patterns

These patterns are usually non-public corporate entities. Search Round 1 only; if no clear consumer-facing business, return `""`:
- Contains `Holdings`, `Nominees`, `Investments`, `Acquisitions`, `Pastoral`, `Superannuation`, `Family Trust`, `Trustee`
- Contains `Group Pty Ltd` without a known brand result
- Personal name plus `Enterprises`, `Trading`, `Consulting`, or `Services`
- Generic location/word plus `Enterprises`, `Acquisitions`, `Investments`, or `Holdings`
- ABN/ASIC-only results with no registered trading/business name

#### Evidence Rules

Classify only with one of these evidence patterns:
- Official website or brand page clearly shows the activity and matches the merchant/legal name.
- ABN Lookup/ABR shows a registered business or trading name; that trading name is then found as a real business with an activity.
- Franchisee/store/operator list, shopping-centre tenant page, map listing, or reputable directory links the legal entity or trading name to an operating business.
- Legal PDF/news/database links the legal entity to a brand, and another source confirms the brand activity.

Return `""` when:
- Results are only ABN/ASIC/company-registration pages with no trading name.
- A brand exists but cannot be linked to the searched legal entity or trading name.
- The name is too generic and search results conflict.
- The activity cannot be mapped confidently to exactly one valid category.
- Only social media or weak directory snippets exist and no corroborating source is found.

#### Round 2 Triggers

Run Round 2 only if Round 1 produced a plausible lead:
- A trading/business name from ABN Lookup.
- A possible brand/store name.
- A specific location plus business listing.
- A source naming an industry but not enough to classify.

Round 2 query options:
- Search the trading/business name exactly.
- Search suffix-stripped name plus one likely industry hint found from Round 1.
- Search `"legal name" "trading as"` or `"legal name" franchise`.
- Search the candidate brand plus Australia if the result set is global/noisy.

Do not run Round 2 when Round 1 found only registry pages or no meaningful lead.

#### Source Reliability

Use sources in this order:
1. Official brand/store/franchise pages.
2. ABN Lookup/ABR trading or business names.
3. Shopping centre tenant pages, maps, reputable business directories.
4. Legal documents, PDFs, franchise schedules, court filings.
5. Industry databases/SIC/ANZSIC records as supporting evidence only.
6. News and social media as weak corroboration only.

#### Classification Output While Searching

For each merchant, keep a short internal note:
- `category`: valid category or `""`
- `confidence`: `high`, `medium`, or `empty`
- `evidence`: one short phrase naming the best evidence
- `reason`: why the category was chosen or why it is empty

The final saved JSON only needs `results`; `evidence` and `reason` are for your summary and self-check.

### Step 3: Write Results

Write `cache/_batch_result.json` with every merchant from the batch included exactly once:

```json
{
  "results": {
    "Merchant Name 1": "Dining Out",
    "Merchant Name 2": ""
  }
}
```

Rules:
- Empty string means searched but no category was confirmed.
- Category values must exactly match the valid category list.
- Never omit a merchant from the batch.

Then run the save script:

```bash
python -c "
import csv, json, os, time
from datetime import datetime, timezone, timedelta

VALID = {
    'Automotive', 'Department Stores', 'Dining Out', 'Donations', 'Education',
    'Entertainment', 'Financial Institutions', 'Gambling', 'Groceries',
    'Gyms and other memberships', 'Health', 'Home Improvement', 'Information',
    'Insurance', 'Personal Care', 'Pet Care', 'Rent', 'Retail',
    'Subscription TV', 'Telecommunications', 'Transport', 'Travel', 'Utilities'
}

def norm(value):
    return ' '.join((value or '').strip().split()).casefold()

with open('cache/_batch_result.json', 'r', encoding='utf-8') as f:
    batch = json.load(f)
results = batch['results']
if not isinstance(results, dict) or not results:
    raise ValueError('cache/_batch_result.json must contain a non-empty results object')

clean_results = {}
for name, category in results.items():
    merchant_name = ' '.join(str(name).strip().split())
    category = str(category).strip()
    if not merchant_name:
        raise ValueError('Result contains an empty merchant name')
    if category and category not in VALID:
        raise ValueError(f'Invalid category for {merchant_name}: {category}')
    clean_results[merchant_name] = category

csv_path = 'merchant_kb.csv'
tracking_path = 'cache/web_classify_tracking.json'
now = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%dT%H:%M:%S+08:00')

with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
    reader = csv.DictReader(f)
    fieldnames = list(reader.fieldnames or [])
    rows = list(reader)

required = {'merchant_name', 'category', 'category_source', 'category_updated_at'}
missing = required - set(fieldnames)
if missing:
    raise ValueError(f'Merchant KB missing required columns: {sorted(missing)}')

results_by_key = {norm(name): category for name, category in clean_results.items()}
updated = 0
matched = set()
for row in rows:
    key = norm(row.get('merchant_name', ''))
    if key not in results_by_key:
        continue
    matched.add(key)
    new_cat = results_by_key[key]
    if new_cat and not (row.get('category') or '').strip():
        row['category'] = new_cat
        row['category_source'] = 'web_search'
        row['category_updated_at'] = now
        updated += 1

unmatched = set(results_by_key) - matched
if unmatched:
    raise ValueError(f'Results did not match CSV merchants: {sorted(unmatched)[:5]}')

target = os.path.abspath(csv_path)
tmp = f'{target}.{os.getpid()}.tmp'
with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
for attempt in range(1, 4):
    try:
        os.replace(tmp, target)
        break
    except PermissionError:
        if attempt >= 3:
            raise
        time.sleep(1)

searched_names = []
if os.path.exists(tracking_path):
    with open(tracking_path, encoding='utf-8') as f:
        searched_names = [
            ' '.join(str(name).strip().split())
            for name in json.load(f).get('searched', [])
            if str(name).strip()
        ]
searched_keys = {norm(name) for name in searched_names}
for name in clean_results:
    key = norm(name)
    if key not in searched_keys:
        searched_names.append(name)
        searched_keys.add(key)

tracking_payload = {
    'searched': sorted(searched_names, key=lambda value: value.casefold()),
    'last_updated': now
}
tracking_tmp = f'{os.path.abspath(tracking_path)}.{os.getpid()}.tmp'
with open(tracking_tmp, 'w', encoding='utf-8') as f:
    json.dump(tracking_payload, f, ensure_ascii=False, indent=2)
os.replace(tracking_tmp, tracking_path)

os.remove('cache/_batch_result.json')

classified = sum(1 for v in clean_results.values() if v.strip())
empty = len(clean_results) - classified
print(f'SAVED: classified={classified} empty={empty} updated={updated} total_tracked={len(searched_names)}')
"
```

### Step 4: Loop or Stop

Report a one-line summary after each batch. If `$max_batches` is 0, go back to Step 1. If the configured number of batches has completed, stop. If Step 1 returned `ALL_DONE`, stop.

Final report format:
```text
Batch N: classified=X empty=Y updated=U | total tracked=Z | [CONTINUING|ALL_DONE]
```

## Classification Rules

- Classify by actual, real-world business activity confirmed via web search.
- Must use web search; do not guess from name fragments.
- Prefer `""` over a weak or inferred category.
- For names with legal suffixes, search both full and suffix-stripped forms in the same round.
- When ABN Lookup shows a trading/business name different from the legal name, search that trading name too.
- If a source only proves a legal entity exists, not what it operates, return `""`.
- For holding/property/investment/trust entities, do not use passive asset ownership as proof of `Rent` unless the entity operates property management, leasing, real estate agency, or storage services.

## Valid Categories

Exact, case-sensitive:

Automotive, Department Stores, Dining Out, Donations, Education, Entertainment, Financial Institutions, Gambling, Groceries, Gyms and other memberships, Health, Home Improvement, Information, Insurance, Personal Care, Pet Care, Rent, Retail, Subscription TV, Telecommunications, Transport, Travel, Utilities

Quick reference:
- Automotive: fuel, vehicles, repairs, parts, car washes, roadside
- Department Stores: large mixed-retail, discount, supercentre
- Dining Out: restaurants, cafes, bars, fast food, food delivery, catering, prepared meals
- Donations: charities, non-profits, fundraising, religious giving
- Education: childcare, schools, universities, tutoring, training
- Entertainment: cinemas, theatres, museums, attractions, events, clubs, music, games
- Financial Institutions: banks, lenders, payment services, mortgages, securities, wealth, brokers
- Gambling: casinos, betting, wagering, lotteries, gaming venues
- Groceries: supermarkets, food shops, bakeries, butchers, seafood, liquor, bottle shops, food suppliers, wholesalers, processors
- Gyms and other memberships: gyms, fitness, yoga, pilates, sports training, member clubs
- Health: pharmacies, dentists, optometrists, clinics, hospitals, healthcare
- Home Improvement: construction, trades, hardware, cleaning, repairs, maintenance, facilities, security, building services
- Information: software, IT, computer services, data, media, publishing, digital platforms
- Insurance: insurers, brokers, policies, claims, warranties
- Personal Care: hair, beauty, nails, spas, grooming, laundry, tailoring, consumer photography
- Pet Care: vets, animal hospitals, pet shops, pet food, grooming, boarding
- Rent: rent, leases, property managers, real estate agencies, storage
- Retail: clothing, shoes, jewellery, books, florists, gifts, electronics, specialty goods
- Subscription TV: cable, satellite, streaming TV, paid television
- Telecommunications: mobile, phone, internet, broadband, network, telecom providers
- Transport: public transport, taxis, rideshare, parking, tolls, freight, logistics, couriers, vehicle registration
- Travel: hotels, holiday rentals, airlines, travel agencies, tours, cruises, car rental
- Utilities: electricity, gas, water, waste, taxes, council rates, government fees, fines, public services

## Tie-Breakers

- Bottle shops, liquor stores, bakeries, butchers, seafood shops, and food wholesalers: `Groceries`
- Cafes, restaurants, bars, catering, take-away, prepared meals: `Dining Out`
- Pharmacies: `Health`, even if they also sell retail goods
- Hardware, building supplies, trades, cleaners, maintenance, security installers: `Home Improvement`
- Passive investment/property holders: `""` unless operating a customer-facing rent/property service
- Sports clubs: `Entertainment`; gyms, fitness studios, yoga/pilates: `Gyms and other memberships`
- Streaming TV: `Subscription TV`; general software/SaaS/media platforms: `Information`
- Council rates, fines, taxes, government fees: `Utilities`

## Examples

| Merchant | Search finding | Category |
|----------|----------------|----------|
| Naked for Satan | Bar/restaurant in Melbourne | Dining Out |
| Cellarbrations | Bottle shop / liquor store chain | Groceries |
| Blackburn Football Club | Local football/sports club | Entertainment |
| Chemist Warehouse | Pharmacy chain | Health |
| BWS | Beer Wine Spirits bottle shop | Groceries |
| Uber | Rideshare platform | Transport |
| Bunnings Warehouse | Hardware store chain | Home Improvement |
| Telstra | Telco provider | Telecommunications |
| Netflix | Streaming TV service | Subscription TV |
| BENMIREN NOMINEES PTY LTD | Generic corporate entity, no public business | "" |
| GOCUP PASTORAL PTY LTD | No public-facing business found | "" |
| J SMITH ENTERPRISES PTY LTD | Personal/generic enterprise, no clear business | "" |

## Safety Rules

- Only modify `category`, `category_source`, and `category_updated_at` values in `merchant_kb.csv`.
- Only write to `merchant_kb.csv`, `cache/web_classify_tracking.json`, and `cache/_batch_result.json`.
- Read CSV with `utf-8-sig`; write CSV with `utf-8-sig` to preserve Excel compatibility.
- Use the save script for tracking updates; do not edit tracking manually.
- If the save script fails, report the error and stop. Do not retry with different code.
