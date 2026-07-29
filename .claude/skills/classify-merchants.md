---
name: classify-merchants
description: Research and classify merchants with empty categories using web search
arguments:
  - name: count
    description: How many merchants to classify in this run
    required: false
    default: "20"
---

# classify-merchants

You are classifying merchants from `merchant_kb.csv` whose `category` field is empty. Do NOT use the batch LLM pipeline — this is a research-driven, one-at-a-time classification pass where each merchant gets a proper web search.

## Valid Categories

The category MUST be one of these (from `classify_merchants.py`):

```
Automotive, Department Stores, Dining Out, Donations, Education,
Entertainment, Financial Institutions, Gambling, Groceries,
Gyms and other memberships, Health, Home Improvement, Information,
Insurance, Personal Care, Pet Care, Rent, Retail, Subscription TV,
Telecommunications, Transport, Travel, Utilities
```

## Classification Guidelines

- Cafes, restaurants, pubs, bars, fast food, food delivery → **Dining Out**
- Bakeries, butchers, liquor stores, bottle shops, specialty food shops → **Groceries**
- Gas stations, car repair, auto parts, car washes, car dealers → **Automotive**
- Electricians, plumbers, roofers, painters, builders, hardware stores → **Home Improvement**
- Hair salons, barbers, nail salons, beauty salons, spas, dry cleaners → **Personal Care**
- Pharmacies, dentists, optometrists, physiotherapists, medical clinics → **Health**
- Hotels, motels, resorts → **Travel**
- Childcare, daycare, preschool, kindergarten → **Education**
- Parking, toll roads, vehicle registration, freight, courier → **Transport**
- Gyms, yoga studios, pilates, fitness centres → **Gyms and other memberships**
- Cinemas, theatres, museums, galleries, theme parks, sports clubs → **Entertainment**
- Clothing, shoes, jewellery, bookstores, florists, gift shops, newsagents, phone repair → **Retail**
- Banks, credit unions, lenders, mortgage providers, investment firms → **Financial Institutions**
- Tax office, council rates, government fees, public services → **Utilities**
- Accountants, consultants, architects, lawyers, marketing agencies → leave empty
- Property developers, real estate agencies, holding companies → leave empty
- Family trusts, nominees, superannuation funds, investment vehicles → leave empty
- Photography services → **Personal Care**
- Religious organizations, charities → **Donations**
- Veterinarians → **Pet Care**
- IT/web/software/SaaS companies → **Information**

## Workflow

### Step 1: Read the data

Read `merchant_kb.csv` and find rows where the `category` field is empty. The CSV has columns:
`merchant_name,keywords,link,category,keyword_updated_at,category_updated_at`

### Step 2: Pick merchants

From the empty-category rows, pick `$count` merchants that look classifiable. Skip:
- "PTY LTD" / "PTY. LTD." / "PTY LIMITED" shell companies with no keywords
- Family trusts, nominee companies, superannuation funds
- Generic holding/investment companies
- Rows where merchant_name and keywords are identical and generic

Prioritize merchants that have:
- A `link` pointing to a real website
- Descriptive keywords that suggest a real business
- Well-known brand names

### Step 3: Research each merchant

For EACH selected merchant:
1. Use `WebSearch` with the merchant name + any location hints from keywords
2. Read the search results to determine what the business actually does
3. If the first search is inconclusive, try a second search with different terms

### Step 4: Classify

Assign the best-fit category from the valid enum. If after thorough search the business type is still unclear, leave it empty. Document your reasoning.

### Step 5: Update the CSV

Use a Python script (write it, run it, then delete it) to update the CSV. The script must:
- Read `merchant_kb.csv`
- For each classified merchant, set `category` to the chosen value
- Set `category_updated_at` to the current time in `+08:00` timezone (format: `2026-07-29T17:26:31+08:00`)
- Preserve all existing data
- Handle UTF-8 BOM encoding

### Step 6: Report

Output a table of what was classified:

| # | Merchant | Category | Reasoning |
|---|----------|----------|-----------|

Also report: how many empty categories remain, and any merchants you intentionally skipped.
