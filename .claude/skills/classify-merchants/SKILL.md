---
name: classify-merchants
description: Classify uncategorized merchants in merchant_kb.csv with web evidence, update confirmed categories, and track every searched merchant. Use when asked to batch-classify merchant names, fill blank category values, or continue merchant KB cleanup.
arguments:
  - name: batch_size
    description: Number of merchants to process per invocation (default 10)
    required: false
  - name: max_batches
    description: Maximum batches to run before stopping (default 1). Set to 0 for unlimited.
    required: false
---

# Classify Merchants

Classify uncategorized merchants in `merchant_kb.csv` using conservative web evidence. Work in batches, keep the CSV intact, and mark every searched merchant so future runs do not repeat work.

`$batch_size` defaults to 10. `$max_batches` defaults to 1. If `$max_batches` is 0, keep running until there are no uncategorized and unsearched merchants.

## Files

- Read and update `merchant_kb.csv`.
- Read and update `cache/web_classify_tracking.json`.
- Use `cache/_batch_result.json` only as a temporary scratch file if helpful.
- Do not call project scripts.
- Only edit `category`, `category_source`, and `category_updated_at`.

## Workflow

Run the steps below as a loop, up to `$max_batches` batches.

### Step 1: Find Next Batch

Ensure tracking exists:

```bash
python -c "import json, os; os.makedirs('cache', exist_ok=True); p='cache/web_classify_tracking.json'; (not os.path.exists(p)) and json.dump({'searched':[],'last_updated':''}, open(p,'w',encoding='utf-8'), ensure_ascii=False, indent=2)"
```

Find uncategorized merchants not already searched:

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

If output is `ALL_DONE`, stop and report `All done - every merchant has a category or has been searched.`

### Step 2: Search and Classify

Web-search every selected merchant.

## Search Rules

- Treat the KB as global; do not assume a country.
- Search the exact merchant name first.
- In the same search round, try a cleaned name without legal suffixes if the exact name is too noisy. Common suffixes include `Inc`, `LLC`, `Ltd`, `Pty Ltd`, `GmbH`, `BV`, `Pte Ltd`, `Holdings`, `Group`, `Trust`, `Trustee`, and `Nominees`.
- Use CSV `keywords` and `link` fields as hints, not automatic proof.
- Use one search round per merchant unless the user explicitly asks for deeper research.
- If one round does not produce enough evidence, return `""` and move on.

## Decision Rules

Classify only when both are true:

- The source reasonably matches the merchant, legal entity, trading name, or brand.
- The source shows real business activity that maps to exactly one valid category.

Use `""` when evidence is weak, registry-only, passive, ambiguous, conflicting, or only based on name fragments.

Reliable evidence includes official sites, business registries with useful activity/trading-name detail, maps, reputable directories, store lists, shopping-center tenant pages, filings, PDFs, and news. One strong source is enough; otherwise use two independent sources that agree.

Same-name businesses count only when location, brand, registration detail, website, or other context supports the match.

## Valid Categories

Use exact spelling:

```text
Automotive
Department Stores
Dining Out
Donations
Education
Entertainment
Financial Institutions
Gambling
Groceries
Gyms and other memberships
Health
Home Improvement
Information
Insurance
Personal Care
Pet Care
Rent
Retail
Subscription TV
Telecommunications
Transport
Travel
Utilities
```

## Quick Mapping

- Automotive: fuel, vehicles, repairs, parts, car wash, roadside assistance.
- Department Stores: large mixed retail, discount stores, supercenters.
- Dining Out: restaurants, cafes, bars, takeaway, food delivery, catering, prepared meals.
- Donations: charities, nonprofits, religious giving, fundraising.
- Education: childcare, schools, universities, tutoring, training.
- Entertainment: cinemas, theaters, museums, events, attractions, clubs, music, games, sports clubs.
- Financial Institutions: banks, lenders, payment services, mortgages, brokers, wealth, securities.
- Gambling: casinos, betting, wagering, lotteries, gaming venues.
- Groceries: supermarkets, liquor stores, bakeries, butchers, seafood, food suppliers, wholesalers, processors.
- Gyms and other memberships: gyms, fitness studios, yoga, pilates, sports training, member clubs.
- Health: pharmacies, dentists, optometrists, clinics, hospitals, healthcare.
- Home Improvement: construction, trades, hardware, cleaning, repairs, maintenance, facilities, security, building services.
- Information: software, IT, computer services, data, media, publishing, digital platforms.
- Insurance: insurers, brokers, policies, claims, warranties.
- Personal Care: hair, beauty, nails, spas, grooming, laundry, tailoring, consumer photography.
- Pet Care: vets, animal hospitals, pet shops, pet food, grooming, boarding.
- Rent: rent, leases, property managers, real estate agencies, storage.
- Retail: clothing, shoes, jewelry, books, florists, gifts, electronics, specialty goods.
- Subscription TV: cable, satellite, streaming TV, paid television.
- Telecommunications: mobile, phone, internet, broadband, network, telecom providers.
- Transport: public transport, taxis, rideshare, parking, tolls, freight, logistics, couriers, vehicle registration.
- Travel: hotels, holiday rentals, airlines, travel agencies, tours, cruises, car rental.
- Utilities: electricity, gas, water, waste, taxes, council rates, government fees, fines, public services.

## Tie-Breakers

- Food retail is `Groceries`; prepared food is `Dining Out`.
- Pharmacies are `Health`.
- Hardware, trades, cleaning, maintenance, and security installation are `Home Improvement`.
- Passive holding, investment, nominee, trustee, and shell entities are `""` unless they operate a customer-facing service.
- Sports clubs are `Entertainment`; fitness studios and gyms are `Gyms and other memberships`.
- Streaming TV is `Subscription TV`; SaaS, media platforms, and general digital services are `Information`.
- Government fees, taxes, council rates, and fines are `Utilities`.

## Final Check

Before finishing, confirm that every batch merchant was searched, every non-empty category is valid, weak matches are `""`, the CSV was saved successfully, and the tracking file includes every searched name.

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

- Category values must exactly match the valid category list.
- Empty string means searched but no category was confirmed.
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
