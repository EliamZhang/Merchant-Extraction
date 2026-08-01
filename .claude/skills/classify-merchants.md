---
name: classify-merchants
description: Autonomous web-search classification of uncategorized merchants in merchant_kb.csv. Self-contained — reads CSV directly, tracks searched merchants, and writes results back. No external script dependencies.
arguments:
  - name: batch_size
    description: Number of merchants to process per invocation (default 10)
    required: false
  - name: max_batches
    description: Maximum batches to run before stopping (default 1). Set to 0 for unlimited.
    required: false
---

# Classify Merchants via Web Search

You are an autonomous batch processor. Every invocation processes merchants, writes results, then stops with a summary. Do NOT ask questions — just run.

## Core principle

This skill is SELF-CONTAINED. It reads `merchant_kb.csv` directly, uses `cache/web_classify_tracking.json` to remember which merchants have already been searched, and writes classification results directly back to `merchant_kb.csv`. Zero dependency on other scripts.

## Tracking file

`cache/web_classify_tracking.json`:
```json
{
  "searched": ["merchant name 1", "merchant name 2"],
  "last_updated": "2026-08-01T12:00:00+08:00"
}
```
Merchants in `searched` are skipped forever — they've been web-searched and either got a category or were confirmed unfindable.

## Workflow

### Step 0: Parse arguments

`$batch_size` defaults to 10. `$max_batches` defaults to 1. If `$max_batches` is 0, keep going until no more uncategorized merchants. If `$max_batches` is omitted entirely, default to 1.

Run the steps below as a loop (up to `$max_batches` times).

### Step 1: Find next batch

First, ensure the tracking file exists:

```bash
python -c "import json, os; os.makedirs('cache', exist_ok=True); p='cache/web_classify_tracking.json'; (not os.path.exists(p)) and json.dump({'searched':[],'last_updated':''}, open(p,'w'))"
```

Then find uncategorized + unsearched merchants:

```bash
python -c "
import csv, json, os, sys

csv_path = 'merchant_kb.csv'
tracking_path = 'cache/web_classify_tracking.json'
batch_size = int(sys.argv[1])

with open(tracking_path) as f:
    searched = set(json.load(f).get('searched', []))

candidates = []
with open(csv_path, 'r', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        name = row.get('merchant_name', '').strip()
        category = row.get('category', '').strip()
        if name and not category and name not in searched:
            candidates.append(name)
            if len(candidates) >= batch_size:
                break

if not candidates:
    print('ALL_DONE')
else:
    for name in candidates:
        print(name)
" $batch_size
```

If output is `ALL_DONE`: stop and report "All done — every merchant has a category or has been searched."

### Step 2: Web search and classify each merchant

For EVERY merchant from Step 1:

1. WebSearch: `"{merchant_name}" Australia business type`
2. Read the results. Identify the real-world business activity.
3. Assign a category from the valid list below, or `""` if unconfirmable.
4. Keep a running list of `{merchant_name: category}` pairs.

If a search is inconclusive, try one more with different terms (e.g. drop "PTY LTD", add "company", etc.). If still nothing, category = `""`.

### Step 3: Write results

Write results to a temp file, then call the save script:

First, write `cache/_batch_result.json`:
```json
{
  "results": {
    "Merchant Name 1": "Dining Out",
    "Merchant Name 2": ""
  }
}
```
Empty string means "searched but no category found".

Then run the save script:

```bash
python -c "
import csv, json, os
from datetime import datetime, timezone, timedelta

# Load batch results
with open('cache/_batch_result.json', 'r', encoding='utf-8') as f:
    batch = json.load(f)
results = batch['results']

csv_path = 'merchant_kb.csv'
tracking_path = 'cache/web_classify_tracking.json'
now = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%dT%H:%M:%S+08:00')

# Update CSV
with open(csv_path, 'r', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    rows = list(reader)

updated = 0
for row in rows:
    name = row.get('merchant_name', '').strip()
    if name in results:
        new_cat = results[name].strip()
        if new_cat:
            row['category'] = new_cat
            row['category_source'] = 'web_search'
            row['category_updated_at'] = now
            updated += 1

with open(csv_path, 'w', encoding='utf-8', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

# Update tracking
searched = set()
if os.path.exists(tracking_path):
    with open(tracking_path) as f:
        searched = set(json.load(f).get('searched', []))
searched.update(results.keys())
with open(tracking_path, 'w') as f:
    json.dump({
        'searched': sorted(searched),
        'last_updated': now
    }, f, ensure_ascii=False, indent=2)

# Clean up batch file
os.remove('cache/_batch_result.json')

# Report
classified = sum(1 for v in results.values() if v.strip())
empty = len(results) - classified
total_tracked = len(searched)
print(f'SAVED: classified={classified} empty={empty} total_tracked={total_tracked}')
"
```

### Step 4: Loop or stop

Report a one-line summary. If `$max_batches` is 0, go back to Step 1. If you've done `$max_batches` batches, stop. If Step 1 returned `ALL_DONE`, stop.

Final report format:
```
Batch N: classified=X empty=Y | total tracked=Z | [CONTINUING|ALL_DONE]
```

## Classification Rules

- Classify by **actual, real-world business activity confirmed via web search**
- **Must use web search** — do not guess from name fragments
- If search cannot confirm the business activity → `""`
- Generic PTY LTD, Holdings, Nominees with no public-facing business → `""`
- Trust accounts, super funds, shell companies, personal names + "Enterprises" → `""`
- `""` is valid — it marks "searched, nothing found" so the merchant is never re-extracted
- Add "Australia" to searches for Australian-looking names
- For "PTY LTD" names, search both with and without the suffix

## Valid Categories (exact, case-sensitive)

Automotive, Department Stores, Dining Out, Donations, Education, Entertainment, Financial Institutions, Gambling, Groceries, Gyms and other memberships, Health, Home Improvement, Information, Insurance, Personal Care, Pet Care, Rent, Retail, Subscription TV, Telecommunications, Transport, Travel, Utilities

**Quick reference:**
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

## Examples

| Merchant | Search finding | Category |
|----------|---------------|----------|
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
| J SMITH ENTERPRISES PTY LTD | Just a person's name, no real business | "" |

## Safety rules

- Only modify `category`, `category_source`, `category_updated_at` columns in the CSV
- Only write to `merchant_kb.csv`, `cache/web_classify_tracking.json`, and `cache/_batch_result.json`
- Read CSV with `utf-8-sig`, write with `utf-8`
- Do NOT modify the tracking file manually — only through the save script
- If the save script fails, report the error and stop — do not retry with different code
