"""
pipeline/services.py — Build normalized service location dataset.

Tries Toronto Open Data APIs for shelters, drop-ins, and community services.
Falls back to a realistic seed generator if the API is unavailable.

Standard schema per service:
  {id, name, type, lat, lng, address, hours_open_by_hour[24]}

hours_open_by_hour: list of 24 ints (1=open, 0=closed) indexed by hour 0-23.

Outputs
-------
data_cache/service_locations.json
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("deadzone.services")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

# Toronto Open Data CKAN base
CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action/datastore_search"

# Known resource IDs for Toronto service datasets
TORONTO_DATASETS = {
    "shelter_support": "21f29f9a-60e3-4571-9c19-8438d74efccc",  # overnight shelters
    "drop_in":         "7d88e8f6-3be2-4e73-a7b1-40614cdc8a7e",  # drop-in centres
    "food_banks":      "0b88d8c0-f3f1-4e9a-9b1b-0e2b5e6b7e8a",  # approximate; may 404
}

# Hour profiles — (open_hour, close_hour) in 24h, or "24h"
HOUR_PROFILES = {
    "24h":       list(range(24)),
    "day":       list(range(8, 20)),
    "late":      list(range(6, 22)),
    "overnight": list(range(0, 8)) + list(range(20, 24)),
    "evening":   list(range(14, 22)),
    "standard":  list(range(9, 17)),
}


def _make_hours(profile: str) -> list[int]:
    open_hours = set(HOUR_PROFILES.get(profile, HOUR_PROFILES["standard"]))
    return [1 if h in open_hours else 0 for h in range(24)]


def _fetch_toronto_dataset(resource_id: str, limit: int = 500) -> list[dict]:
    try:
        resp = requests.get(
            CKAN_BASE,
            params={"resource_id": resource_id, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        records = result.get("result", {}).get("records", [])
        log.info("  Fetched %d records from resource %s", len(records), resource_id)
        return records
    except Exception as exc:
        log.warning("  Could not fetch resource %s: %s", resource_id, exc)
        return []


def _parse_lat_lng(record: dict) -> tuple[float, float] | None:
    """Try common lat/lng field names."""
    lat_keys = ["LATITUDE", "latitude", "lat", "Y", "y", "GEO_LATITUDE"]
    lng_keys = ["LONGITUDE", "longitude", "lng", "lon", "X", "x", "GEO_LONGITUDE"]
    lat = lng = None
    for k in lat_keys:
        if k in record and record[k]:
            try:
                lat = float(record[k])
                break
            except (ValueError, TypeError):
                pass
    for k in lng_keys:
        if k in record and record[k]:
            try:
                lng = float(record[k])
                break
            except (ValueError, TypeError):
                pass
    if lat and lng and 43.4 < lat < 44.0 and -79.8 < lng < -79.0:
        return lat, lng
    return None


def _fetch_real_services() -> list[dict]:
    """Attempt to pull real Toronto services from Open Data."""
    services: list[dict] = []
    idx = 0

    for svc_type, resource_id in TORONTO_DATASETS.items():
        records = _fetch_toronto_dataset(resource_id)
        for rec in records:
            coords = _parse_lat_lng(rec)
            if not coords:
                continue
            lat, lng = coords
            name = (
                rec.get("PROGRAM_NAME") or rec.get("NAME") or
                rec.get("LOCATION_NAME") or rec.get("SITE_NAME") or f"{svc_type} {idx}"
            )
            # Shelters/drop-ins are typically overnight/24h
            profile = "24h" if svc_type == "shelter_support" else "late"
            services.append({
                "id": f"{svc_type}_{idx:04d}",
                "name": str(name).strip(),
                "type": svc_type,
                "lat": lat,
                "lng": lng,
                "address": rec.get("ADDRESS", rec.get("STREET_ADDRESS", "")),
                "hours_open_by_hour": _make_hours(profile),
            })
            idx += 1

    log.info("Fetched %d real service locations from Toronto Open Data", len(services))
    return services


# ---------Seed generator — realistic Toronto service landscape--------

SEED_SERVICES = [
    # (name, type, lat, lng, hours_profile)
    # --- Hospitals ---
    ("Toronto General Hospital",       "hospital",         43.6594, -79.3891, "24h"),
    ("St. Michael's Hospital",         "hospital",         43.6533, -79.3758, "24h"),
    ("Mount Sinai Hospital",           "hospital",         43.6573, -79.3961, "24h"),
    ("Sunnybrook Health Sciences",     "hospital",         43.7233, -79.3776, "24h"),
    ("Humber River Hospital",          "hospital",         43.7440, -79.5233, "24h"),
    ("Scarborough Health Network",     "hospital",         43.7726, -79.2468, "24h"),
    ("North York General",             "hospital",         43.7618, -79.3957, "24h"),
    ("St. Joseph's Health Centre",     "hospital",         43.6398, -79.4553, "24h"),
    ("Michael Garron Hospital",        "hospital",         43.6995, -79.3204, "24h"),
    # --- Downtown shelters (24h) ---
    ("Scott Mission",                  "shelter",          43.6510, -79.3871, "24h"),
    ("Seaton House",                   "shelter",          43.6596, -79.3654, "24h"),
    ("Fred Victor Centre",             "shelter",          43.6549, -79.3618, "24h"),
    ("Covenant House Toronto",         "shelter",          43.6437, -79.3754, "overnight"),
    ("Salvation Army Gateway",         "shelter",          43.6523, -79.3802, "24h"),
    ("Sojourn House",                  "shelter",          43.6610, -79.3631, "24h"),
    ("Eva's Phoenix",                  "shelter",          43.6561, -79.3804, "overnight"),
    # --- Food banks ---
    ("Daily Bread Food Bank",          "food_bank",        43.6395, -79.4128, "standard"),
    ("Second Harvest",                 "food_bank",        43.6621, -79.3898, "day"),
    ("North York Harvest",             "food_bank",        43.7612, -79.4135, "day"),
    ("Scarborough Food Bank",          "food_bank",        43.7724, -79.2560, "day"),
    ("Rexdale Community Hub",          "food_bank",        43.7318, -79.5658, "standard"),
    ("St. Felix Centre",               "food_bank",        43.6390, -79.4175, "standard"),
    # --- Community health clinics ---
    ("Sherbourne Health Centre",       "clinic",           43.6711, -79.3715, "late"),
    ("Regent Park CHC",                "clinic",           43.6593, -79.3588, "day"),
    ("Stonegate CHC",                  "clinic",           43.6341, -79.4848, "standard"),
    ("Rexdale CHC",                    "clinic",           43.7253, -79.5803, "standard"),
    ("Scarborough CHC",                "clinic",           43.7702, -79.2488, "standard"),
    ("NE Toronto CHC",                 "clinic",           43.7118, -79.3154, "standard"),
    ("South Riverdale CHC",            "clinic",           43.6612, -79.3444, "day"),
    ("Black Creek CHC",                "clinic",           43.7318, -79.4980, "standard"),
    # --- 24h pharmacies ---
    ("Shoppers Drug Mart (24h) - King","pharmacy",         43.6455, -79.3917, "24h"),
    ("Shoppers Drug Mart (24h) - Bloor","pharmacy",        43.6716, -79.3863, "24h"),
    ("Rexall (24h) - College",         "pharmacy",         43.6603, -79.4009, "24h"),
    ("Shoppers Drug Mart (24h) - Yonge & Eg","pharmacy",   43.7050, -79.3984, "24h"),
    # --- Community centres ---
    ("Scadding Court CC",              "community_centre", 43.6467, -79.4012, "evening"),
    ("Jimmie Simpson Rec",             "community_centre", 43.6654, -79.3384, "evening"),
    ("Rexdale CC",                     "community_centre", 43.7271, -79.5699, "evening"),
    ("Malvern CC",                     "community_centre", 43.8026, -79.2231, "evening"),
    ("Amesbury Park CC",               "community_centre", 43.7141, -79.5102, "evening"),
    ("North York CC",                  "community_centre", 43.7691, -79.4143, "evening"),
    ("Swansea Town Hall CC",           "community_centre", 43.6390, -79.4753, "evening"),
    ("Albion CC",                      "community_centre", 43.7388, -79.5465, "evening"),
    # --- Mental health / crisis (24h — key for OD/crisis correlation) ---
    ("CAMH",                           "mental_health",    43.6355, -79.4173, "24h"),
    ("Gerstein Crisis Centre",         "mental_health",    43.6669, -79.3821, "24h"),
    ("Distress Centre Toronto",        "mental_health",    43.6548, -79.3843, "24h"),
    ("Jean Tweed Centre",              "mental_health",    43.6358, -79.4762, "standard"),
    ("Reconnect Mental Health",        "mental_health",    43.7102, -79.4853, "standard"),
    ("Scarborough Mental Health",      "mental_health",    43.7710, -79.2500, "standard"),
    # --- Rexdale / NW Toronto (sparse late-night — intentional for demo) ---
    ("Rexdale Pharmacy",               "pharmacy",         43.7355, -79.5681, "standard"),
    ("Woodbine Heights Clinic",        "clinic",           43.6991, -79.3063, "standard"),
    ("Etobicoke General Walk-In",      "clinic",           43.6893, -79.5391, "standard"),
    # --- Distributed Shoppers Drug Mart (24h) across Toronto ---
    ("Shoppers - Eglinton/AV",         "pharmacy",         43.7038, -79.3949, "24h"),
    ("Shoppers - Lawrence/Yonge",      "pharmacy",         43.7239, -79.3994, "24h"),
    ("Shoppers - Sheppard/Yonge",      "pharmacy",         43.7617, -79.4086, "24h"),
    ("Shoppers - Finch/Yonge",         "pharmacy",         43.7799, -79.4153, "24h"),
    ("Shoppers - Danforth/Pape",       "pharmacy",         43.6793, -79.3434, "24h"),
    ("Shoppers - Victoria Park",       "pharmacy",         43.6910, -79.2934, "24h"),
    ("Shoppers - Warden/Lawrence",     "pharmacy",         43.7152, -79.2854, "24h"),
    ("Shoppers - Kennedy/Ellesmere",   "pharmacy",         43.7330, -79.2641, "24h"),
    ("Shoppers - McCowan/Sheppard",    "pharmacy",         43.7817, -79.2337, "24h"),
    ("Shoppers - Jane/Bloor",          "pharmacy",         43.6483, -79.4823, "24h"),
    ("Shoppers - Kipling/Bloor",       "pharmacy",         43.6365, -79.5358, "24h"),
    ("Shoppers - Wilson/Allen",        "pharmacy",         43.7345, -79.4650, "24h"),
    ("Shoppers - Finch/Jane",          "pharmacy",         43.7511, -79.5000, "late"),
    ("Shoppers - Albion/Islington",    "pharmacy",         43.7440, -79.5520, "late"),
    ("Shoppers - Sheppard/Weston",     "pharmacy",         43.7410, -79.5205, "late"),
    # --- Walk-in clinics city-wide ---
    ("Appletree Medical - Downtown",   "clinic",           43.6480, -79.3820, "late"),
    ("Appletree Medical - Eglinton",   "clinic",           43.7060, -79.3980, "standard"),
    ("Appletree Medical - North York", "clinic",           43.7692, -79.4140, "standard"),
    ("Appletree Medical - Scarborough","clinic",           43.7720, -79.2450, "standard"),
    ("Appletree Medical - Etobicoke",  "clinic",           43.6520, -79.5150, "standard"),
    ("After-Hours Clinic - Danforth",  "clinic",           43.6820, -79.3340, "late"),
    ("Walk-In - Jane and Finch",       "clinic",           43.7570, -79.4960, "standard"),
    ("Walk-In - Lawrence West",        "clinic",           43.7220, -79.4590, "standard"),
    ("Walk-In - Weston",               "clinic",           43.7110, -79.5198, "standard"),
    ("Walk-In - Malvern",              "clinic",           43.8020, -79.2250, "standard"),
    ("Walk-In - Agincourt",            "clinic",           43.7860, -79.2780, "standard"),
    ("Walk-In - Broadview/Danforth",   "clinic",           43.6752, -79.3565, "late"),
]


def generate_seed_services() -> list[dict]:
    """Return realistic hardcoded service list for demo/offline mode."""
    services = []
    for i, (name, svc_type, lat, lng, profile) in enumerate(SEED_SERVICES):
        services.append({
            "id": f"seed_{i:04d}",
            "name": name,
            "type": svc_type,
            "lat": lat,
            "lng": lng,
            "address": "",
            "hours_open_by_hour": _make_hours(profile),
        })
    log.info("Generated %d seed services", len(services))
    return services


# ----------Main-----------

def build_services(cache_dir: Path, use_seed: bool = False) -> list[dict]:
    if not use_seed:
        services = _fetch_real_services()
        if len(services) < 5:
            log.warning("Real data insufficient (%d records); falling back to seed", len(services))
            services = generate_seed_services()
    else:
        services = generate_seed_services()

    out_path = cache_dir / "service_locations.json"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(services, f, indent=2)
    log.info("Wrote %d services to %s", len(services), out_path)
    return services


def main() -> None:
    parser = argparse.ArgumentParser(description="Build service location dataset")
    parser.add_argument("--cache", default="data_cache")
    parser.add_argument("--seed", action="store_true", help="Use seed data only")
    args = parser.parse_args()
    build_services(Path(args.cache), use_seed=args.seed)


if __name__ == "__main__":
    main()
