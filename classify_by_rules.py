#!/usr/bin/env python3
"""
Update an existing merchant KB using high-confidence classification rules.

Behaviour
---------
1. Only rows with an empty ``category`` are evaluated.
2. Only a clear, high-confidence winner is written back to the KB.
3. Conflicting, low-confidence, excluded and unmatched rows are left unchanged.
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

Use stricter thresholds:
    python classify_by_rules_high_confidence.py --min-score 95 --min-margin 25
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
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
RULE_VERSION = "merchant_rules_v2.1_high_confidence_20260729"

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


# Evidence weights. A single strong phrase in the merchant name can classify;
# a website keyword normally needs corroborating evidence.
WEIGHT_EXACT_NAME = 100
WEIGHT_EXACT_DOMAIN = 100
WEIGHT_STRONG_NAME = 85
WEIGHT_STRONG_DOMAIN = 75
WEIGHT_STRONG_KEYWORD = 60
WEIGHT_WEAK_NAME = 45
WEIGHT_WEAK_DOMAIN = 35
WEIGHT_WEAK_KEYWORD = 20

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

    ``weak``
        Supporting phrases that should not classify on their own.

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
    weak: tuple[str, ...] = ()
    required_any: tuple[str, ...] = ()
    required_all: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    score_adjustment: int = 0


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


@dataclass(frozen=True)
class Evidence:
    field: str
    phrase: str
    strength: str
    weight: int

    def display(self) -> str:
        return f"{self.field}:{self.phrase}({self.strength},{self.weight})"


@dataclass(frozen=True)
class RuleMatch:
    rule_name: str
    category: str
    score: int
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class CategoryCandidate:
    category: str
    score: int
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
    score: int = 0
    margin: int = 0
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
    weak: Sequence[str] = (),
    required_any: Sequence[str] = (),
    required_all: Sequence[str] = (),
    excludes: Sequence[str] = (),
    score_adjustment: int = 0,
) -> Rule:
    """Compact rule-construction helper."""

    return Rule(
        name=name,
        category=category,
        exact_names=tuple(exact_names),
        exact_domains=tuple(exact_domains),
        strong=tuple(strong),
        weak=tuple(weak),
        required_any=tuple(required_any),
        required_all=tuple(required_all),
        excludes=tuple(excludes),
        score_adjustment=score_adjustment,
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
        weak=("kennel",),
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
        weak=("college", "academy"),
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
        weak=("builder", "construction"),
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
        weak=("tiling", "flooring"),
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
        weak=("termite",),
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
        weak=("painter", "painters"),
        excludes=("artist", "portrait", "gallery"),
    ),
    R(
        "home_fencing",
        "Home Improvement",
        strong=("fencing contractor", "fence installer", "pool fencing"),
        weak=("fencing",),
        excludes=("fencing club", "fencing academy", "fencing sport"),
    ),
    R(
        "home_hardware",
        "Home Improvement",
        exact_names=("bunnings", "bunnings warehouse", "mitre 10", "home hardware"),
        exact_domains=("bunnings.com.au",),
        strong=("hardware store", "building supplies", "building materials"),
        weak=("hardware",),
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
        weak=("mechanic",),
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
        weak=("tyres", "tires"),
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
        weak=("petrol", "fuel"),
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
        weak=("barber", "barbers"),
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
        weak=("waxing", "nails"),
    ),
    R(
        "personal_massage",
        "Personal Care",
        strong=("massage centre", "massage center", "massage spa", "massage therapy"),
        weak=("massage",),
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
        weak=("laundry",),
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
        weak=("butcher", "deli", "seafood"),
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
        weak=("bakery",),
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
        weak=("gym", "fitness"),
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
        weak=("pilates", "crossfit"),
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
        weak=("lottery", "lotto"),
    ),
    R(
        "gambling_casino",
        "Gambling",
        strong=("online casino", "casino gaming", "poker machines", "pokies"),
        weak=("casino",),
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
        weak=("insurance", "insurer", "underwriting"),
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
        weak=("telecommunications", "telecom", "broadband"),
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
        weak=("donation", "charity", "fundraising"),
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
        weak=("real estate",),
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
        weak=("taxi", "taxis", "cab", "cabs"),
        excludes=("uber eats", "food delivery", "taxi truck"),
    ),
    R(
        "transport_toll_parking",
        "Transport",
        exact_names=("linkt", "citylink"),
        strong=("toll road", "road toll", "e toll", "e-toll", "car parking", "parking station"),
        weak=("parking",),
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
        weak=("freight", "courier", "couriers", "logistics"),
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
        weak=("removals", "movers"),
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
        weak=("hotel", "motel", "resort", "hostel", "backpackers"),
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
        weak=("cinema", "cinemas", "cineplex"),
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
        weak=("museum", "zoo", "aquarium", "trampoline", "paintball"),
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
        weak=("theatre", "theater", "bowling"),
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
        weak=("cafe", "pizza", "sushi", "takeaway", "gelato"),
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
        weak=("chemist",),
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
        weak=("dental",),
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
        weak=("physio", "acupuncture"),
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
        weak=("hospital", "surgery"),
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
        weak=("radiology", "pathology"),
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
        weak=("bank", "lender", "financial services", "finance company"),
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
        weak=("retail", "shop", "store"),
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
        weak=("newspaper", "publisher", "publishing", "news media"),
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
        weak=("information services",),
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
        weak=("electricity bill", "gas bill", "water bill", "utilities"),
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
# Text and CSV helpers
# =============================================================================


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
    return re.sub(r"\s+", " ", clean_value(value)).strip()


def normalize_text(value: object) -> str:
    """Normalize punctuation, whitespace and common company-name variants."""

    text = unicodedata.normalize("NFKC", clean_value(value)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"(?<=\w)[’']s\b", "s", text)
    text = re.sub(r"[’'`]", "", text)
    text = text.replace("+", " plus ")
    text = re.sub(r"[_/\\]+", " ", text)
    text = re.sub(r"[-–—]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def compact_text(value: object) -> str:
    return re.sub(r"[^\w]", "", normalize_text(value), flags=re.UNICODE)


def strip_business_suffixes(name: str) -> str:
    result = normalize_text(name)
    changed = True
    while result and changed:
        changed = False
        for suffix in BUSINESS_SUFFIXES:
            suffix_norm = normalize_text(suffix)
            if result == suffix_norm:
                return ""
            marker = f" {suffix_norm}"
            if result.endswith(marker):
                result = result[: -len(marker)].strip()
                changed = True
                break
    return result


def split_keywords(value: object) -> list[str]:
    text = clean_value(value)
    if not text:
        return []
    # The existing KB uses "|". Newlines are accepted as a convenience.
    parts = re.split(r"\s*\|\s*|\r?\n+", text)
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = normalize_text(part)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def normalize_hostname(url: object) -> str:
    raw = clean_value(url)
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


def domain_to_text(hostname: str) -> str:
    if not hostname:
        return ""
    return normalize_text(hostname.replace(".", " ").replace("-", " "))


def phrase_in_text(text: str, phrase: str) -> bool:
    """Token-aware phrase matching on normalized text."""

    phrase_norm = normalize_text(phrase)
    if not phrase_norm or not text:
        return False
    return f" {phrase_norm} " in f" {text} "


def phrase_anywhere(context: MerchantContext, phrase: str) -> bool:
    return phrase_in_text(context.combined_text, phrase)


def build_context(row: dict[str, str]) -> MerchantContext:
    merchant_name_raw = normalize_space(row.get("merchant_name", ""))
    name = normalize_text(merchant_name_raw)
    name_core = strip_business_suffixes(name)
    keyword_items = tuple(split_keywords(row.get("keywords", "")))
    keywords_text = " | ".join(keyword_items)
    hostname = normalize_hostname(row.get("link", ""))
    domain_text = domain_to_text(hostname)
    combined_text = " | ".join(
        part for part in (name, name_core, keywords_text, domain_text) if part
    )
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
        if not any((rule.exact_names, rule.exact_domains, rule.strong, rule.weak)):
            raise ValueError(f"Rule {rule.name!r} has no matching evidence.")


def _deduplicate_evidence(items: Iterable[Evidence]) -> tuple[Evidence, ...]:
    best: dict[tuple[str, str], Evidence] = {}
    for item in items:
        key = (item.field, normalize_text(item.phrase))
        previous = best.get(key)
        if previous is None or item.weight > previous.weight:
            best[key] = item
    return tuple(
        sorted(
            best.values(),
            key=lambda item: (-item.weight, item.field, normalize_text(item.phrase)),
        )
    )


def _score_evidence(evidence: Sequence[Evidence], score_adjustment: int) -> int:
    if not evidence:
        return 0

    ordered = sorted(evidence, key=lambda item: item.weight, reverse=True)
    score = ordered[0].weight

    # Additional independent evidence is useful, but never as valuable as the
    # strongest signal. This avoids keyword-heavy web pages inflating scores.
    seen_phrases = {normalize_text(ordered[0].phrase)}
    for item in ordered[1:]:
        phrase_norm = normalize_text(item.phrase)
        if phrase_norm in seen_phrases:
            continue
        seen_phrases.add(phrase_norm)
        if item.weight >= 85:
            score += 15
        elif item.weight >= 60:
            score += 12
        elif item.weight >= 35:
            score += 8
        else:
            score += 4

    # Evidence from more than one source is more trustworthy.
    fields = {item.field for item in ordered}
    if len(fields) >= 2:
        score += 5
    if len(fields) >= 3:
        score += 5

    score += score_adjustment
    return max(0, min(100, score))


def evaluate_rule(rule: Rule, context: MerchantContext) -> RuleMatch | None:
    if any(phrase_anywhere(context, phrase) for phrase in rule.excludes):
        return None

    evidence: list[Evidence] = []

    for expected in rule.exact_names:
        if exact_name_matches(context, expected):
            evidence.append(
                Evidence("merchant_name", expected, "exact", WEIGHT_EXACT_NAME)
            )

    for expected in rule.exact_domains:
        if exact_domain_matches(context.hostname, expected):
            evidence.append(
                Evidence("domain", expected, "exact", WEIGHT_EXACT_DOMAIN)
            )

    # Exact merchant/domain matches are allowed to bypass supporting conditions.
    # The conditions mainly protect generic phrases such as "hardware".
    has_exact_evidence = bool(evidence)
    if not has_exact_evidence:
        if rule.required_any and not any(
            phrase_anywhere(context, phrase) for phrase in rule.required_any
        ):
            return None

        if rule.required_all and not all(
            phrase_anywhere(context, phrase) for phrase in rule.required_all
        ):
            return None

    for phrase in rule.strong:
        if phrase_in_text(context.name, phrase) or phrase_in_text(context.name_core, phrase):
            evidence.append(
                Evidence("merchant_name", phrase, "strong", WEIGHT_STRONG_NAME)
            )
        if phrase_in_text(context.domain_text, phrase):
            evidence.append(Evidence("domain", phrase, "strong", WEIGHT_STRONG_DOMAIN))
        if phrase_in_text(context.keywords_text, phrase):
            evidence.append(
                Evidence("keywords", phrase, "strong", WEIGHT_STRONG_KEYWORD)
            )

    for phrase in rule.weak:
        if phrase_in_text(context.name, phrase) or phrase_in_text(context.name_core, phrase):
            evidence.append(Evidence("merchant_name", phrase, "weak", WEIGHT_WEAK_NAME))
        if phrase_in_text(context.domain_text, phrase):
            evidence.append(Evidence("domain", phrase, "weak", WEIGHT_WEAK_DOMAIN))
        if phrase_in_text(context.keywords_text, phrase):
            evidence.append(Evidence("keywords", phrase, "weak", WEIGHT_WEAK_KEYWORD))

    unique_evidence = _deduplicate_evidence(evidence)
    if not unique_evidence:
        return None

    score = _score_evidence(unique_evidence, rule.score_adjustment)
    return RuleMatch(
        rule_name=rule.name,
        category=rule.category,
        score=score,
        evidence=unique_evidence,
    )


def aggregate_candidates(rule_matches: Sequence[RuleMatch]) -> tuple[CategoryCandidate, ...]:
    grouped: dict[str, list[RuleMatch]] = defaultdict(list)
    for match in rule_matches:
        grouped[match.category].append(match)

    candidates: list[CategoryCandidate] = []
    for category, matches in grouped.items():
        ordered = sorted(matches, key=lambda item: (-item.score, item.rule_name))
        # A second independent rule in the same category can strengthen the
        # result, but the bonus is intentionally small.
        score = ordered[0].score + min(10, 5 * (len(ordered) - 1))
        candidates.append(
            CategoryCandidate(
                category=category,
                score=min(100, score),
                rule_matches=tuple(ordered),
            )
        )

    return tuple(sorted(candidates, key=lambda item: (-item.score, item.category)))


def classify_context(
    context: MerchantContext,
    *,
    min_score: int,
    min_margin: int,
) -> Decision:
    """Return an automatic classification only for a clear winner.

    A row is automatically classified when all of the following hold:
    - the best category score reaches ``min_score``;
    - no second category also reaches ``min_score``; and
    - the best category leads the runner-up by at least ``min_margin``.

    Every other outcome is ignored by the KB update process.
    """

    if any(phrase_anywhere(context, phrase) for phrase in GLOBAL_SKIP_PHRASES):
        return Decision(status="GLOBAL_SKIP")

    rule_matches = [
        match for rule in RULES if (match := evaluate_rule(rule, context)) is not None
    ]
    candidates = aggregate_candidates(rule_matches)

    if not candidates:
        return Decision(status="NO_MATCH")

    winner = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0
    margin = winner.score - second_score

    # Conservative conflict definition:
    # 1. another category is independently high-confidence; or
    # 2. the winning category does not lead by enough points.
    competing_high_confidence = len(candidates) > 1 and second_score >= min_score
    insufficient_margin = len(candidates) > 1 and margin < min_margin
    conflict = competing_high_confidence or insufficient_margin

    evidence = winner.evidence
    winning_rule = winner.rule_matches[0].rule_name if winner.rule_matches else ""

    if winner.score >= min_score and not conflict:
        return Decision(
            status="AUTO_CLASSIFIED",
            category=winner.category,
            score=winner.score,
            margin=margin,
            winning_rule=winning_rule,
            candidates=candidates,
            evidence=evidence,
            conflict=False,
        )

    return Decision(
        status="IGNORED_CONFLICT" if conflict else "IGNORED_LOW_CONFIDENCE",
        # The candidate is retained only for internal logging. It is never
        # written to the merchant KB when the decision is ignored.
        category=winner.category,
        score=winner.score,
        margin=margin,
        winning_rule=winning_rule,
        candidates=candidates,
        evidence=evidence,
        conflict=conflict,
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
            decision = classify_context(
                context,
                min_score=args.min_score,
                min_margin=args.min_margin,
            )
            stats[decision.status] += 1

            if decision.status == "AUTO_CLASSIFIED":
                # These are the only KB mutations made by the script.
                row["category"] = decision.category
                row["category_source"] = f"RULES:{RULE_VERSION}"
                row["category_updated_at"] = now_china_timestamp()
                category_hits[decision.category] += 1
                rule_hits[decision.winning_rule] += 1

                if args.verbose:
                    print(
                        "  UPDATE "
                        f"score={decision.score:<3} "
                        f"margin={decision.margin:<3} "
                        f"category={decision.category!r:<30} "
                        f"merchant={context.merchant_name_raw!r} "
                        f"rule={decision.winning_rule!r}",
                        flush=True,
                    )

            # For conflicts, low-confidence cases, excluded rows and no-match
            # rows, the original row is written back without any classification.
            if output_writer is not None:
                row.setdefault("category_source", "")
                output_writer.writerow(row)

            if args.progress_every > 0 and stats["total"] % args.progress_every == 0:
                ignored = (
                    stats["IGNORED_CONFLICT"]
                    + stats["IGNORED_LOW_CONFIDENCE"]
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
        + stats["IGNORED_LOW_CONFIDENCE"]
        + stats["NO_MATCH"]
        + stats["GLOBAL_SKIP"]
    )

    print("\nSummary", flush=True)
    print(f"  total rows             : {stats['total']}", flush=True)
    print(f"  already categorized    : {stats['already_categorized']}", flush=True)
    print(f"  empty category         : {stats['empty_category']}", flush=True)
    print(f"  KB categories updated  : {stats['AUTO_CLASSIFIED']}", flush=True)
    print(f"  ignored conflicts      : {stats['IGNORED_CONFLICT']}", flush=True)
    print(
        f"  ignored low confidence : {stats['IGNORED_LOW_CONFIDENCE']}",
        flush=True,
    )
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


def bounded_int(minimum: int, maximum: int):
    def parser(value: str) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be between {minimum} and {maximum}"
            )
        return parsed

    return parser


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
        "--min-score",
        type=bounded_int(0, 100),
        default=85,
        help="Minimum winning score required to update the KB (default: 85).",
    )
    parser.add_argument(
        "--min-margin",
        type=bounded_int(0, 100),
        default=20,
        help=(
            "Minimum score lead over the second category required to update "
            "the KB (default: 20)."
        ),
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
