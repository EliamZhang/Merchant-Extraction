#!/usr/bin/env python3
"""
Classify merchants using high-confidence keyword rules.

Only processes rows where category is empty.
Rules are designed for precision over recall: if unsure, skip.

Usage:
  python classify_by_rules.py --dry-run           # preview matches without writing
  python classify_by_rules.py --verbose --dry-run  # show each match with merchant name
  python classify_by_rules.py                      # apply rules and write back
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import (
    china_timestamp_now,
    clean_output_value,
    normalize_space,
    open_csv_dict_reader,
    safe_url,
    split_kb_keywords,
    KEYWORD_SEPARATOR,
)

DEFAULT_MERCHANT_KB = Path("merchant_kb.csv")

KB_FIELDNAMES = [
    "merchant_name",
    "keywords",
    "link",
    "category",
    "category_source",
    "keyword_updated_at",
    "category_updated_at",
]


# ── Rules ────────────────────────────────────────────────────────────────────
# Each rule: (name, category, [keywords])
# A keyword matches when it appears as a whole-word/phrase in the search text
# (merchant_name + all keywords). Rules are checked in order; first match wins.
# Only add keywords that are unambiguous signals for the target category.


@dataclass
class Rule:
    name: str
    category: str
    keywords: list[str] = field(default_factory=list)


RULES: list[Rule] = [
    # ── Pet Care (before Health so "animal hospital" beats bare "hospital") ──
    Rule("veterinary", "Pet Care", ["veterinary", "veterinarian", "vet clinic", "vet hospital"]),
    Rule("animal_hospital", "Pet Care", ["animal hospital", "pet hospital", "pet medical"]),
    Rule("pet_boarding", "Pet Care", ["pet boarding", "cat boarding", "dog boarding", "kennel", "cattery"]),

    # ── Education ──
    Rule("childcare", "Education", ["childcare", "child care", "daycare", "day care", "family day care"]),
    Rule("kindergarten", "Education", ["kindergarten", "preschool", "pre-school"]),
    Rule("primary_school", "Education", ["primary school", "primary college"]),
    Rule("high_school", "Education", ["high school", "secondary school", "secondary college"]),
    Rule("tafe", "Education", ["tafe"]),
    Rule("montessori", "Education", ["montessori"]),
    Rule("early_learning", "Education", ["early learning centre", "early learning center"]),
    Rule("driving_school", "Education", ["driving school", "driving academy", "driver training"]),
    Rule("grammar_school", "Education", ["grammar school", "catholic school", "christian school", "public school", "anglican school", "lutheran school"]),
    Rule("language_school", "Education", ["language school", "language centre", "english school"]),
    Rule("tutoring", "Education", ["tutoring", "tuition centre", "tuition center"]),
    Rule("child_services", "Education", ["before school care", "after school care", "outside school hours care", "vacation care"]),

    # ── Home Improvement ──
    Rule("plumber", "Home Improvement", ["plumber", "plumbing"]),
    Rule("electrician", "Home Improvement", ["electrician", "electrical contractor"]),
    Rule("roofer", "Home Improvement", ["roofer", "roofing", "roof restoration", "roof repairs"]),
    Rule("concreter", "Home Improvement", ["concreter", "concreting", "concrete services"]),
    Rule("bricklayer", "Home Improvement", ["bricklayer", "bricklaying"]),
    Rule("carpenter", "Home Improvement", ["carpenter", "carpentry"]),
    Rule("tiler", "Home Improvement", ["tiler", "tiling"]),
    Rule("glazier", "Home Improvement", ["glazier", "glass glazing"]),
    Rule("landscaper", "Home Improvement", ["landscaper", "landscaping", "landscape supplies", "landscape supply"]),
    Rule("garden_centre", "Home Improvement", ["garden centre", "garden center", "garden supplies", "garden nursery"]),
    Rule("hardware", "Home Improvement", ["hardware"]),
    Rule("smash_repair", "Home Improvement", ["smash repair", "smash repairs", "panel beating", "panel beater"]),
    Rule("plasterer", "Home Improvement", ["plasterer", "plastering", "gyprock", "gyprocker"]),
    Rule("pest_control", "Home Improvement", ["pest control", "termite"]),
    Rule("painter", "Home Improvement", ["painter", "painters", "painting contractor", "painting service"]),
    Rule("floor_sander", "Home Improvement", ["floor sander", "floor sanding", "timber floor"]),
    Rule("bathroom_renovation", "Home Improvement", ["bathroom renovation", "bathroom renovations", "kitchen renovation", "kitchen renovations"]),
    Rule("builder", "Home Improvement", ["builder", "building contractor"]),
    Rule("scaffolding", "Home Improvement", ["scaffolding", "scaffold"]),
    Rule("welder", "Home Improvement", ["welder", "welding"]),
    Rule("fencing", "Home Improvement", ["fencing contractor", "fencing"]),

    # ── Automotive ──
    Rule("mechanic", "Automotive", ["mechanic", "mechanical repairs", "mechanical repair", "auto repairs"]),
    Rule("tyre_service", "Automotive", ["tyre service", "tyre centre", "tyre shop", "tyre centre", "tyre and auto"]),
    Rule("muffler", "Automotive", ["muffler", "mufflers", "exhaust centre", "exhaust service"]),
    Rule("auto_electrician", "Automotive", ["auto electrician", "auto electrical"]),
    Rule("car_wash", "Automotive", ["car wash", "car detailer", "car detailing"]),
    Rule("towing", "Automotive", ["towing", "tow truck", "tilt tray"]),
    Rule("auto_glass", "Automotive", ["auto glass", "windscreen", "windshield", "autoglass"]),

    # ── Personal Care ──
    Rule("barber", "Personal Care", ["barber", "barbers", "barber shop"]),
    Rule("hairdresser", "Personal Care", ["hairdresser", "hairdressing", "hair salon", "hair studio"]),
    Rule("nail_salon", "Personal Care", ["nail salon", "nail bar", "nail spa", "nails"]),
    Rule("beauty_salon", "Personal Care", ["beauty salon", "beauty spa", "beauty therapist"]),
    Rule("dry_cleaner", "Personal Care", ["dry cleaner", "dry cleaning", "drycleaner", "drycleaning"]),
    Rule("laundromat", "Personal Care", ["laundromat", "laundry", "laundrette", "coin laundry"]),
    Rule("massage", "Personal Care", ["massage", "massage therapy", "remedial massage"]),
    Rule("waxing", "Personal Care", ["waxing", "waxing salon", "waxing studio"]),
    Rule("laser_clinic", "Personal Care", ["laser clinic", "laser hair removal", "skin clinic"]),

    # ── Groceries ──
    Rule("butcher", "Groceries", ["butcher", "butchers", "butchery"]),
    Rule("liquor", "Groceries", ["liquorland", "liquor land", "liquor store", "bottle shop", "bottleshop"]),
    Rule("cellarbrations", "Groceries", ["cellarbrations"]),
    Rule("dan_murphy", "Groceries", ["dan murphy", "dan murphys"]),
    Rule("bws", "Groceries", ["bws"]),
    Rule("delicatessen", "Groceries", ["delicatessen", "deli"]),
    Rule("seafood", "Groceries", ["seafood", "fishmonger", "fish market"]),
    Rule("greengrocer", "Groceries", ["greengrocer", "fruit market", "vegetable market", "fruit and veg"]),

    # ── Gyms and other memberships ──
    Rule("pilates", "Gyms and other memberships", ["pilates"]),
    Rule("yoga", "Gyms and other memberships", ["yoga studio", "yoga centre", "yoga center"]),
    Rule("crossfit", "Gyms and other memberships", ["crossfit"]),
    Rule("swim_school", "Gyms and other memberships", ["swim school", "swim centre", "swimming school", "swimming centre"]),
    Rule("gym", "Gyms and other memberships", ["gym"]),
    Rule("fitness_centre", "Gyms and other memberships", ["fitness centre", "fitness center", "fitness studio", "fitness first"]),
    Rule("martial_arts", "Gyms and other memberships", ["martial arts", "karate", "taekwondo", "jiu jitsu", "judo", "kung fu", "aikido"]),
    Rule("boxing", "Gyms and other memberships", ["boxing gym", "boxing club", "boxing studio"]),
    Rule("personal_trainer", "Gyms and other memberships", ["personal trainer", "personal training"]),
    Rule("f45", "Gyms and other memberships", ["f45"]),
    Rule("anytime_fitness", "Gyms and other memberships", ["anytime fitness"]),
    Rule("snap_fitness", "Gyms and other memberships", ["snap fitness"]),
    Rule("bft", "Gyms and other memberships", ["body fit training"]),

    # ── Gambling ──
    Rule("sportsbet", "Gambling", ["sportsbet"]),
    Rule("ladbrokes", "Gambling", ["ladbrokes"]),
    Rule("bet365", "Gambling", ["bet365"]),
    Rule("betfair", "Gambling", ["betfair"]),
    Rule("tabcorp", "Gambling", ["tabcorp", "tab limited", "tab pty"]),
    Rule("pointsbet", "Gambling", ["pointsbet"]),
    Rule("unibet", "Gambling", ["unibet"]),
    Rule("lottery", "Gambling", ["lottery", "lotto", "lotteries", "oz lotto", "powerball", "ozlotteries"]),
    Rule("casino", "Gambling", ["casino"]),
    Rule("poker_machines", "Gambling", ["poker machines", "pokies"]),
    Rule("betting", "Gambling", ["betting agency", "betting shop"]),

    # ── Insurance ──
    Rule("insurance", "Insurance", ["insurance"]),

    # ── Telecommunications ──
    Rule("telstra", "Telecommunications", ["telstra"]),
    Rule("optus", "Telecommunications", ["optus"]),
    Rule("vodafone", "Telecommunications", ["vodafone"]),
    Rule("broadband", "Telecommunications", ["broadband", "internet service provider"]),
    Rule("telecom", "Telecommunications", ["telecom", "telecommunications"]),
    Rule("aussie_broadband", "Telecommunications", ["aussie broadband"]),
    Rule("superloop", "Telecommunications", ["superloop"]),
    Rule("tpg", "Telecommunications", ["tpg telecom", "tpg internet"]),

    # ── Donations ──
    Rule("gofundme", "Donations", ["gofundme"]),
    Rule("red_cross", "Donations", ["red cross"]),
    Rule("salvation_army", "Donations", ["salvation army", "salvos"]),
    Rule("st_vincent_de_paul", "Donations", ["st vincent de paul", "vinnies"]),
    Rule("foodbank", "Donations", ["foodbank", "food bank"]),
    Rule("world_vision", "Donations", ["world vision"]),
    Rule("oxfam", "Donations", ["oxfam"]),
    Rule("unicef", "Donations", ["unicef"]),
    Rule("cancer_council", "Donations", ["cancer council"]),
    Rule("rspca", "Donations", ["rspca"]),
    Rule("beyond_blue", "Donations", ["beyond blue", "beyondblue"]),
    Rule("heart_foundation", "Donations", ["heart foundation"]),
    Rule("lifeline", "Donations", ["lifeline"]),
    Rule("guide_dogs", "Donations", ["guide dogs"]),
    Rule("msf", "Donations", ["medecins sans frontieres", "doctors without borders", "msf"]),

    # ── Subscription TV ──
    Rule("netflix", "Subscription TV", ["netflix"]),
    Rule("stan", "Subscription TV", ["stan"]),
    Rule("disney_plus", "Subscription TV", ["disney+", "disney plus", "disneyplus"]),
    Rule("foxtel", "Subscription TV", ["foxtel"]),
    Rule("britbox", "Subscription TV", ["britbox"]),
    Rule("binge", "Subscription TV", ["binge"]),
    Rule("amazon_prime", "Subscription TV", ["amazon prime", "amznprime", "prime video"]),
    Rule("apple_tv", "Subscription TV", ["apple tv", "appletv"]),
    Rule("paramount_plus", "Subscription TV", ["paramount+", "paramount plus", "paramountplus"]),
    Rule("kayo", "Subscription TV", ["kayo sports", "kayo"]),
    Rule("optus_sport", "Subscription TV", ["optus sport"]),

    # ── Rent ──
    Rule("storage_king", "Rent", ["storage king"]),
    Rule("national_storage", "Rent", ["national storage"]),
    Rule("self_storage", "Rent", ["self storage"]),
    Rule("storage_unit", "Rent", ["storage unit", "storage units", "storage locker", "storage warehouse", "storage shed"]),
    Rule("abacus_storage", "Rent", ["abacus storage"]),

    # ── Transport ──
    Rule("taxi", "Transport", ["taxi", "taxis", "cab", "cabs", "maxi taxi"]),
    Rule("uber", "Transport", ["uber"]),
    Rule("didi", "Transport", ["didi"]),
    Rule("ola", "Transport", ["ola cabs"]),
    Rule("removalist", "Transport", ["removalist", "removalists", "removals", "furniture removal"]),
    Rule("toll", "Transport", ["toll road", "linkt", "citylink", "e toll", "e-toll"]),
    Rule("parking", "Transport", ["parking"]),
    Rule("car_rental", "Transport", ["car rental", "car hire", "truck rental", "truck hire", "ute hire"]),
    Rule("bus_service", "Transport", ["bus service", "bus lines", "coach service", "busways", "transit systems"]),
    Rule("freight", "Transport", ["freight", "courier", "couriers", "logistics"]),
    Rule("moving", "Transport", ["moving company", "moving service", "movers"]),  # duplicate with Automotive? Let me check...

    # ── Travel ──
    Rule("motel", "Travel", ["motel"]),
    Rule("resort", "Travel", ["resort"]),
    Rule("caravan_park", "Travel", ["caravan park", "holiday park", "tourist park"]),
    Rule("holiday_rental", "Travel", ["holiday rental", "holiday accommodation", "holiday letting"]),
    Rule("backpackers", "Travel", ["backpackers", "hostel"]),
    Rule("bed_breakfast", "Travel", ["bed and breakfast", "bed & breakfast"]),

    # ── Entertainment ──
    Rule("cinema", "Entertainment", ["cinema", "cinemas", "cineplex"]),
    Rule("museum", "Entertainment", ["museum"]),
    Rule("zoo", "Entertainment", ["zoo", "zoological"]),
    Rule("aquarium", "Entertainment", ["aquarium"]),
    Rule("theme_park", "Entertainment", ["theme park", "amusement park", "water park"]),
    Rule("bowling", "Entertainment", ["bowling", "bowl", "tenpin"]),
    Rule("nightclub", "Entertainment", ["nightclub", "night club", "gentlemans club", "strip club"]),
    Rule("theatre", "Entertainment", ["theatre", "theater"]),
    Rule("escape_room", "Entertainment", ["escape room"]),
    Rule("paintball", "Entertainment", ["paintball", "laser tag", "laser skirmish"]),
    Rule("trampoline", "Entertainment", ["trampoline", "bounce inc", "flip out"]),
    Rule("event_cinemas", "Entertainment", ["event cinemas", "hoyts", "dendy", "village cinemas", "reading cinemas"]),

    # ── Dining Out (conservative — only clear signals) ──
    Rule("pizzeria", "Dining Out", ["pizzeria", "pizza"]),
    Rule("sushi", "Dining Out", ["sushi", "sushi bar", "sushi train"]),
    Rule("noodle_bar", "Dining Out", ["noodle bar", "noodle house", "noodle box", "noodlebox"]),
    Rule("thai_restaurant", "Dining Out", ["thai restaurant", "thai cuisine"]),
    Rule("chinese_restaurant", "Dining Out", ["chinese restaurant"]),
    Rule("indian_restaurant", "Dining Out", ["indian restaurant"]),
    Rule("vietnamese_restaurant", "Dining Out", ["vietnamese restaurant", "pho"]),
    Rule("japanese_restaurant", "Dining Out", ["japanese restaurant", "ramen"]),
    Rule("korean_restaurant", "Dining Out", ["korean restaurant", "korean bbq"]),
    Rule("mexican_restaurant", "Dining Out", ["mexican restaurant", "taco", "burrito"]),
    Rule("fish_chips", "Dining Out", ["fish and chips", "fish & chips", "fish n chips"]),
    Rule("takeaway", "Dining Out", ["takeaway", "take away", "take-away"]),
    Rule("roast", "Dining Out", ["roast", "roast kitchen", "roast shop"]),
    Rule("chicken_shop", "Dining Out", ["charcoal chicken", "chicken shop", "chicken bar", "chicken express"]),
    Rule("gelato", "Dining Out", ["gelato", "gelateria", "ice creamery"]),

    # ── Department Stores ──
    Rule("kmart", "Department Stores", ["kmart"]),
    Rule("big_w", "Department Stores", ["big w"]),
    Rule("myer", "Department Stores", ["myer"]),
    Rule("david_jones", "Department Stores", ["david jones"]),
    Rule("temu", "Department Stores", ["temu"]),
    Rule("target_department_store", "Department Stores", ["target australia"]),

    # ── Health (after Pet Care to avoid "animal hospital" matching bare "hospital") ──
    Rule("pharmacy", "Health", ["pharmacy"]),
    Rule("chemist", "Health", ["chemist", "chem mart", "chemmart", "chemist warehouse", "priceline pharmacy", "chemist discount"]),
    Rule("dental", "Health", ["dental", "dentist", "orthodontist", "endodontist", "periodontist", "oral surgeon"]),
    Rule("optometrist", "Health", ["optometrist", "optometry"]),
    Rule("physiotherapy", "Health", ["physiotherapy", "physiotherapist", "physio"]),
    Rule("chiropractor", "Health", ["chiropractor", "chiropractic"]),
    Rule("psychology", "Health", ["psychology", "psychologist", "clinical psychologist"]),
    Rule("radiology", "Health", ["radiology", "radiologist"]),
    Rule("pathology", "Health", ["pathology"]),
    Rule("podiatry", "Health", ["podiatry", "podiatrist"]),
    Rule("audiology", "Health", ["audiology", "audiologist"]),
    Rule("ambulance", "Health", ["ambulance"]),
    Rule("osteopath", "Health", ["osteopath", "osteopathy"]),
    Rule("naturopath", "Health", ["naturopath", "naturopathy"]),
    Rule("acupuncture", "Health", ["acupuncture"]),
    Rule("medical_clinic", "Health", ["medical centre", "medical clinic", "medical practice", "medical group"]),
    Rule("speech_pathology", "Health", ["speech pathology", "speech pathologist", "speech therapist"]),
    Rule("occupational_therapist", "Health", ["occupational therapist", "occupational therapy"]),
    Rule("dietitian", "Health", ["dietitian", "dietician", "dietetics"]),
    Rule("hospital", "Health", ["hospital", "day surgery"]),
    Rule("allied_health", "Health", ["allied health"]),
    Rule("midwife", "Health", ["midwife", "midwifery"]),
    Rule("dermatology", "Health", ["dermatology", "dermatologist"]),
    Rule("cardiology", "Health", ["cardiology", "cardiologist"]),
    Rule("gynaecology", "Health", ["gynaecology", "gynecology", "gynaecologist", "gynecologist"]),
    Rule("ophthalmology", "Health", ["ophthalmology", "ophthalmologist"]),
    Rule("gastroenterology", "Health", ["gastroenterology", "gastroenterologist"]),
    Rule("neurology", "Health", ["neurology", "neurologist"]),
    Rule("oncology", "Health", ["oncology", "oncologist"]),
    Rule("paediatrics", "Health", ["paediatrics", "pediatrics", "paediatrician", "pediatrician"]),
    Rule("psychiatry", "Health", ["psychiatry", "psychiatrist"]),
    Rule("urology", "Health", ["urology", "urologist"]),
    Rule("orthopaedics", "Health", ["orthopaedics", "orthopedics", "orthopaedic", "orthopedic"]),
    Rule("endocrinology", "Health", ["endocrinology", "endocrinologist"]),
    Rule("rheumatology", "Health", ["rheumatology", "rheumatologist"]),
    Rule("anaesthetist", "Health", ["anaesthetist", "anesthetist", "anaesthesiology"]),
    Rule("surgeon", "Health", ["surgeon", "surgery"]),
]


# ── Helper functions ──────────────────────────────────────────────────────────


def validate_kb_fieldnames(path: Path, reader: csv.DictReader) -> None:
    fieldnames = list(reader.fieldnames or [])
    missing = [col for col in KB_FIELDNAMES if col not in fieldnames]
    if missing and missing != ["category_source"]:
        raise ValueError(
            f"Merchant KB schema mismatch in {path}. "
            f"Missing columns {missing}. Expected {KB_FIELDNAMES}, got {fieldnames}."
        )


def normalize_kb_row(row: dict[str, str]) -> dict[str, str]:
    category = clean_output_value(row.get("category", ""))
    category_source = clean_output_value(row.get("category_source", ""))
    if category and not category_source:
        category_source = "AI"
    return {
        "merchant_name": normalize_space(row.get("merchant_name", "")),
        "keywords": KEYWORD_SEPARATOR.join(split_kb_keywords(row.get("keywords", ""))),
        "link": safe_url(row.get("link", "")),
        "category": category,
        "category_source": category_source,
        "keyword_updated_at": normalize_space(row.get("keyword_updated_at", "")),
        "category_updated_at": normalize_space(row.get("category_updated_at", "")),
    }


def write_merchant_kb(path: Path, rows: list[dict[str, str]]) -> None:
    target_path = path.resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f"{target_path.name}.{os.getpid()}.tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=KB_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(target_path)


def word_boundary_match(text: str, keyword: str) -> bool:
    """Return True when *keyword* appears as a whole-word/phrase in *text*."""
    pattern = r"(?<![a-zA-Z0-9])" + re.escape(keyword) + r"(?![a-zA-Z0-9])"
    return bool(re.search(pattern, text, re.IGNORECASE))


def build_search_text(merchant_name: str, keywords: str) -> str:
    """Combine merchant_name and all keywords into one searchable string."""
    parts = [normalize_space(merchant_name)]
    for kw in split_kb_keywords(keywords):
        kw = normalize_space(kw)
        if kw:
            parts.append(kw)
    return " | ".join(parts)


def match_rules(search_text: str) -> tuple[str, str]:
    """Match search_text against rules in priority order. Returns (category, rule_name) or ("", "")."""
    for rule in RULES:
        for keyword in rule.keywords:
            if word_boundary_match(search_text, keyword):
                return rule.category, rule.name
    return "", ""


# ── Main ──────────────────────────────────────────────────────────────────────


def process_kb(args: argparse.Namespace) -> dict:
    if args.verbose or args.dry_run:
        print(f"Reading merchant KB path={args.merchant_kb}", flush=True)

    reader = open_csv_dict_reader(args.merchant_kb)
    validate_kb_fieldnames(args.merchant_kb, reader)

    original_fieldnames = list(reader.fieldnames or [])
    has_category_source = "category_source" in original_fieldnames
    output_fieldnames = list(original_fieldnames)
    if not has_category_source:
        output_fieldnames.append("category_source")
    for name in KB_FIELDNAMES:
        if name not in output_fieldnames:
            output_fieldnames.append(name)

    stats = {
        "total": 0,
        "already_categorized": 0,
        "empty": 0,
        "matched": 0,
        "skipped": 0,
    }
    rule_hits: dict[str, int] = {}

    temp_path: Path | None = None
    writer: csv.DictWriter | None = None
    out_handle: object | None = None

    if not args.dry_run:
        target_path = args.merchant_kb.resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_name(f"{target_path.name}.{os.getpid()}.tmp")
        out_handle = temp_path.open("w", encoding="utf-8-sig", newline="")
        writer = csv.DictWriter(out_handle, fieldnames=output_fieldnames, extrasaction="ignore")
        writer.writeheader()

    try:
        for raw_row in reader:
            row = {key: value or "" for key, value in raw_row.items()}
            stats["total"] += 1

            category = clean_output_value(row.get("category", ""))
            if category:
                stats["already_categorized"] += 1
                if writer is not None:
                    if not has_category_source:
                        row["category_source"] = "AI"
                    writer.writerow(row)
                continue

            stats["empty"] += 1
            search_text = build_search_text(row.get("merchant_name", ""), row.get("keywords", ""))
            matched_category, rule_name = match_rules(search_text)
            if matched_category:
                if args.verbose:
                    print(
                        f"  {rule_name!r} → {matched_category!r}  merchant={row.get('merchant_name', '')!r}",
                        flush=True,
                    )
                row["category"] = matched_category
                row["category_source"] = "RULES"
                row["category_updated_at"] = china_timestamp_now()
                stats["matched"] += 1
                rule_hits[rule_name] = rule_hits.get(rule_name, 0) + 1

            if writer is not None:
                if not has_category_source:
                    row.setdefault("category_source", "")
                writer.writerow(row)

            if stats["total"] % 500000 == 0:
                print(
                    f"  progress rows={stats['total']} matched={stats['matched']}",
                    flush=True,
                )

    finally:
        if out_handle is not None:
            out_handle.close()

    stats["skipped"] = stats["empty"] - stats["matched"]

    if args.dry_run:
        print("DRY RUN — no changes written", flush=True)
    elif stats["matched"] > 0 and temp_path is not None:
        temp_path.replace(args.merchant_kb.resolve())
        if args.verbose:
            print(f"Wrote {stats['total']} rows to {args.merchant_kb}", flush=True)
    elif temp_path is not None:
        temp_path.unlink(missing_ok=True)

    print(
        f"total={stats['total']}  already_categorized={stats['already_categorized']}  "
        f"empty={stats['empty']}  matched={stats['matched']}  skipped={stats['skipped']}",
        flush=True,
    )
    if rule_hits:
        print("Rule hits:", flush=True)
        for name, count in sorted(rule_hits.items(), key=lambda kv: -kv[1]):
            print(f"  {name}: {count}", flush=True)

    return stats


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify merchants using high-confidence keyword rules."
    )
    parser.add_argument("--merchant-kb", type=Path, default=DEFAULT_MERCHANT_KB)
    parser.add_argument("--dry-run", action="store_true", help="Preview matches without writing")
    parser.add_argument("--verbose", action="store_true", help="Print each match")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    if not args.merchant_kb.exists():
        print(f"File not found: {args.merchant_kb}", file=sys.stderr)
        return 1
    process_kb(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
