---
name: classify-merchants
description: Use web search to classify uncategorized merchants in the merchant knowledge base
arguments:
  - name: split_file
    description: Name of the split file to process (e.g. merchant_kb_part_01.json). If omitted, shows status of all files.
    required: false
  - name: batch_size
    description: Number of merchants to process in this invocation (default 30)
    required: false
---

# Classify Merchants via Web Search

You are classifying merchants from a merchant knowledge base. Your job is to research each merchant using web search and assign the correct category from the 23-category enum.

## Workflow

### Step 1: Determine what to process

If `$split_file` is not provided by the user, run:
```bash
python classify_batch.py status
```
Show the user the progress of all split files and ask which one to work on.

### Step 2: Extract a batch

```bash
python classify_batch.py extract "knowledge-base-split/$split_file" --count $batch_size
```
This creates a batch file in `knowledge-base-web-classify/batches/`. Read that file.

### Step 3: Classify each merchant

For each merchant in the batch, do the following:

1. **Search the web** using WebSearch:
   ```
   "{merchant_name}" business type company what they do
   ```

2. **Analyze the search results**: What does this business actually do? Is it a real consumer-facing business?

3. **Determine the category** using the rules below.

4. **Write the category** into the item's `category` field.

### Step 4: Save results

After processing all merchants in the batch, write the updated JSON back to the same batch file.

### Step 5: Merge

```bash
python classify_batch.py merge "knowledge-base-split/$split_file" "knowledge-base-web-classify/batches/<batch_file>"
```

Show the user the progress after merge.

## Classification Rules

- Classify by **actual, real-world business activity**, not by name alone
- **Must use web search** — do not guess from name fragments
- If web search cannot confirm the business activity, return `""` (empty string)
- When search reveals the official website URL, include it in the `link` field
- Generic PTY LTD companies, nominee companies, holding companies with no public-facing business → empty category

## Valid Categories

| Category | Covers |
|----------|--------|
| Automotive | fuel, vehicles, repairs, parts, car washes, roadside services |
| Department Stores | large mixed-retail, discount, supercentre, department-store chains |
| Dining Out | restaurants, cafes, bars, fast food, food delivery, catering, prepared meals |
| Donations | charities, non-profits, fundraising, religious giving |
| Education | childcare, schools, universities, tutoring, training |
| Entertainment | cinemas, theatres, museums, attractions, events, clubs, music, games |
| Financial Institutions | banks, lenders, payment services, mortgages, securities, wealth, financial brokers |
| Gambling | casinos, betting, wagering, lotteries, gaming venues |
| Groceries | supermarkets, food shops, bakeries, butchers, seafood, liquor, bottle shops, food suppliers, wholesalers, processors |
| Gyms and other memberships | gyms, fitness, yoga, pilates, sports training, member clubs |
| Health | pharmacies, dentists, optometrists, clinics, hospitals, healthcare |
| Home Improvement | construction, trades, hardware, cleaning, repairs, maintenance, facilities, security, building and property services |
| Information | software, IT, computer services, data, media, publishing, digital information platforms |
| Insurance | insurers, brokers, policies, claims, warranties |
| Personal Care | hair, beauty, nails, spas, grooming, laundry, tailoring, consumer photography |
| Pet Care | vets, animal hospitals, pet shops, pet food, grooming, boarding |
| Rent | rent, leases, property managers, real estate agencies, rental agencies, storage |
| Retail | clothing, shoes, jewellery, books, florists, gifts, electronics, specialty goods |
| Subscription TV | cable, satellite, streaming TV, paid television |
| Telecommunications | mobile, phone, internet, broadband, network, telecom providers |
| Transport | public transport, taxis, rideshare, parking, tolls, freight, logistics, couriers, vehicle registration |
| Travel | hotels, holiday rentals, airlines, travel agencies, tours, cruises, car rental |
| Utilities | electricity, gas, water, waste, taxes, council rates, government fees, fines, public services |

## Empty category examples

These should get `""` because they are generic corporate entities, not consumer-facing businesses:
- "BENMIREN NOMINEES PTY LTD"
- "GOCUP PASTORAL PTY LTD"
- "TRANBERRY PTY LTD"
- "ABC Holdings Pty Ltd"
- "Smith Family Trust"

## Category assignment examples

| Merchant | Search finding | Category |
|----------|---------------|----------|
| Naked for Satan | Bar/restaurant in Melbourne | Dining Out |
| Cellarbrations Central Geraldton | Bottle shop / liquor store | Groceries |
| Blackburn Football Club | Local football/sports club | Entertainment |
| Chemist Warehouse | Pharmacy chain | Health |
| BWS | Beer Wine Spirits, bottle shop | Groceries |
| Uber | Rideshare and food delivery | Transport |
| Bunnings Warehouse | Hardware and home improvement | Home Improvement |
| Telstra | Telecommunications provider | Telecommunications |
| Netflix | Streaming TV service | Subscription TV |
| ATO (Australian Taxation Office) | Government tax agency | Utilities |
| DoorDash | Food delivery platform | Dining Out |
| Booking.com | Travel booking platform | Travel |

## Important notes

- Write the EXACT category strings — case-sensitive, must match the enum exactly
- After merging, always show the user the updated progress
- If a batch finishes and there are still unclassified merchants, let the user know they can invoke `/classify-merchants` again
- Process merchants in order — don't skip any
- If a web search returns no useful information after 2 attempts, classify as `""`
