"""
pipeline/demand.py — Aggregate demand events (311 calls) into grid cells by hour.

Fetches Toronto 311 Open Data (or a cached snapshot) and assigns each event
to an H3 cell + hour bucket.

Output
------
data_cache/demand_by_cell_hour.json   — {cell_id: {hour_str: count}}
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

import requests

log = logging.getLogger("deadzone.demand")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

# Toronto Open Data — 311 Service Requests (current year)
CKAN_311_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action/datastore_search"
RESOURCE_311 = "b1f32e82-6fba-4b04-93e6-38d71a3afe75"  # 311 customer-initiated requests

# Max rows to fetch (demo limit — full dataset is ~500k rows)
FETCH_LIMIT = 10_000

# Service request types that map to night-time demand
DEMAND_TYPES = {
    "Shelter and Housing",
    "Encampment",
    "Streets and Sidewalks",
    "Property Standards",
    "Parks and Recreation",
    "Graffiti",
    "Noise",
    "Abandoned Vehicles",
}

DEFAULT_RESOLUTION = 8


def _import_h3():
    import h3 as _h3
    return _h3


def _latlng_to_cell(h3, lat: float, lng: float, resolution: int) -> str:
    if hasattr(h3, "latlng_to_cell"):
        return h3.latlng_to_cell(lat, lng, resolution)
    return h3.geo_to_h3(lat, lng, resolution)


def _fetch_311(limit: int = FETCH_LIMIT) -> list[dict]:
    """Fetch 311 records from Toronto Open Data CKAN API."""
    log.info("Fetching 311 data from Toronto Open Data (limit=%d) …", limit)
    try:
        resp = requests.get(
            CKAN_311_BASE,
            params={"resource_id": RESOURCE_311, "limit": limit},
            timeout=30,
        )
        resp.raise_for_status()
        records = resp.json().get("result", {}).get("records", [])
        log.info("  Fetched %d 311 records", len(records))
        return records
    except Exception as exc:
        log.warning("  311 fetch failed: %s", exc)
        return []


def _parse_lat_lng_311(rec: dict) -> tuple[float, float] | None:
    lat = rec.get("LATITUDE") or rec.get("latitude")
    lng = rec.get("LONGITUDE") or rec.get("longitude")
    if lat is None or lng is None:
        return None
    try:
        lat, lng = float(lat), float(lng)
    except (ValueError, TypeError):
        return None
    if not (43.4 < lat < 44.0 and -79.9 < lng < -78.9):
        return None
    return lat, lng


def _parse_hour_311(rec: dict) -> int:
    """Extract hour of day from creation_date field."""
    for key in ["CREATION_DATE", "creation_date", "OPEN_DATE", "open_date"]:
        val = rec.get(key, "")
        if val and "T" in str(val):
            try:
                return int(str(val).split("T")[1][:2])
            except (IndexError, ValueError):
                pass
        if val and " " in str(val):
            try:
                return int(str(val).split(" ")[1][:2])
            except (IndexError, ValueError):
                pass
    # Fallback: uniform distribution over evening hours (biased to be realistic)
    import random
    return random.choices(range(24), weights=[
        1,1,1,1,1,1,2,3,4,5,5,5,5,5,5,5,6,7,8,9,8,6,4,2
    ])[0]


def aggregate_demand(
    records: list[dict],
    resolution: int = DEFAULT_RESOLUTION,
) -> dict[str, dict[str, float]]:
    """
    Assign each 311 record to (cell_id, hour) and count.
    Returns {cell_id: {str(hour): count}}.
    """
    h3 = _import_h3()
    counts: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    skipped = 0

    for rec in records:
        coords = _parse_lat_lng_311(rec)
        if not coords:
            skipped += 1
            continue
        lat, lng = coords
        hour = _parse_hour_311(rec)
        cell = _latlng_to_cell(h3, lat, lng, resolution)
        counts[cell][str(hour)] += 1.0

    log.info("Aggregated %d events into %d cells (%d skipped no-coords)", len(records) - skipped, len(counts), skipped)
    return {k: dict(v) for k, v in counts.items()}


def generate_seed_demand(grid_path: Path, resolution: int = DEFAULT_RESOLUTION) -> dict[str, dict[str, float]]:
    """
    Generate synthetic demand from grid cells.
    Rexdale and downtown get boosted evening demand.
    """
    import random
    random.seed(2024)

    if not grid_path.exists():
        log.warning("grid.geojson not found; seed demand will be empty")
        return {}

    with grid_path.open() as f:
        fc = json.load(f)

    REXDALE = {"lat_min": 43.715, "lat_max": 43.760, "lng_min": -79.615, "lng_max": -79.535}
    DOWNTOWN = {"lat_min": 43.635, "lat_max": 43.665, "lng_min": -79.415, "lng_max": -79.360}

    counts: dict[str, dict[str, float]] = {}
    for feat in fc["features"]:
        p = feat["properties"]
        cell_id = p.get("cell_id", p.get("hex_id", ""))
        lat, lng = p["lat"], p["lng"]

        in_rexdale = REXDALE["lat_min"] <= lat <= REXDALE["lat_max"] and REXDALE["lng_min"] <= lng <= REXDALE["lng_max"]
        in_downtown = DOWNTOWN["lat_min"] <= lat <= DOWNTOWN["lat_max"] and DOWNTOWN["lng_min"] <= lng <= DOWNTOWN["lng_max"]

        base = 8.0 if in_rexdale else (5.0 if in_downtown else 2.0)
        cell_counts: dict[str, float] = {}
        for h in range(24):
            evening_boost = 1.8 if 20 <= h <= 23 else (1.3 if 17 <= h <= 19 else 1.0)
            val = max(0, base * evening_boost + random.gauss(0, 1.5))
            cell_counts[str(h)] = round(val, 1)
        counts[cell_id] = cell_counts

    log.info("Generated seed demand for %d cells", len(counts))
    return counts


def build_demand(cache_dir: Path, use_seed: bool = False, resolution: int = DEFAULT_RESOLUTION) -> None:
    t0 = time.perf_counter()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not use_seed:
        records = _fetch_311()
        if len(records) < 50:
            log.warning("Insufficient 311 data; using seed demand")
            use_seed = True
        else:
            counts = aggregate_demand(records, resolution)

    if use_seed:
        counts = generate_seed_demand(cache_dir / "grid.geojson", resolution)

    out_path = cache_dir / "demand_by_cell_hour.json"
    with out_path.open("w") as f:
        json.dump(counts, f, separators=(",", ":"))
    log.info("Wrote demand data to %s  (%.2f s)", out_path, time.perf_counter() - t0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate 311 demand events")
    parser.add_argument("--cache", default="data_cache")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--resolution", type=int, default=DEFAULT_RESOLUTION)
    args = parser.parse_args()
    build_demand(Path(args.cache), use_seed=args.seed, resolution=args.resolution)


if __name__ == "__main__":
    main()
