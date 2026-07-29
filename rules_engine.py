#!/usr/bin/env python3
"""
Update an existing merchant KB using high-confidence classification rules.

Behaviour
---------
1. Only rows with an empty ``category`` are evaluated.
2. Only a clear, high-confidence winner is written back to the KB.
3. Conflicting, excluded and unmatched rows are left unchanged.
4. Existing categories and their source fields are always preserved.
5. By default, ``merchant_kb.csv`` is updated atomically in place.
6. No review queue, audit file or uncertain recommendation is produced.

Examples
--------
Preview the high-confidence updates without changing the KB:
    python classify_by_rules_high_confidence.py --dry-run --verbose

Update merchant_kb.csv directly:
    python classify_by_rules_high_confidence.py

Use another KB path:
    python classify_by_rules_high_confidence.py --merchant-kb data/merchant_kb.csv

High-confidence means an exact-name, exact-domain or curated strong-phrase
rule matches. If more than one category matches, the row is treated as a
conflict and left unchanged.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from datetime import datetime


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_MERCHANT_KB = Path("merchant_kb.csv")
KEYWORD_SEPARATOR = "|"
RULE_VERSION = "merchant_rules_v3.1_exact_strong_only_20260729"

ALLOWED_CATEGORIES = {
    "Automotive",
    "Department Stores",
    "Dining Out",
    "Donations",
    "Education",
    "Entertainment",
    "Financial Institutions",
    "Gambling",
    "Groceries",
    "Gyms and other memberships",
    "Health",
    "Home Improvement",
    "Information",
    "Insurance",
    "Personal Care",
    "Pet Care",
    "Rent",
    "Retail",
    "Subscription TV",
    "Telecommunications",
    "Transport",
    "Travel",
    "Utilities",
}

REQUIRED_INPUT_FIELDS = [
    "merchant_name",
    "keywords",
    "link",
    "category",
    "keyword_updated_at",
    "category_updated_at",
]

DEFAULT_OUTPUT_FIELDS = [
    "merchant_name",
    "keywords",
    "link",
    "category",
    "category_source",
    "keyword_updated_at",
    "category_updated_at",
]


# Only curated high-confidence evidence is supported: exact merchant names,
# exact domains and strong phrases. There is no numeric ranking or threshold.

BUSINESS_SUFFIXES = (
    "proprietary limited",
    "pty limited",
    "pty ltd",
    "limited",
    "pty",
    "ltd",
    "incorporated",
    "inc",
    "llc",
    "plc",
)

# These are transaction descriptors rather than useful merchant identities.
# They are skipped before rule evaluation.
GLOBAL_SKIP_PHRASES = (
    "internal transfer",
    "external transfer",
    "bank transfer",
    "account transfer",
    "cash withdrawal",
    "cash deposit",
    "atm withdrawal",
    "direct debit fee",
    "dishonour fee",
    "dishonored fee",
    "dishonoured fee",
    "late payment fee",
    "credit card repayment",
    "loan repayment",
)


# =============================================================================
# Data models
# =============================================================================


@dataclass(frozen=True)
class Rule:
    """One merchant-classification rule.

    ``exact_names``
        Exact normalized merchant names. Highest-confidence evidence.

    ``exact_domains``
        Exact website hostnames or hostname suffixes.

    ``strong``
        Highly indicative phrases. A merchant-name match is usually enough.

    ``required_any``
        At least one phrase must appear anywhere in the merchant context.

    ``required_all``
        Every phrase must appear somewhere in the merchant context.

    ``excludes``
        If any phrase appears, this rule is disabled for the row.
    """

    name: str
    category: str
    exact_names: tuple[str, ...] = ()
    exact_domains: tuple[str, ...] = ()
    strong: tuple[str, ...] = ()
    required_any: tuple[str, ...] = ()
    required_all: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MerchantContext:
    merchant_name_raw: str
    name: str
    name_core: str
    name_compact: str
    keyword_items: tuple[str, ...]
    keywords_text: str
    hostname: str
    domain_text: str
    combined_text: str
    name_search: str
    keywords_search: str
    domain_search: str
    combined_search: str
    tokens: frozenset[str]


@dataclass(frozen=True)
class Evidence:
    field: str
    phrase: str
    strength: str

    def display(self) -> str:
        return f"{self.field}:{self.phrase}({self.strength})"


@dataclass(frozen=True)
class RuleMatch:
    rule_name: str
    category: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class CategoryCandidate:
    category: str
    rule_matches: tuple[RuleMatch, ...]

    @property
    def matched_rules(self) -> tuple[str, ...]:
        return tuple(match.rule_name for match in self.rule_matches)

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        seen: set[tuple[str, str, str]] = set()
        result: list[Evidence] = []
        for match in self.rule_matches:
            for item in match.evidence:
                key = (item.field, item.phrase, item.strength)
                if key not in seen:
                    seen.add(key)
                    result.append(item)
        return tuple(result)


@dataclass(frozen=True)
class Decision:
    status: str
    category: str = ""
    winning_rule: str = ""
    candidates: tuple[CategoryCandidate, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    conflict: bool = False


# =============================================================================
# Rules
# =============================================================================


def R(
    name: str,
    category: str,
    *,
    exact_names: Sequence[str] = (),
    exact_domains: Sequence[str] = (),
    strong: Sequence[str] = (),
    required_any: Sequence[str] = (),
    required_all: Sequence[str] = (),
    excludes: Sequence[str] = (),
) -> Rule:
    """Compact rule-construction helper."""

    return Rule(
        name=name,
        category=category,
        exact_names=tuple(exact_names),
        exact_domains=tuple(exact_domains),
        strong=tuple(strong),
        required_any=tuple(required_any),
        required_all=tuple(required_all),
        excludes=tuple(excludes),
    )


RULES: tuple[Rule, ...] = (
    # -------------------------------------------------------------------------
    # Pet Care
    # -------------------------------------------------------------------------
    R(
        "pet_veterinary",
        "Pet Care",
        strong=(
            "veterinary",
            "veterinarian",
            "vet clinic",
            "vet hospital",
            "animal hospital",
            "animal clinic",
            "pet hospital",
            "pet medical",
            "animal surgery",
        ),
    ),
    R(
        "pet_grooming",
        "Pet Care",
        strong=(
            "pet grooming",
            "dog grooming",
            "cat grooming",
            "mobile dog wash",
            "dog wash",
        ),
    ),
    R(
        "pet_boarding",
        "Pet Care",
        strong=(
            "pet boarding",
            "dog boarding",
            "cat boarding",
            "boarding kennel",
            "dog kennel",
            "cattery",
            "dog daycare",
            "pet daycare",
        ),
    ),
    R(
        "pet_retail",
        "Pet Care",
        strong=(
            "pet store",
            "pet shop",
            "pet supplies",
            "pet food",
            "aquarium supplies",
        ),
    ),

    # -------------------------------------------------------------------------
    # Education
    # -------------------------------------------------------------------------
    R(
        "education_childcare",
        "Education",
        strong=(
            "childcare",
            "child care",
            "daycare",
            "day care",
            "family day care",
            "early learning centre",
            "early learning center",
            "before school care",
            "after school care",
            "outside school hours care",
            "vacation care",
        ),
    ),
    R(
        "education_school",
        "Education",
        strong=(
            "kindergarten",
            "preschool",
            "pre school",
            "primary school",
            "secondary school",
            "high school",
            "grammar school",
            "catholic school",
            "christian school",
            "anglican school",
            "lutheran school",
            "public school",
            "montessori school",
        ),
    ),
    R(
        "education_tertiary",
        "Education",
        exact_names=("tafe",),
        strong=(
            "university",
            "technical college",
            "vocational education",
            "training institute",
            "tafe college",
        ),
    ),
    R(
        "education_tutoring",
        "Education",
        strong=(
            "tutoring",
            "tuition centre",
            "tuition center",
            "learning centre",
            "learning center",
            "maths tuition",
            "english tuition",
        ),
    ),
    R(
        "education_specialist_school",
        "Education",
        strong=(
            "driving school",
            "driving academy",
            "driver training",
            "language school",
            "music school",
            "dance school",
            "swim school",
            "swimming school",
        ),
    ),

    # -------------------------------------------------------------------------
    # Home Improvement
    # -------------------------------------------------------------------------
    R(
        "home_plumbing",
        "Home Improvement",
        strong=("plumber", "plumbing", "drainage contractor", "blocked drain"),
    ),
    R(
        "home_electrical",
        "Home Improvement",
        strong=("electrician", "electrical contractor", "electrical services"),
        excludes=("auto electrical", "automotive electrical"),
    ),
    R(
        "home_building_trades",
        "Home Improvement",
        strong=(
            "building contractor",
            "home builder",
            "residential builder",
            "bricklayer",
            "bricklaying",
            "carpenter",
            "carpentry",
            "plasterer",
            "plastering",
            "gyprock",
            "concreter",
            "concreting",
            "scaffolding",
            "roofing",
            "roofer",
            "roof restoration",
            "roof repairs",
        ),
    ),
    R(
        "home_surfaces",
        "Home Improvement",
        strong=(
            "tiling contractor",
            "floor sanding",
            "floor sander",
            "timber flooring",
            "carpet installer",
            "flooring contractor",
            "glazier",
            "glass glazing",
        ),
    ),
    R(
        "home_landscape_garden",
        "Home Improvement",
        strong=(
            "landscaper",
            "landscaping",
            "landscape supplies",
            "garden centre",
            "garden center",
            "garden supplies",
            "garden nursery",
            "lawn mowing",
            "tree services",
            "arborist",
        ),
    ),
    R(
        "home_pest",
        "Home Improvement",
        strong=("pest control", "termite control", "termite inspection"),
    ),
    R(
        "home_renovation",
        "Home Improvement",
        strong=(
            "bathroom renovation",
            "kitchen renovation",
            "home renovation",
            "renovation builder",
            "cabinet maker",
            "cabinetmaker",
            "kitchen cabinets",
        ),
    ),
    R(
        "home_painting",
        "Home Improvement",
        strong=("painting contractor", "house painter", "painting service"),
        excludes=("artist", "portrait", "gallery"),
    ),
    R(
        "home_fencing",
        "Home Improvement",
        strong=("fencing contractor", "fence installer", "pool fencing"),
        excludes=("fencing club", "fencing academy", "fencing sport"),
    ),
    R(
        "home_hardware",
        "Home Improvement",
        exact_names=("bunnings", "bunnings warehouse", "mitre 10", "home hardware"),
        exact_domains=("bunnings.com.au",),
        strong=("hardware store", "building supplies", "building materials"),
        required_any=(
            "tools",
            "building supplies",
            "building materials",
            "paint",
            "timber",
            "plumbing supplies",
            "garden supplies",
        ),
        excludes=("computer hardware", "software", "information technology"),
    ),

    # -------------------------------------------------------------------------
    # Automotive
    # -------------------------------------------------------------------------
    R(
        "automotive_mechanical",
        "Automotive",
        strong=(
            "auto repair",
            "auto repairs",
            "automotive repair",
            "mechanical repair",
            "mechanical repairs",
            "motor mechanic",
            "car mechanic",
            "vehicle servicing",
            "car servicing",
            "logbook service",
        ),
    ),
    R(
        "automotive_smash_repair",
        "Automotive",
        strong=(
            "smash repair",
            "smash repairs",
            "panel beating",
            "panel beater",
            "collision repair",
            "body repair shop",
        ),
    ),
    R(
        "automotive_tyres",
        "Automotive",
        strong=(
            "tyre service",
            "tyre centre",
            "tyre center",
            "tyre shop",
            "tyre and auto",
            "wheel alignment",
        ),
    ),
    R(
        "automotive_specialist",
        "Automotive",
        strong=(
            "auto electrician",
            "auto electrical",
            "automotive electrical",
            "muffler shop",
            "exhaust centre",
            "exhaust service",
            "auto glass",
            "windscreen repair",
            "windshield repair",
            "car detailing",
            "car detailer",
            "car wash",
            "tow truck",
            "towing service",
            "tilt tray",
        ),
    ),
    R(
        "automotive_dealer_parts",
        "Automotive",
        strong=(
            "car dealer",
            "motor dealer",
            "vehicle dealer",
            "used cars",
            "motorcycle dealer",
            "auto parts",
            "automotive parts",
            "car accessories",
        ),
    ),
    R(
        "automotive_fuel",
        "Automotive",
        strong=("petrol station", "fuel station", "service station"),
    ),

    # -------------------------------------------------------------------------
    # Personal Care
    # -------------------------------------------------------------------------
    R(
        "personal_hair",
        "Personal Care",
        strong=(
            "barber shop",
            "barbershop",
            "hairdresser",
            "hairdressing",
            "hair salon",
            "hair studio",
        ),
    ),
    R(
        "personal_beauty",
        "Personal Care",
        strong=(
            "beauty salon",
            "beauty spa",
            "beauty therapist",
            "nail salon",
            "nail bar",
            "nail spa",
            "waxing salon",
            "laser hair removal",
            "tanning salon",
            "brow bar",
            "eyelash studio",
        ),
    ),
    R(
        "personal_massage",
        "Personal Care",
        strong=("massage centre", "massage center", "massage spa", "massage therapy"),
        excludes=("physiotherapy", "chiropractic", "medical", "hospital"),
    ),
    R(
        "personal_laundry",
        "Personal Care",
        strong=(
            "dry cleaner",
            "dry cleaning",
            "laundromat",
            "coin laundry",
            "laundrette",
        ),
    ),
    R(
        "personal_tattoo",
        "Personal Care",
        strong=("tattoo studio", "tattoo parlour", "tattoo parlor", "body piercing"),
    ),

    # -------------------------------------------------------------------------
    # Groceries
    # -------------------------------------------------------------------------
    R(
        "grocery_supermarket_brands",
        "Groceries",
        exact_names=(
            "woolworths",
            "woolworths supermarket",
            "coles",
            "coles supermarket",
            "aldi",
            "iga",
            "iga supermarket",
            "costco",
            "foodworks",
            "drakes supermarket",
            "harris farm markets",
        ),
        exact_domains=(
            "woolworths.com.au",
            "coles.com.au",
            "aldi.com.au",
            "iga.com.au",
            "costco.com.au",
        ),
    ),
    R(
        "grocery_supermarket",
        "Groceries",
        strong=(
            "supermarket",
            "grocery store",
            "food market",
            "convenience store",
            "asian grocery",
            "indian grocery",
        ),
    ),
    R(
        "grocery_fresh_food",
        "Groceries",
        strong=(
            "butcher shop",
            "butchery",
            "fishmonger",
            "fish market",
            "fruit market",
            "vegetable market",
            "fruit and veg",
            "greengrocer",
            "delicatessen",
        ),
        excludes=("restaurant", "cafe", "takeaway"),
    ),
    R(
        "grocery_liquor",
        "Groceries",
        exact_names=(
            "liquorland",
            "bws",
            "dan murphys",
            "dan murphy's",
            "cellarbrations",
            "first choice liquor",
            "vintage cellars",
        ),
        strong=("liquor store", "bottle shop", "bottleshop"),
    ),
    R(
        "grocery_bakery",
        "Groceries",
        strong=("retail bakery", "bread shop", "artisan bakery"),
        excludes=("bakery cafe", "cafe", "restaurant", "coffee"),
    ),

    # -------------------------------------------------------------------------
    # Gyms and other memberships
    # -------------------------------------------------------------------------
    R(
        "gym_brands",
        "Gyms and other memberships",
        exact_names=(
            "anytime fitness",
            "fitness first",
            "snap fitness",
            "f45",
            "f45 training",
            "body fit training",
            "bft",
            "plus fitness",
            "jetts fitness",
        ),
    ),
    R(
        "gym_general",
        "Gyms and other memberships",
        strong=(
            "fitness centre",
            "fitness center",
            "fitness studio",
            "health club",
            "gymnasium",
            "personal training studio",
        ),
        excludes=("fitness equipment", "fitness clothing", "fitness retailer"),
    ),
    R(
        "gym_studio",
        "Gyms and other memberships",
        strong=(
            "pilates studio",
            "yoga studio",
            "yoga centre",
            "crossfit gym",
            "boxing gym",
            "boxing club",
            "martial arts",
            "karate school",
            "taekwondo",
            "jiu jitsu",
            "judo club",
            "kung fu",
        ),
    ),
    R(
        "gym_membership",
        "Gyms and other memberships",
        strong=("membership club", "sports club membership", "recreation centre membership"),
    ),

    # -------------------------------------------------------------------------
    # Gambling
    # -------------------------------------------------------------------------
    R(
        "gambling_brands",
        "Gambling",
        exact_names=(
            "sportsbet",
            "ladbrokes",
            "bet365",
            "betfair",
            "pointsbet",
            "unibet",
            "neds",
            "tabcorp",
            "tab",
            "bluebet",
        ),
        strong=(
            "sports betting",
            "betting agency",
            "betting shop",
            "online betting",
            "wagering",
        ),
    ),
    R(
        "gambling_lottery",
        "Gambling",
        strong=(
            "lottery ticket",
            "lotteries",
            "oz lotto",
            "powerball",
            "ozlotteries",
        ),
    ),
    R(
        "gambling_casino",
        "Gambling",
        strong=("online casino", "casino gaming", "poker machines", "pokies"),
        excludes=("casino hotel", "casino resort"),
    ),

    # -------------------------------------------------------------------------
    # Insurance
    # -------------------------------------------------------------------------
    R(
        "insurance_general",
        "Insurance",
        strong=(
            "insurance company",
            "insurance broker",
            "insurance agency",
            "insurance services",
            "general insurance",
            "life insurance",
            "car insurance",
            "home insurance",
            "health insurance",
            "income protection insurance",
            "underwriting agency",
        ),
    ),
    R(
        "insurance_brands",
        "Insurance",
        exact_names=(
            "aami",
            "allianz insurance",
            "nrma insurance",
            "budget direct insurance",
            "youi insurance",
            "qbe insurance",
            "racv insurance",
            "racq insurance",
        ),
    ),

    # -------------------------------------------------------------------------
    # Telecommunications
    # -------------------------------------------------------------------------
    R(
        "telecom_brands",
        "Telecommunications",
        exact_names=(
            "telstra",
            "optus",
            "vodafone",
            "aussie broadband",
            "superloop",
            "tpg",
            "tpg telecom",
            "belong mobile",
            "amaysim",
            "boost mobile",
            "dodo internet",
            "iinet",
        ),
        exact_domains=(
            "telstra.com.au",
            "optus.com.au",
            "vodafone.com.au",
            "aussiebroadband.com.au",
        ),
        excludes=("optus sport",),
    ),
    R(
        "telecom_general",
        "Telecommunications",
        strong=(
            "telecommunications provider",
            "telecom provider",
            "mobile phone service",
            "internet service provider",
            "broadband provider",
            "nbn provider",
            "mobile network",
        ),
        excludes=("telecom equipment", "phone accessories", "mobile phone repair"),
    ),

    # -------------------------------------------------------------------------
    # Donations
    # -------------------------------------------------------------------------
    R(
        "donation_brands",
        "Donations",
        exact_names=(
            "gofundme",
            "red cross",
            "australian red cross",
            "salvation army",
            "the salvation army",
            "st vincent de paul",
            "vinnies",
            "foodbank",
            "world vision",
            "oxfam",
            "unicef",
            "cancer council",
            "rspca",
            "beyond blue",
            "beyondblue",
            "heart foundation",
            "lifeline",
            "guide dogs",
            "doctors without borders",
            "medecins sans frontieres",
        ),
    ),
    R(
        "donation_general",
        "Donations",
        strong=(
            "charitable donation",
            "charity donation",
            "donation appeal",
            "fundraising appeal",
            "not for profit charity",
            "non profit charity",
        ),
    ),

    # -------------------------------------------------------------------------
    # Subscription TV
    # -------------------------------------------------------------------------
    R(
        "subscription_tv_brands",
        "Subscription TV",
        exact_names=(
            "netflix",
            "stan",
            "disney plus",
            "disney+",
            "foxtel",
            "britbox",
            "binge",
            "prime video",
            "apple tv",
            "paramount plus",
            "paramount+",
            "kayo",
            "kayo sports",
            "optus sport",
        ),
        exact_domains=(
            "netflix.com",
            "disneyplus.com",
            "stan.com.au",
            "foxtel.com.au",
            "binge.com.au",
            "kayosports.com.au",
        ),
        strong=(
            "video streaming subscription",
            "streaming television",
            "subscription television",
            "tv streaming service",
        ),
    ),

    # -------------------------------------------------------------------------
    # Rent
    # -------------------------------------------------------------------------
    R(
        "rent_payment",
        "Rent",
        strong=(
            "rent payment",
            "rental payment",
            "residential rent",
            "weekly rent",
            "monthly rent",
            "property rent",
            "tenant payment",
        ),
    ),
    R(
        "rent_property_management",
        "Rent",
        strong=(
            "residential property management",
            "rental property management",
            "property manager",
            "property management",
            "real estate rent",
        ),
    ),
    R(
        "rent_storage",
        "Rent",
        exact_names=("storage king", "national storage", "abacus storage"),
        strong=(
            "self storage",
            "storage unit",
            "storage units",
            "storage locker",
            "storage warehouse",
        ),
    ),

    # -------------------------------------------------------------------------
    # Transport
    # -------------------------------------------------------------------------
    R(
        "transport_rideshare",
        "Transport",
        exact_names=("uber", "didi", "ola cabs", "13cabs", "silver service taxi"),
        exact_domains=("uber.com",),
        strong=("taxi service", "taxi company", "rideshare", "ride sharing"),
        excludes=("uber eats", "food delivery", "taxi truck"),
    ),
    R(
        "transport_toll_parking",
        "Transport",
        exact_names=("linkt", "citylink"),
        strong=("toll road", "road toll", "e toll", "e-toll", "car parking", "parking station"),
    ),
    R(
        "transport_public",
        "Transport",
        strong=(
            "bus service",
            "coach service",
            "public transport",
            "train service",
            "rail service",
            "transit system",
            "ferry service",
        ),
    ),
    R(
        "transport_freight_courier",
        "Transport",
        strong=(
            "freight company",
            "freight service",
            "courier service",
            "parcel delivery",
            "transport logistics",
            "shipping company",
        ),
        excludes=("software logistics", "logistics consulting"),
    ),
    R(
        "transport_removals",
        "Transport",
        strong=(
            "removalist",
            "removalists",
            "furniture removal",
            "moving company",
            "moving service",
        ),
    ),
    R(
        "transport_vehicle_rental",
        "Transport",
        strong=(
            "car rental",
            "car hire",
            "truck rental",
            "truck hire",
            "ute hire",
            "vehicle rental",
        ),
    ),

    # -------------------------------------------------------------------------
    # Travel
    # -------------------------------------------------------------------------
    R(
        "travel_booking_brands",
        "Travel",
        exact_names=(
            "airbnb",
            "booking.com",
            "expedia",
            "wotif",
            "webjet",
            "flight centre",
            "qantas",
            "jetstar",
            "virgin australia",
        ),
        exact_domains=(
            "airbnb.com",
            "booking.com",
            "qantas.com",
            "jetstar.com",
        ),
    ),
    R(
        "travel_accommodation",
        "Travel",
        strong=(
            "hotel accommodation",
            "motel accommodation",
            "holiday accommodation",
            "holiday rental",
            "holiday letting",
            "caravan park",
            "holiday park",
            "tourist park",
            "backpacker hostel",
            "bed and breakfast",
        ),
        excludes=("casino", "restaurant", "hotel supplies"),
    ),
    R(
        "travel_agency",
        "Travel",
        strong=(
            "travel agency",
            "travel agent",
            "tour operator",
            "holiday packages",
            "airline tickets",
            "flight booking",
        ),
    ),
    R(
        "travel_airline",
        "Travel",
        strong=("airline", "air travel", "domestic flights", "international flights"),
        excludes=("airline catering", "airline equipment"),
    ),

    # -------------------------------------------------------------------------
    # Entertainment
    # -------------------------------------------------------------------------
    R(
        "entertainment_cinema",
        "Entertainment",
        exact_names=(
            "event cinemas",
            "hoyts",
            "village cinemas",
            "reading cinemas",
            "dendy cinemas",
        ),
        strong=("movie cinema", "cinema complex", "movie theatre", "movie theater"),
    ),
    R(
        "entertainment_attraction",
        "Entertainment",
        strong=(
            "theme park",
            "amusement park",
            "water park",
            "wildlife park",
            "zoological park",
            "public aquarium",
            "trampoline park",
            "escape room",
            "laser tag",
            "laser skirmish",
            "paintball centre",
            "paintball center",
        ),
    ),
    R(
        "entertainment_performance",
        "Entertainment",
        strong=(
            "performing arts theatre",
            "performing arts theater",
            "live theatre",
            "live theater",
            "concert venue",
            "entertainment venue",
            "night club",
            "nightclub",
            "bowling alley",
            "tenpin bowling",
        ),
    ),
    R(
        "entertainment_ticketing",
        "Entertainment",
        exact_names=("ticketek", "ticketmaster", "eventbrite"),
        strong=("event ticketing", "concert tickets", "show tickets"),
    ),
    R(
        "entertainment_gaming",
        "Entertainment",
        exact_names=("steam games", "playstation network", "xbox live", "nintendo eshop"),
        strong=("video game subscription", "online gaming platform", "arcade games"),
    ),

    # -------------------------------------------------------------------------
    # Dining Out
    # -------------------------------------------------------------------------
    R(
        "dining_chains",
        "Dining Out",
        exact_names=(
            "mcdonalds",
            "mcdonald's",
            "kfc",
            "hungry jacks",
            "hungry jack's",
            "subway",
            "dominos pizza",
            "domino's pizza",
            "grilld",
            "grill'd",
            "nandos",
            "nando's",
            "guzman y gomez",
            "zambrero",
        ),
    ),
    R(
        "dining_delivery",
        "Dining Out",
        exact_names=("uber eats", "doordash", "menulog", "deliveroo"),
        exact_domains=("ubereats.com", "doordash.com", "menulog.com.au"),
        strong=("food delivery service", "restaurant delivery", "meal delivery platform"),
    ),
    R(
        "dining_general",
        "Dining Out",
        strong=(
            "restaurant",
            "cafe restaurant",
            "coffee shop",
            "takeaway food",
            "take away food",
            "fast food",
            "food court",
            "pizza restaurant",
            "pizzeria",
            "sushi bar",
            "sushi train",
            "noodle bar",
            "noodle house",
            "thai restaurant",
            "chinese restaurant",
            "indian restaurant",
            "vietnamese restaurant",
            "japanese restaurant",
            "korean restaurant",
            "mexican restaurant",
            "fish and chips",
            "charcoal chicken",
            "chicken shop",
            "gelato shop",
            "ice creamery",
        ),
    ),

    # -------------------------------------------------------------------------
    # Department Stores
    # -------------------------------------------------------------------------
    R(
        "department_store_brands",
        "Department Stores",
        exact_names=(
            "kmart",
            "kmart australia",
            "big w",
            "myer",
            "david jones",
            "target australia",
            "temu",
        ),
        exact_domains=("kmart.com.au", "bigw.com.au", "myer.com.au", "temu.com"),
    ),
    R(
        "department_store_general",
        "Department Stores",
        strong=("department store", "general merchandise store"),
    ),

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------
    R(
        "health_pharmacy",
        "Health",
        exact_names=("chemist warehouse", "priceline pharmacy"),
        strong=("pharmacy", "community pharmacy", "discount chemist"),
    ),
    R(
        "health_dental",
        "Health",
        strong=(
            "dental clinic",
            "dental practice",
            "dentist",
            "orthodontist",
            "endodontist",
            "periodontist",
            "oral surgeon",
        ),
    ),
    R(
        "health_allied",
        "Health",
        strong=(
            "physiotherapy",
            "physiotherapist",
            "chiropractor",
            "chiropractic",
            "psychologist",
            "psychology clinic",
            "podiatrist",
            "podiatry",
            "audiologist",
            "audiology",
            "osteopath",
            "osteopathy",
            "naturopath",
            "naturopathy",
            "acupuncture clinic",
            "speech pathologist",
            "speech pathology",
            "occupational therapist",
            "occupational therapy",
            "dietitian",
            "dietician",
            "allied health",
        ),
    ),
    R(
        "health_medical_clinic",
        "Health",
        strong=(
            "medical centre",
            "medical center",
            "medical clinic",
            "medical practice",
            "general practice clinic",
            "gp clinic",
            "day surgery",
            "ambulance service",
        ),
        excludes=(
            "animal hospital",
            "vet hospital",
            "pet hospital",
            "animal surgery",
            "veterinary surgery",
        ),
    ),
    R(
        "health_diagnostics",
        "Health",
        strong=(
            "radiology clinic",
            "diagnostic imaging",
            "pathology laboratory",
            "medical pathology",
        ),
    ),
    R(
        "health_specialist",
        "Health",
        strong=(
            "optometrist",
            "optometry",
            "ophthalmologist",
            "ophthalmology",
            "dermatologist",
            "dermatology",
            "cardiologist",
            "cardiology",
            "gynaecologist",
            "gynecologist",
            "gynaecology",
            "gynecology",
            "gastroenterologist",
            "gastroenterology",
            "neurologist",
            "neurology",
            "oncologist",
            "oncology",
            "paediatrician",
            "pediatrician",
            "paediatrics",
            "pediatrics",
            "psychiatrist",
            "psychiatry",
            "urologist",
            "urology",
            "orthopaedic",
            "orthopedic",
            "orthopaedics",
            "orthopedics",
            "endocrinologist",
            "endocrinology",
            "rheumatologist",
            "rheumatology",
            "anaesthetist",
            "anesthetist",
            "midwife",
            "midwifery",
        ),
    ),

    # -------------------------------------------------------------------------
    # Financial Institutions
    # -------------------------------------------------------------------------
    R(
        "financial_bank_brands",
        "Financial Institutions",
        exact_names=(
            "commonwealth bank",
            "commonwealth bank of australia",
            "commbank",
            "cba",
            "westpac",
            "nab",
            "national australia bank",
            "anz",
            "anz bank",
            "bankwest",
            "bendigo bank",
            "adelaide bank",
            "macquarie bank",
            "ing bank",
            "suncorp bank",
            "bank of queensland",
            "boq",
        ),
        exact_domains=(
            "commbank.com.au",
            "westpac.com.au",
            "nab.com.au",
            "anz.com.au",
        ),
    ),
    R(
        "financial_institution_general",
        "Financial Institutions",
        strong=(
            "credit union",
            "building society",
            "banking corporation",
            "commercial bank",
            "mutual bank",
            "online bank",
            "financial institution",
            "mortgage lender",
            "personal loan provider",
            "consumer lender",
            "payday lender",
        ),
        excludes=(
            "food bank",
            "blood bank",
            "river bank",
            "bankstown",
            "finance broker",
            "financial adviser",
            "financial advisor",
            "accounting",
        ),
    ),

    # -------------------------------------------------------------------------
    # Retail
    # -------------------------------------------------------------------------
    R(
        "retail_electronics",
        "Retail",
        exact_names=(
            "jb hi fi",
            "jb hi-fi",
            "harvey norman",
            "the good guys",
            "officeworks",
            "apple store",
        ),
        strong=(
            "electronics retailer",
            "computer store",
            "mobile phone store",
            "office supplies store",
            "appliance store",
        ),
        excludes=("mobile phone repair", "computer repair"),
    ),
    R(
        "retail_home_furniture",
        "Retail",
        exact_names=("ikea", "fantastic furniture", "amart furniture"),
        strong=(
            "furniture store",
            "homewares store",
            "mattress store",
            "lighting store",
        ),
        excludes=("furniture removal", "furniture repair"),
    ),
    R(
        "retail_fashion",
        "Retail",
        exact_names=(
            "cotton on",
            "uniqlo",
            "zara",
            "h and m",
            "h&m",
            "rebel sport",
        ),
        strong=(
            "clothing store",
            "fashion retailer",
            "shoe store",
            "footwear store",
            "sportswear store",
            "jewellery store",
            "jewelry store",
        ),
    ),
    R(
        "retail_specialty",
        "Retail",
        strong=(
            "book store",
            "bookshop",
            "toy store",
            "gift shop",
            "florist shop",
            "craft store",
            "sporting goods store",
            "camera store",
            "music store",
        ),
    ),
    R(
        "retail_general",
        "Retail",
        strong=("online retailer", "specialty retailer", "retail store"),
        excludes=(
            "pet store",
            "pet shop",
            "hardware store",
            "grocery store",
            "liquor store",
            "department store",
            "charity shop",
            "coffee shop",
        ),
    ),

    # -------------------------------------------------------------------------
    # Information
    # -------------------------------------------------------------------------
    R(
        "information_news_publishing",
        "Information",
        strong=(
            "newspaper publisher",
            "news publisher",
            "digital publisher",
            "magazine publisher",
            "book publisher",
            "publishing company",
            "online news service",
            "business information service",
            "market information service",
            "data information service",
        ),
        excludes=("book store", "music publisher", "video production"),
    ),
    R(
        "information_data_services",
        "Information",
        strong=(
            "data provider",
            "information provider",
            "credit information service",
            "business directory service",
            "research database",
        ),
    ),

    # -------------------------------------------------------------------------
    # Utilities
    # -------------------------------------------------------------------------
    R(
        "utility_energy_brands",
        "Utilities",
        exact_names=(
            "agl",
            "agl energy",
            "origin energy",
            "energyaustralia",
            "energy australia",
            "red energy",
            "alinta energy",
            "simply energy",
            "powershop",
            "momentum energy",
            "synergy energy",
            "ergon energy",
            "aurora energy",
        ),
        exact_domains=("agl.com.au", "originenergy.com.au", "energyaustralia.com.au"),
    ),
    R(
        "utility_water_brands",
        "Utilities",
        exact_names=(
            "sydney water",
            "yarra valley water",
            "south east water",
            "sa water",
            "water corporation",
            "unitywater",
            "city west water",
        ),
    ),
    R(
        "utility_network_brands",
        "Utilities",
        exact_names=("ausgrid", "endeavour energy", "essential energy", "jemena"),
    ),
    R(
        "utility_general",
        "Utilities",
        strong=(
            "electricity retailer",
            "energy retailer",
            "gas retailer",
            "electricity provider",
            "gas provider",
            "water utility",
            "water authority",
            "water corporation",
            "electricity network",
            "electricity distributor",
            "utility provider",
        ),
        excludes=(
            "solar installer",
            "electrical contractor",
            "gas plumber",
            "water filter",
            "bottled water",
        ),
    ),
)


# =============================================================================
# Text, rule-compilation and CSV helpers
# =============================================================================


# Compiled regular expressions avoid rebuilding the same regex objects for every
# merchant and every rule phrase.
_SPACE_RE = re.compile(r"\s+")
_POSSESSIVE_RE = re.compile(r"(?<=\w)[’']s\b")
_APOSTROPHE_RE = re.compile(r"[’'`]")
_SEPARATOR_RE = re.compile(r"[_/\\]+")
_DASH_RE = re.compile(r"[-–—]+")
_NON_WORD_SPACE_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_NON_WORD_RE = re.compile(r"[^\w]", flags=re.UNICODE)
_KEYWORD_SPLIT_RE = re.compile(r"\s*\|\s*|\r?\n+")

# Bounded caches keep frequently repeated merchant/rule strings fast without
# allowing memory use to grow indefinitely on very large files.
TEXT_CACHE_SIZE = 100_000
HOST_CACHE_SIZE = 50_000


@dataclass(frozen=True)
class CompiledRule:
    """A Rule with all fixed text normalized once at startup."""

    original: Rule
    exact_names: tuple[tuple[str, str, str], ...]
    exact_domains: tuple[tuple[str, str], ...]
    strong: tuple[tuple[str, str], ...]
    required_any: tuple[tuple[str, str], ...]
    required_all: tuple[tuple[str, str], ...]
    excludes: tuple[tuple[str, str], ...]


@dataclass
class RuleEngine:
    """Precompiled rules and indexes used to select candidate rules quickly."""

    rules: tuple[CompiledRule, ...]
    token_index: dict[str, tuple[int, ...]]
    exact_name_index: dict[str, tuple[int, ...]]
    exact_name_compact_index: dict[str, tuple[int, ...]]
    exact_domain_index: dict[str, tuple[int, ...]]
    global_skip_phrases: tuple[tuple[str, str], ...]


_RULE_ENGINE: RuleEngine | None = None


def now_china_timestamp() -> str:
    """Return a stable timestamp string in Asia/Shanghai time."""

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def clean_value(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def normalize_space(value: object) -> str:
    return _SPACE_RE.sub(" ", clean_value(value)).strip()


@lru_cache(maxsize=TEXT_CACHE_SIZE)
def _normalize_text_cached(text: str) -> str:
    """Normalize an already-clean string; safe to memoize because it is pure."""

    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("&", " and ")
    text = _POSSESSIVE_RE.sub("s", text)
    text = _APOSTROPHE_RE.sub("", text)
    text = text.replace("+", " plus ")
    text = _SEPARATOR_RE.sub(" ", text)
    text = _DASH_RE.sub(" ", text)
    text = _NON_WORD_SPACE_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def normalize_text(value: object) -> str:
    """Normalize punctuation, whitespace and common company-name variants."""

    return _normalize_text_cached(clean_value(value))


@lru_cache(maxsize=TEXT_CACHE_SIZE)
def _compact_normalized_text(normalized_text: str) -> str:
    return _NON_WORD_RE.sub("", normalized_text)


def compact_text(value: object) -> str:
    return _compact_normalized_text(normalize_text(value))


@lru_cache(maxsize=1)
def _normalized_business_suffixes() -> tuple[str, ...]:
    return tuple(normalize_text(suffix) for suffix in BUSINESS_SUFFIXES)


@lru_cache(maxsize=TEXT_CACHE_SIZE)
def _strip_business_suffixes_cached(normalized_name: str) -> str:
    result = normalized_name
    changed = True
    suffixes = _normalized_business_suffixes()

    while result and changed:
        changed = False
        for suffix_norm in suffixes:
            if result == suffix_norm:
                return ""
            marker = f" {suffix_norm}"
            if result.endswith(marker):
                result = result[: -len(marker)].strip()
                changed = True
                break
    return result


def strip_business_suffixes(name: str) -> str:
    return _strip_business_suffixes_cached(normalize_text(name))


def split_keywords(value: object) -> list[str]:
    text = clean_value(value)
    if not text:
        return []

    result: list[str] = []
    seen: set[str] = set()
    for part in _KEYWORD_SPLIT_RE.split(text):
        normalized = normalize_text(part)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


@lru_cache(maxsize=HOST_CACHE_SIZE)
def _normalize_hostname_cached(raw: str) -> str:
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        hostname = (urlparse(candidate).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def normalize_hostname(url: object) -> str:
    return _normalize_hostname_cached(clean_value(url))


def domain_to_text(hostname: str) -> str:
    if not hostname:
        return ""
    return normalize_text(hostname.replace(".", " ").replace("-", " "))


def _as_search_text(normalized_text: str) -> str:
    return f" {normalized_text} " if normalized_text else ""


def _phrase_in_search(search_text: str, normalized_phrase: str) -> bool:
    """Token-aware match where both inputs have already been normalized."""

    return bool(search_text and normalized_phrase and f" {normalized_phrase} " in search_text)


def phrase_in_text(text: str, phrase: str) -> bool:
    """Compatibility wrapper for token-aware matching on normalized text."""

    return _phrase_in_search(_as_search_text(text), normalize_text(phrase))


def phrase_anywhere(context: MerchantContext, phrase: str) -> bool:
    return _phrase_in_search(context.combined_search, normalize_text(phrase))


def build_context(row: dict[str, str]) -> MerchantContext:
    merchant_name_raw = normalize_space(row.get("merchant_name", ""))
    name = normalize_text(merchant_name_raw)
    name_core = strip_business_suffixes(name)
    keyword_items = tuple(split_keywords(row.get("keywords", "")))
    keywords_text = " | ".join(keyword_items)
    hostname = normalize_hostname(row.get("link", ""))
    domain_text = domain_to_text(hostname)

    # name_core is intentionally omitted here because it is a strict prefix of
    # name after suffix removal and therefore adds duplicate phrase checks.
    combined_text = " | ".join(
        part for part in (name, keywords_text, domain_text) if part
    )

    name_search = _as_search_text(name)
    keywords_search = _as_search_text(keywords_text)
    domain_search = _as_search_text(domain_text)
    combined_search = _as_search_text(combined_text)
    tokens = frozenset(token for token in combined_text.split() if token != "|")

    return MerchantContext(
        merchant_name_raw=merchant_name_raw,
        name=name,
        name_core=name_core,
        name_compact=compact_text(name_core or name),
        keyword_items=keyword_items,
        keywords_text=keywords_text,
        hostname=hostname,
        domain_text=domain_text,
        combined_text=combined_text,
        name_search=name_search,
        keywords_search=keywords_search,
        domain_search=domain_search,
        combined_search=combined_search,
        tokens=tokens,
    )


def exact_name_matches(context: MerchantContext, expected: str) -> bool:
    expected_norm = strip_business_suffixes(expected)
    if not expected_norm:
        return False
    if context.name == expected_norm or context.name_core == expected_norm:
        return True
    expected_compact = compact_text(expected_norm)
    return bool(expected_compact and context.name_compact == expected_compact)


def exact_domain_matches(hostname: str, expected: str) -> bool:
    expected_norm = normalize_hostname(expected)
    if not expected_norm:
        expected_norm = clean_value(expected).lower().strip(".")
        if expected_norm.startswith("www."):
            expected_norm = expected_norm[4:]
    return bool(
        hostname
        and expected_norm
        and (hostname == expected_norm or hostname.endswith(f".{expected_norm}"))
    )


def open_csv_reader(path: Path) -> tuple[object, csv.DictReader]:
    handle = path.open("r", encoding="utf-8-sig", newline="")
    reader = csv.DictReader(handle)
    return handle, reader


def validate_fieldnames(path: Path, reader: csv.DictReader) -> list[str]:
    fieldnames = list(reader.fieldnames or [])
    missing = [field for field in REQUIRED_INPUT_FIELDS if field not in fieldnames]
    if missing:
        raise ValueError(
            f"Merchant KB schema mismatch in {path}. Missing columns: {missing}. "
            f"Found columns: {fieldnames}."
        )
    return fieldnames


def ensure_fields(fieldnames: Iterable[str], additions: Iterable[str]) -> list[str]:
    result = list(fieldnames)
    for field_name in additions:
        if field_name not in result:
            result.append(field_name)
    return result


def validate_rules(rules: Sequence[Rule]) -> None:
    seen_names: set[str] = set()
    for rule in rules:
        if rule.name in seen_names:
            raise ValueError(f"Duplicate rule name: {rule.name}")
        seen_names.add(rule.name)
        if rule.category not in ALLOWED_CATEGORIES:
            raise ValueError(
                f"Rule {rule.name!r} uses unknown category {rule.category!r}."
            )
        if not any((rule.exact_names, rule.exact_domains, rule.strong)):
            raise ValueError(f"Rule {rule.name!r} has no matching evidence.")


def _compile_phrase_pairs(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for original in values:
        normalized = normalize_text(original)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append((original, normalized))
    return tuple(result)


def _compile_exact_names(values: Sequence[str]) -> tuple[tuple[str, str, str], ...]:
    result: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for original in values:
        normalized = strip_business_suffixes(original)
        compact = compact_text(normalized)
        key = (normalized, compact)
        if normalized and key not in seen:
            seen.add(key)
            result.append((original, normalized, compact))
    return tuple(result)


def _compile_exact_domains(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for original in values:
        normalized = normalize_hostname(original)
        if not normalized:
            normalized = clean_value(original).lower().strip(".")
            if normalized.startswith("www."):
                normalized = normalized[4:]
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append((original, normalized))
    return tuple(result)


def _freeze_index(index: dict[str, set[int]]) -> dict[str, tuple[int, ...]]:
    return {key: tuple(sorted(values)) for key, values in index.items()}


def _build_rule_engine(rules: Sequence[Rule]) -> RuleEngine:
    validate_rules(rules)

    compiled_rules: list[CompiledRule] = []
    token_index_work: dict[str, set[int]] = defaultdict(set)
    exact_name_index_work: dict[str, set[int]] = defaultdict(set)
    exact_name_compact_index_work: dict[str, set[int]] = defaultdict(set)
    exact_domain_index_work: dict[str, set[int]] = defaultdict(set)

    for rule_index, rule in enumerate(rules):
        exact_names = _compile_exact_names(rule.exact_names)
        exact_domains = _compile_exact_domains(rule.exact_domains)
        strong = _compile_phrase_pairs(rule.strong)
        required_any = _compile_phrase_pairs(rule.required_any)
        required_all = _compile_phrase_pairs(rule.required_all)
        excludes = _compile_phrase_pairs(rule.excludes)

        compiled_rules.append(
            CompiledRule(
                original=rule,
                exact_names=exact_names,
                exact_domains=exact_domains,
                strong=strong,
                required_any=required_any,
                required_all=required_all,
                excludes=excludes,
            )
        )

        # A rule can only match when an exact value or strong phrase is present.
        # Indexing these signals avoids scanning every rule for every merchant.
        for _, normalized, compact in exact_names:
            exact_name_index_work[normalized].add(rule_index)
            if compact:
                exact_name_compact_index_work[compact].add(rule_index)

        for _, normalized in exact_domains:
            exact_domain_index_work[normalized].add(rule_index)

        for _, normalized in strong:
            for token in normalized.split():
                token_index_work[token].add(rule_index)

    return RuleEngine(
        rules=tuple(compiled_rules),
        token_index=_freeze_index(token_index_work),
        exact_name_index=_freeze_index(exact_name_index_work),
        exact_name_compact_index=_freeze_index(exact_name_compact_index_work),
        exact_domain_index=_freeze_index(exact_domain_index_work),
        global_skip_phrases=_compile_phrase_pairs(GLOBAL_SKIP_PHRASES),
    )


def get_rule_engine() -> RuleEngine:
    global _RULE_ENGINE
    if _RULE_ENGINE is None:
        _RULE_ENGINE = _build_rule_engine(RULES)
    return _RULE_ENGINE


def _candidate_rule_indexes(
    context: MerchantContext,
    engine: RuleEngine,
) -> tuple[int, ...]:
    candidates: set[int] = set()

    for token in context.tokens:
        candidates.update(engine.token_index.get(token, ()))

    candidates.update(engine.exact_name_index.get(context.name, ()))
    candidates.update(engine.exact_name_index.get(context.name_core, ()))
    candidates.update(engine.exact_name_compact_index.get(context.name_compact, ()))

    if context.hostname:
        hostname_parts = context.hostname.split(".")
        for index in range(len(hostname_parts)):
            suffix = ".".join(hostname_parts[index:])
            candidates.update(engine.exact_domain_index.get(suffix, ()))

    return tuple(sorted(candidates))


def _deduplicate_evidence(items: Iterable[Evidence]) -> tuple[Evidence, ...]:
    """Deduplicate deterministic evidence for logging and diagnostics."""

    strength_rank = {"exact": 2, "strong": 1}
    best: dict[tuple[str, str], Evidence] = {}

    for item in items:
        normalized = normalize_text(item.phrase)
        key = (item.field, normalized)
        previous = best.get(key)
        if previous is None or strength_rank[item.strength] > strength_rank[previous.strength]:
            best[key] = item

    return tuple(
        sorted(
            best.values(),
            key=lambda item: (
                -strength_rank[item.strength],
                item.field,
                normalize_text(item.phrase),
            ),
        )
    )


def _compiled_exact_name_matches(
    context: MerchantContext,
    normalized: str,
    compact: str,
) -> bool:
    if context.name == normalized or context.name_core == normalized:
        return True
    return bool(compact and context.name_compact == compact)


def _compiled_exact_domain_matches(hostname: str, expected: str) -> bool:
    return bool(
        hostname
        and expected
        and (hostname == expected or hostname.endswith(f".{expected}"))
    )


def evaluate_rule(rule: CompiledRule, context: MerchantContext) -> RuleMatch | None:
    """Return a match only when a curated high-confidence rule is satisfied."""

    if any(
        _phrase_in_search(context.combined_search, normalized)
        for _, normalized in rule.excludes
    ):
        return None

    evidence: list[Evidence] = []

    for original, normalized, compact in rule.exact_names:
        if _compiled_exact_name_matches(context, normalized, compact):
            evidence.append(Evidence("merchant_name", original, "exact"))

    for original, normalized in rule.exact_domains:
        if _compiled_exact_domain_matches(context.hostname, normalized):
            evidence.append(Evidence("domain", original, "exact"))

    # Exact merchant/domain matches bypass supporting conditions. Conditions
    # mainly protect otherwise generic strong phrases such as "hardware".
    has_exact_evidence = bool(evidence)
    if not has_exact_evidence:
        if rule.required_any and not any(
            _phrase_in_search(context.combined_search, normalized)
            for _, normalized in rule.required_any
        ):
            return None

        if rule.required_all and not all(
            _phrase_in_search(context.combined_search, normalized)
            for _, normalized in rule.required_all
        ):
            return None

    for original, normalized in rule.strong:
        if _phrase_in_search(context.name_search, normalized):
            evidence.append(Evidence("merchant_name", original, "strong"))
        if _phrase_in_search(context.domain_search, normalized):
            evidence.append(Evidence("domain", original, "strong"))
        if _phrase_in_search(context.keywords_search, normalized):
            evidence.append(Evidence("keywords", original, "strong"))

    unique_evidence = _deduplicate_evidence(evidence)
    if not unique_evidence:
        return None

    return RuleMatch(
        rule_name=rule.original.name,
        category=rule.original.category,
        evidence=unique_evidence,
    )


def aggregate_candidates(
    rule_matches: Sequence[RuleMatch],
) -> tuple[CategoryCandidate, ...]:
    grouped: dict[str, list[RuleMatch]] = defaultdict(list)
    for match in rule_matches:
        grouped[match.category].append(match)

    return tuple(
        CategoryCandidate(
            category=category,
            rule_matches=tuple(sorted(matches, key=lambda item: item.rule_name)),
        )
        for category, matches in sorted(grouped.items())
    )


def classify_context(context: MerchantContext) -> Decision:
    """Classify only when exactly one category matches high-confidence rules.

    - No matched category: leave unchanged.
    - One matched category: write the category.
    - Multiple matched categories: treat as conflict and leave unchanged.
    """

    engine = get_rule_engine()

    if any(
        _phrase_in_search(context.combined_search, normalized)
        for _, normalized in engine.global_skip_phrases
    ):
        return Decision(status="GLOBAL_SKIP")

    candidate_indexes = _candidate_rule_indexes(context, engine)
    rule_matches = [
        match
        for index in candidate_indexes
        if (match := evaluate_rule(engine.rules[index], context)) is not None
    ]
    candidates = aggregate_candidates(rule_matches)

    if not candidates:
        return Decision(status="NO_MATCH")

    if len(candidates) > 1:
        return Decision(
            status="IGNORED_CONFLICT",
            candidates=candidates,
            conflict=True,
        )

    winner = candidates[0]
    winning_match = winner.rule_matches[0]
    return Decision(
        status="AUTO_CLASSIFIED",
        category=winner.category,
        winning_rule=winning_match.rule_name,
        candidates=candidates,
        evidence=winner.evidence,
        conflict=False,
    )


# =============================================================================
# Output helpers
# =============================================================================


def open_atomic_writer(
    target_path: Path,
    fieldnames: Sequence[str],
) -> tuple[Path, object, csv.DictWriter]:
    """Open a temporary CSV that can atomically replace the target file."""

    target_path = target_path.resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f"{target_path.name}.{os.getpid()}.tmp")
    handle = temp_path.open("w", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
    writer.writeheader()
    return temp_path, handle, writer


# =============================================================================
# Main processing
# =============================================================================


def process_kb(args: argparse.Namespace) -> dict[str, int]:
    """Update only high-confidence empty categories in the merchant KB."""

    validate_rules(RULES)

    source_path: Path = args.merchant_kb.resolve()
    # ``--output`` remains available for safe testing, but the normal behaviour
    # is to update the supplied KB directly.
    target_path: Path = (args.output or args.merchant_kb).resolve()

    print(f"Processing: {source_path}", flush=True)
    if args.dry_run:
        print("Mode: DRY RUN — no KB fields will be changed", flush=True)
    elif target_path == source_path:
        print("Mode: update the merchant KB in place", flush=True)
    else:
        print(f"Output: {target_path}", flush=True)

    source_handle, reader = open_csv_reader(source_path)
    original_fieldnames = validate_fieldnames(source_path, reader)
    output_fieldnames = ensure_fields(original_fieldnames, ["category_source"])

    temp_path: Path | None = None
    output_handle: object | None = None
    output_writer: csv.DictWriter | None = None

    if not args.dry_run:
        temp_path, output_handle, output_writer = open_atomic_writer(
            target_path, output_fieldnames
        )

    stats: Counter[str] = Counter()
    category_hits: Counter[str] = Counter()
    rule_hits: Counter[str] = Counter()
    run_timestamp = now_china_timestamp()

    processing_succeeded = False
    try:
        for raw_row in reader:
            # Keep all original fields and values except for the three fields
            # explicitly updated after a high-confidence decision.
            row = {key: clean_value(value) for key, value in raw_row.items()}
            stats["total"] += 1

            existing_category = clean_value(row.get("category", ""))
            if existing_category:
                stats["already_categorized"] += 1
                if output_writer is not None:
                    row.setdefault("category_source", "")
                    output_writer.writerow(row)
                continue

            stats["empty_category"] += 1
            context = build_context(row)
            decision = classify_context(context)
            stats[decision.status] += 1

            if decision.status == "AUTO_CLASSIFIED":
                # These are the only KB mutations made by the script.
                row["category"] = decision.category
                row["category_source"] = f"RULES:{RULE_VERSION}"
                row["category_updated_at"] = run_timestamp
                category_hits[decision.category] += 1
                rule_hits[decision.winning_rule] += 1

                if args.verbose:
                    print(
                        "  UPDATE "
                        f"category={decision.category!r:<30} "
                        f"merchant={context.merchant_name_raw!r} "
                        f"rule={decision.winning_rule!r}",
                        flush=True,
                    )

            # Conflicts, excluded rows and no-match rows are written back
            # without any classification.
            if output_writer is not None:
                row.setdefault("category_source", "")
                output_writer.writerow(row)

            if args.progress_every > 0 and stats["total"] % args.progress_every == 0:
                ignored = (
                    stats["IGNORED_CONFLICT"]
                    + stats["NO_MATCH"]
                    + stats["GLOBAL_SKIP"]
                )
                print(
                    "  progress "
                    f"rows={stats['total']} "
                    f"updated={stats['AUTO_CLASSIFIED']} "
                    f"ignored={ignored}",
                    flush=True,
                )

            if args.max_rows and stats["total"] >= args.max_rows:
                print(f"Stopped at --max-rows={args.max_rows}", flush=True)
                break

        processing_succeeded = True
    finally:
        source_handle.close()
        if output_handle is not None:
            output_handle.close()
        if not processing_succeeded and temp_path is not None:
            temp_path.unlink(missing_ok=True)

    if not args.dry_run and temp_path is not None:
        # When updating in place and no category changed, leave the original KB
        # byte-for-byte untouched. For a separate --output path, still create
        # the requested copy.
        if target_path == source_path and stats["AUTO_CLASSIFIED"] == 0:
            temp_path.unlink(missing_ok=True)
        else:
            temp_path.replace(target_path)

    ignored_count = (
        stats["IGNORED_CONFLICT"]
        + stats["NO_MATCH"]
        + stats["GLOBAL_SKIP"]
    )

    print("\nSummary", flush=True)
    print(f"  total rows             : {stats['total']}", flush=True)
    print(f"  already categorized    : {stats['already_categorized']}", flush=True)
    print(f"  empty category         : {stats['empty_category']}", flush=True)
    print(f"  KB categories updated  : {stats['AUTO_CLASSIFIED']}", flush=True)
    print(f"  ignored conflicts      : {stats['IGNORED_CONFLICT']}", flush=True)
    print(f"  ignored no match       : {stats['NO_MATCH']}", flush=True)
    print(f"  ignored global skip    : {stats['GLOBAL_SKIP']}", flush=True)
    print(f"  total ignored          : {ignored_count}", flush=True)

    if category_hits:
        print("\nUpdated by category", flush=True)
        for category, count in category_hits.most_common():
            print(f"  {category:<30} {count}", flush=True)

    if rule_hits:
        print("\nWinning rules", flush=True)
        for rule_name, count in rule_hits.most_common():
            print(f"  {rule_name:<40} {count}", flush=True)

    if args.dry_run:
        print("\nDry run completed. The merchant KB was not changed.", flush=True)
    elif target_path == source_path and stats["AUTO_CLASSIFIED"] == 0:
        print("\nNo high-confidence updates. The original KB was left unchanged.", flush=True)
    else:
        print(f"\nKB written to: {target_path}", flush=True)

    return dict(stats)



def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Directly update empty merchant KB categories only when the "
            "rule result is clear and high-confidence."
        )
    )
    parser.add_argument(
        "--merchant-kb",
        type=Path,
        default=DEFAULT_MERCHANT_KB,
        help="Merchant KB CSV to update (default: merchant_kb.csv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Optional separate output CSV for testing. By default the supplied "
            "merchant KB is updated in place."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only the rows that would be updated; do not write the KB.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each high-confidence category update.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100_000,
        help="Print progress every N rows; use 0 to disable (default: 100000).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Process at most N rows for testing; 0 means no limit.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.merchant_kb.exists():
        print(f"File not found: {args.merchant_kb}", file=sys.stderr)
        return 1
    if args.progress_every < 0:
        parser.error("--progress-every cannot be negative")
    if args.max_rows < 0:
        parser.error("--max-rows cannot be negative")

    try:
        process_kb(args)
    except (OSError, csv.Error, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
