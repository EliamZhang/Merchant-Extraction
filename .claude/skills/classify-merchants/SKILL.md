---
name: classify-merchants
description: Classify uncategorized merchants in merchant_kb.csv with web evidence, update confirmed categories, and track every searched merchant. Use when asked to batch-classify merchant names, fill blank category values, or continue merchant KB cleanup.
---

# Classify Merchants

Classify uncategorized merchants in `merchant_kb.csv` using conservative web evidence. Work in batches, keep the CSV intact, and mark every searched merchant so future runs do not repeat work.

Default batch size is 10. Default max batches is 1. If the user says unlimited, continue until no uncategorized and unsearched merchants remain.

## Files

- Read and update `merchant_kb.csv`.
- Read and update `cache/web_classify_tracking.json`.
- Use `cache/_batch_result.json` only as a temporary scratch file if helpful.
- Do not call project scripts.
- Only edit `category`, `category_source`, and `category_updated_at`.

## Workflow

1. Load `merchant_kb.csv`.
2. Load `cache/web_classify_tracking.json`; create it with `{"searched":[],"last_updated":""}` if missing.
3. Select the first batch of rows where `category` is blank and normalized `merchant_name` is not already searched.
4. Web-search every selected merchant.
5. Decide one valid category or `""` for each merchant.
6. Write confirmed categories back to the CSV:
   - Set `category` to the chosen category.
   - Set `category_source` to `web_search`.
   - Set `category_updated_at` to the current ISO timestamp.
7. Add every batch merchant to `searched`, including merchants classified as `""`.
8. Report `classified`, `empty`, `updated`, and total searched count.

Preserve all CSV columns, row order, encoding, and unrelated values.

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
