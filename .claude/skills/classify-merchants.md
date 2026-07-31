---
name: classify-merchants
description: Use web search to classify uncategorized merchants in the merchant knowledge base. Runs autonomously — one batch per invocation.
arguments:
  - name: start_file
    description: Name of the split file to start with (e.g. merchant_kb_part_01.json). Auto-detects next file if omitted.
    required: false
  - name: batch_size
    description: Number of merchants to process per invocation (default 10)
    required: false
---

# Classify Merchants via Web Search

You are an autonomous batch processor. Every time this skill is invoked, you process ONE batch of merchants, then report the result. Do NOT ask the user questions — just run.

## Workflow (do all steps, no pausing)

### Step 1: Pick the file to work on

If `$start_file` is provided, use it. Otherwise find the next file with remaining work:
```bash
python classify_batch.py next-file
```
If it prints `ALL_DONE`, report "All 20 files complete." and stop.

### Step 2: Extract a batch of $batch_size (default 10) merchants

```bash
python classify_batch.py extract "knowledge-base-split/$start_file" --count $batch_size
```
If it says "All merchants are already classified or processed!", go back to Step 1 and pick the next file instead.

Read the batch file from `knowledge-base-web-classify/batches/`.

### Step 3: Search and classify each merchant

For every merchant in the batch (process ALL of them):
1. WebSearch: `"{merchant_name}" business type company what they do`
2. Read the search results. Is this a real consumer-facing business? What industry?
3. Assign the correct category from the enum below, or `""` if unconfirmable.

### Step 4: Save and merge

Write the updated JSON back to the same batch file, then:
```bash
python classify_batch.py merge "knowledge-base-split/$start_file" "knowledge-base-web-classify/batches/<batch_file>"
```

### Step 5: Report

Output exactly ONE line summarizing the batch:
- How many classified vs empty
- Current file progress %
- If the file is fully done, say "FILE_DONE" and the next file name

## Classification Rules

- Classify by **actual, real-world business activity**, not by name alone
- **Must use web search** — do not guess from name fragments
- If search cannot confirm the business activity, return `""` (empty string)
- Generic PTY LTD, Enterprises, Holdings, Nominees with no public-facing business → `""`
- `""` is a VALID result — you are marking it "searched but nothing found", which prevents re-extraction

## Valid Categories (exact strings, case-sensitive)

Automotive, Department Stores, Dining Out, Donations, Education, Entertainment, Financial Institutions, Gambling, Groceries, Gyms and other memberships, Health, Home Improvement, Information, Insurance, Personal Care, Pet Care, Rent, Retail, Subscription TV, Telecommunications, Transport, Travel, Utilities

**Category guide:**
- Automotive: fuel, vehicles, repairs, parts, car washes, roadside services
- Department Stores: large mixed-retail, discount, supercentre, department-store chains
- Dining Out: restaurants, cafes, bars, fast food, food delivery, catering, prepared meals
- Donations: charities, non-profits, fundraising, religious giving
- Education: childcare, schools, universities, tutoring, training
- Entertainment: cinemas, theatres, museums, attractions, events, clubs, music, games
- Financial Institutions: banks, lenders, payment services, mortgages, securities, wealth, financial brokers
- Gambling: casinos, betting, wagering, lotteries, gaming venues
- Groceries: supermarkets, food shops, bakeries, butchers, seafood, liquor, bottle shops, food suppliers, wholesalers, processors
- Gyms and other memberships: gyms, fitness, yoga, pilates, sports training, member clubs
- Health: pharmacies, dentists, optometrists, clinics, hospitals, healthcare
- Home Improvement: construction, trades, hardware, cleaning, repairs, maintenance, facilities, security, building and property services
- Information: software, IT, computer services, data, media, publishing, digital information platforms
- Insurance: insurers, brokers, policies, claims, warranties
- Personal Care: hair, beauty, nails, spas, grooming, laundry, tailoring, consumer photography
- Pet Care: vets, animal hospitals, pet shops, pet food, grooming, boarding
- Rent: rent, leases, property managers, real estate agencies, rental agencies, storage
- Retail: clothing, shoes, jewellery, books, florists, gifts, electronics, specialty goods
- Subscription TV: cable, satellite, streaming TV, paid television
- Telecommunications: mobile, phone, internet, broadband, network, telecom providers
- Transport: public transport, taxis, rideshare, parking, tolls, freight, logistics, couriers, vehicle registration
- Travel: hotels, holiday rentals, airlines, travel agencies, tours, cruises, car rental
- Utilities: electricity, gas, water, waste, taxes, council rates, government fees, fines, public services

## Empty category triggers

Return `""` for:
- Generic corporate entities: "XYZ Pty Ltd", "ABC Holdings", "DEF Enterprises", "GHI Nominees"
- Trust accounts, super funds, shell companies
- Names that are just people's names + "Enterprises"
- Any merchant where web search returns zero relevant results

## Category assignment examples

| Merchant | Search finding | Category |
|----------|---------------|----------|
| Naked for Satan | Bar/restaurant in Melbourne | Dining Out |
| Cellarbrations | Bottle shop / liquor store | Groceries |
| Blackburn Football Club | Local football/sports club | Entertainment |
| Chemist Warehouse | Pharmacy chain | Health |
| BWS | Beer Wine Spirits bottle shop | Groceries |
| Uber | Rideshare platform | Transport |
| Bunnings Warehouse | Hardware store chain | Home Improvement |
| Telstra | Telco provider | Telecommunications |
| Netflix | Streaming TV | Subscription TV |
| BENMIREN NOMINEES PTY LTD | Generic corporate entity | "" |
| GOCUP PASTORAL PTY LTD | No public business | "" |

## Search tips

- Add "Australia" to searches for merchants that look Australian
- For "PTY LTD" names, search both with and without the suffix
- If first search is inconclusive, try a second search with different keywords
- If still nothing after 2 searches, mark as `""`
