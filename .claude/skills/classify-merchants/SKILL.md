---
name: classify-merchants
description: Fast, evidence-based web-search classification of uncategorized merchants in merchant_kb.csv for a global merchant/company database. Self-contained: reads CSV directly, tracks searched merchants, and writes results back. No external script dependencies.
arguments:
  - name: batch_size
    description: Number of merchants to process per invocation (default 10)
    required: false
  - name: max_batches
    description: Maximum batches to run before stopping (default 1). Set to 0 for unlimited.
    required: false
---

# Classify Merchants via Web Search

Autonomously classify uncategorized merchants in `merchant_kb.csv` using web evidence. Each invocation selects a batch, searches, writes confirmed categories, marks every searched merchant as searched, then reports a concise summary.

Do not ask questions during normal runs. Use conservative judgment and keep moving.

## Core Rules

- Treat the KB as global. Do not assume merchants are Australian, American, or from any single country.
- Classify by confirmed real-world business activity, not by name fragments alone.
- Use web search for every merchant. If evidence is weak or conflicting, return `""`.
- A result is classifiable only when both are true:
  - The source reasonably matches the searched merchant, legal entity, trading name, or brand.
  - The source shows business activity that maps to exactly one valid category.
- Reliable evidence can come from any country: official websites, business registries, maps, reputable directories, franchise/store lists, shopping-center tenant pages, regulatory filings, PDFs, or news.
- Same-name businesses in other countries count only when the name, location, registration detail, brand, or other context supports the match.
- Empty string means searched but not confidently classifiable.

## Files

- Read and update `merchant_kb.csv`.
- Track searched names in `cache/web_classify_tracking.json`.
- Temporarily write batch results to `cache/_batch_result.json`.
- Do not call project scripts.
- Only modify `category`, `category_source`, and `category_updated_at` in the CSV.

## Workflow

### Step 0: Parse Arguments

`$batch_size` defaults to 10. `$max_batches` defaults to 1. If `$max_batches` is 0, keep running until there are no uncategorized and unsearched merchants.

Run Steps 1-4 as a loop, up to `$max_batches` batches.

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

If output is `ALL_DONE`, stop and report:

```text
All done - every merchant has a category or has been searched.
```

### Step 2: Search and Classify

Process the batch as one unit. Run searches for different merchants in parallel whenever the environment supports it.

For each merchant:

1. Search the exact name.
2. Search a cleaned name without common legal suffixes when useful.
3. Use the CSV `keywords` and `link` fields as hints when helpful, but not as automatic proof.
4. Add country, city, registry number, brand, or industry terms only when found in the CSV or search results.
5. Run a second search round only when the first round finds a plausible lead, such as a trading name, possible brand, location, registry page, or industry hint.
6. Stop after two rounds.

Common legal suffixes to ignore for search variants include global company endings and passive-entity words:

```text
Inc, Incorporated, LLC, LLP, LP, Ltd, Limited, PLC, Corp, Corporation, Co,
Company, Pty Ltd, Proprietary Limited, GmbH, AG, SA, SAS, SARL, BV, NV,
SpA, SRL, SL, AB, AS, Oy, A/S, Pte Ltd, SDN BHD, BHD, KK, GK, Kft,
Holdings, Holding, Group, Investments, Nominees, Trustee, Trust
```

Use these simple decisions:

- **Classify** when a reliable source identifies the merchant or its trading brand and shows activity matching one valid category.
- **Classify** when two independent sources converge on the same merchant and activity, even if neither source is perfect alone.
- **Return `""`** when results only prove a legal entity exists, when the entity appears passive, when the activity is unclear, when multiple same-name businesses conflict, or when no category fits cleanly.

Examples of enough evidence:

- Official site shows the brand activity and the name matches the merchant.
- Registry shows a trading name, and the trading name's site/listing confirms activity.
- Map or reputable directory links the name to an operating location with clear activity.
- Franchise, store, tenant, filing, PDF, or news source links the legal entity to a brand, and another source confirms the brand activity.

Examples of not enough evidence:

- Registry-only pages with no trading name or operating activity.
- A brand with no source linking it to the searched merchant.
- Social media or low-quality directory snippets without corroboration.
- Passive holding, investment, nominee, trustee, or property ownership entities with no customer-facing operation.
- A generic personal or shell-company name with no public business.

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

Then run:

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

### Step 4: Report

Report after each batch:

```text
Batch N: classified=X empty=Y updated=U | total tracked=Z | [CONTINUING|ALL_DONE]
```

If `$max_batches` is 0, return to Step 1. Otherwise stop after the configured number of batches.

## Valid Categories

Exact, case-sensitive:

```text
Automotive, Department Stores, Dining Out, Donations, Education, Entertainment,
Financial Institutions, Gambling, Groceries, Gyms and other memberships, Health,
Home Improvement, Information, Insurance, Personal Care, Pet Care, Rent, Retail,
Subscription TV, Telecommunications, Transport, Travel, Utilities
```

Quick mapping:

- Automotive: fuel, vehicles, repairs, parts, car washes, roadside assistance.
- Department Stores: large mixed retail, discount stores, supercenters.
- Dining Out: restaurants, cafes, bars, fast food, food delivery, catering, prepared meals.
- Donations: charities, nonprofits, fundraising, religious giving.
- Education: childcare, schools, universities, tutoring, training.
- Entertainment: cinemas, theaters, museums, attractions, events, clubs, music, games, sports clubs.
- Financial Institutions: banks, lenders, payment services, mortgages, securities, wealth, brokers.
- Gambling: casinos, betting, wagering, lotteries, gaming venues.
- Groceries: supermarkets, food shops, bakeries, butchers, seafood, liquor, food suppliers, wholesalers, processors.
- Gyms and other memberships: gyms, fitness, yoga, pilates, sports training, member clubs.
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

Tie-breakers:

- Bottle shops, liquor stores, bakeries, butchers, seafood shops, and food wholesalers: `Groceries`.
- Cafes, restaurants, bars, catering, take-away, prepared meals: `Dining Out`.
- Pharmacies: `Health`, even when they sell general retail goods.
- Hardware, building supplies, trades, cleaners, maintenance, security installers: `Home Improvement`.
- Passive investment, property holding, nominee, trustee, or shell entities: `""` unless they operate a customer-facing service.
- Sports clubs: `Entertainment`; fitness studios and gyms: `Gyms and other memberships`.
- Streaming TV: `Subscription TV`; general software, SaaS, and media platforms: `Information`.
- Council rates, fines, taxes, government fees: `Utilities`.

## Final Self-Check

Before saving, verify:

- Every batch merchant appears exactly once in `cache/_batch_result.json`.
- Every non-empty category is from the valid list.
- Each non-empty category has evidence for both entity match and activity.
- Weak, passive, ambiguous, registry-only, or conflicting results are `""`.
- The save script completed successfully.
