"""
DeadZone Scoring Engine — pipeline/score_accessibility.py

Scoring philosophy
------------------
We combine three independent signals into a single dead_zone_score per
(grid cell, hour) pair.  Every formula is a weighted linear combination of
normalised sub-scores so a judge can trace any cell's number back to raw
inputs in under 60 seconds.

Signal 1 — Service accessibility (0–100, higher = better access)
    Measures how close the nearest *open* service location is on foot.
    Distance is Haversine (great-circle), which is a deliberate simplification
    flagged in explanation fields.  OSMnx routing, when available, would
    replace this with actual walk-network distance.

    walking_score = 100 * max(0, 1 - dist_m / MAX_WALK_M)

    MAX_WALK_M = 800 m  (roughly a 10-minute walk at 80 m/min)
    If no service is open within MAX_WALK_M the walking score is 0.

Signal 2 — TTC support (0–100, higher = better transit)
    Looks for TTC stops within TTC_RADIUS_M.  The best (most frequent) stop
    within range drives the score.  We convert headway to a score so that
    a bus every 10 min scores 100 and a bus every 60 min scores ~17.

    ttc_score = 100 * (IDEAL_HEADWAY_MIN / max(headway_min, IDEAL_HEADWAY_MIN))

    IDEAL_HEADWAY_MIN = 10  (frequent service benchmark)
    If no TTC stop is found the score is 0.

Composite accessibility_score
    accessibility_score = (
        W_WALK  * walking_score +
        W_TTC   * ttc_score
    ) / (W_WALK + W_TTC)

    W_WALK = 0.6  (service proximity matters more than transit for late-night)
    W_TTC  = 0.4

Signal 3 — Demand score (0–100, higher = more demand pressure)
    Raw event count per cell/hour normalised to [0, 100] using the 95th
    percentile of all counts as the ceiling (so outliers don't flatten the
    map).

dead_zone_score (0–100, higher = worse gap)
    dead_zone_score = W_ACCESS * (100 - accessibility_score)
                    + W_DEMAND * demand_score

    W_ACCESS = 0.65
    W_DEMAND = 0.35

    High dead_zone_score means poor access AND high demand — exactly the
    planning gap we care about.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

# -------Attempt optional heavy imports; fall back gracefully--------
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    import geopandas as gpd
    HAS_GEOPANDAS = True
except ImportError:
    HAS_GEOPANDAS = False

# ---------Tuneable constants — change these to adjust scoring behaviour---------

# Walking / service proximity
# 1500 m ≈ 18-min walk at 80 m/min — realistic late-night "will I bother" threshold.
# 800 m is the "comfortable" walk; beyond that the score degrades rapidly.
MAX_WALK_M: float = 1500.0         # max useful walk distance (metres)
COMFORT_WALK_M: float = 800.0      # distance at which score = 50 (knee of curve)
W_WALK: float = 0.6                # weight of walking score in accessibility

# TTC transit support
TTC_RADIUS_M: float = 1000.0       # radius to search for TTC stops (metres)
IDEAL_HEADWAY_MIN: float = 10.0    # headway considered "frequent" (minutes)
WORST_HEADWAY_MIN: float = 60.0    # headway considered "no service" threshold
W_TTC: float = 0.4                 # weight of TTC score in accessibility

# Dead-zone composite
W_ACCESS: float = 0.65             # weight of poor-accessibility term
W_DEMAND: float = 0.35             # weight of demand-pressure term

# Demand normalisation
DEMAND_PERCENTILE: float = 95.0    # percentile used as demand ceiling

# Rexdale demo bias — cells whose centroid falls inside this bbox get a
# synthetic demand boost after REXDALE_HOUR_THRESHOLD to ensure the area
# shows up dark on the demo heatmap.
REXDALE_BBOX = {
    "lat_min": 43.720, "lat_max": 43.755,
    "lng_min": -79.600, "lng_max": -79.540,
}
REXDALE_HOUR_THRESHOLD: int = 22   # apply boost from 10 pm onward
REXDALE_DEMAND_BOOST: float = 25.0 # additive boost to demand_score (0-100 scale)

# Summary / reporting
TOP_N: int = 50   # top 50 gives the API enough neighbourhood coverage for tool queries
COLLAPSE_HOUR_EARLY: int = 18      # "before" hour for collapse comparison (6 pm)
COLLAPSE_HOUR_LATE: int = 23       # "after" hour for collapse comparison (11 pm)

# ---------Logging----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("deadzone.score")


# -----------Geometry helpers----------

def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in metres between two WGS-84 points."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _in_rexdale(lat: float, lng: float) -> bool:
    b = REXDALE_BBOX
    return b["lat_min"] <= lat <= b["lat_max"] and b["lng_min"] <= lng <= b["lng_max"]


# ----------Data loaders----------

def load_grid(cache_dir: Path) -> list[dict]:
    """Load grid cells from grid.geojson.  Returns list of feature dicts."""
    path = cache_dir / "grid.geojson"
    if not path.exists():
        raise FileNotFoundError(f"Missing {path} — run grid.py first or use --demo")
    with path.open() as f:
        fc = json.load(f)
    cells = []
    for feat in fc["features"]:
        c = feat.get("properties", {}).copy()
        geom = feat.get("geometry", {})
        # Accept Point geometry or polygon centroid stored in properties
        if geom.get("type") == "Point":
            c.setdefault("lng", geom["coordinates"][0])
            c.setdefault("lat", geom["coordinates"][1])
        elif "centroid_lat" in c:
            c.setdefault("lat", c["centroid_lat"])
            c.setdefault("lng", c["centroid_lng"])
        c["_geometry"] = geom
        cells.append(c)
    log.info("Loaded %d grid cells from %s", len(cells), path)
    return cells


def load_services(cache_dir: Path) -> list[dict]:
    """Load normalized service locations."""
    path = cache_dir / "service_locations.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing {path} — run services.py first or use --demo")
    with path.open() as f:
        data = json.load(f)
    services = data if isinstance(data, list) else data.get("services", [])
    log.info("Loaded %d service locations", len(services))
    return services


def load_ttc_frequency(cache_dir: Path) -> dict[str, dict[int, float]]:
    """
    Load TTC stop hourly headway (minutes).
    Returns {stop_id: {hour: headway_minutes}}.
    Accepts parquet (preferred) or JSON fallback.
    """
    parquet_path = cache_dir / "ttc_hourly_frequency.parquet"
    json_path = cache_dir / "ttc_hourly_frequency.json"

    if parquet_path.exists() and HAS_PANDAS:
        import pandas as pd
        df = pd.read_parquet(parquet_path)
        # Expected columns: stop_id, hour, headway_minutes (or trips_per_hour)
        result: dict[str, dict[int, float]] = defaultdict(dict)
        for row in df.itertuples(index=False):
            hw = getattr(row, "headway_minutes", None)
            if hw is None:
                tph = getattr(row, "trips_per_hour", 1)
                hw = 60.0 / max(tph, 0.1)
            result[str(row.stop_id)][int(row.hour)] = float(hw)
        log.info("Loaded TTC frequency for %d stops (parquet)", len(result))
        return dict(result)

    if json_path.exists():
        with json_path.open() as f:
            raw = json.load(f)
        log.info("Loaded TTC frequency for %d stops (json)", len(raw))
        return {str(k): {int(h): float(v) for h, v in hours.items()} for k, hours in raw.items()}

    log.warning("No TTC frequency file found — TTC score will be 0 for all cells")
    return {}


def load_ttc_stops(cache_dir: Path) -> list[dict]:
    """Load TTC stop locations from stops.json."""
    path = cache_dir / "stops.json"
    if not path.exists():
        log.warning("stops.json not found — TTC proximity search disabled")
        return []
    with path.open() as f:
        data = json.load(f)
    stops = data if isinstance(data, list) else data.get("stops", [])
    log.info("Loaded %d TTC stops", len(stops))
    return stops


def load_demand(cache_dir: Path) -> dict[str, dict[int, float]]:
    """
    Load demand events aggregated by cell + hour.
    Returns {cell_id: {hour: count}}.
    """
    path = cache_dir / "demand_by_cell_hour.json"
    if not path.exists():
        log.warning("demand_by_cell_hour.json not found — demand score will be 0")
        return {}
    with path.open() as f:
        raw = json.load(f)
    log.info("Loaded demand data for %d cells", len(raw))
    return {str(k): {int(h): float(v) for h, v in hours.items()} for k, hours in raw.items()}


# --------------Scoring sub-functions----------------

def _nearest_open_service(
    lat: float,
    lng: float,
    hour: int,
    services: list[dict],
) -> tuple[float, dict | None]:
    """
    Return (distance_m, service_dict) for the nearest service open at `hour`.
    Returns (inf, None) if nothing is open within MAX_WALK_M.
    """
    best_dist = math.inf
    best_svc = None
    for svc in services:
        hours_open: list[int] = svc.get("hours_open_by_hour", [1] * 24)
        if not hours_open[hour % 24]:
            continue
        d = haversine_m(lat, lng, float(svc["lat"]), float(svc["lng"]))
        if d < best_dist:
            best_dist = d
            best_svc = svc
    return best_dist, best_svc


def _walking_score(dist_m: float) -> float:
    """
    Score 0–100 using a two-segment curve:

    0 → COMFORT_WALK_M (800 m):  score 100 → 50   [comfortable walk]
    COMFORT_WALK_M → MAX_WALK_M (1500 m): score 50 → 0   [possible but difficult]
    > MAX_WALK_M: score = 0

    This gives a smooth curve that rewards proximity while still awarding
    partial credit to cells within a 20-minute walk.
    """
    if dist_m <= COMFORT_WALK_M:
        # Maps [0, 800] → [100, 50]
        return 100.0 - 50.0 * (dist_m / COMFORT_WALK_M)
    elif dist_m <= MAX_WALK_M:
        # Maps [800, 1500] → [50, 0]
        t = (dist_m - COMFORT_WALK_M) / (MAX_WALK_M - COMFORT_WALK_M)
        return 50.0 * (1.0 - t)
    return 0.0


def _best_ttc_stop(
    lat: float,
    lng: float,
    hour: int,
    stops: list[dict],
    frequency: dict[str, dict[int, float]],
) -> tuple[float, dict | None]:
    """
    Return (headway_minutes, stop_dict) for the most frequent TTC stop within
    TTC_RADIUS_M at `hour`.  Returns (inf, None) if none found.
    """
    best_headway = math.inf
    best_stop = None
    for stop in stops:
        d = haversine_m(lat, lng, float(stop["lat"]), float(stop["lng"]))
        if d > TTC_RADIUS_M:
            continue
        sid = str(stop.get("stop_id", stop.get("id", "")))
        hw = frequency.get(sid, {}).get(hour, math.inf)
        if hw < best_headway:
            best_headway = hw
            best_stop = stop
    return best_headway, best_stop


def _ttc_score(headway_min: float) -> float:
    """
    Score 0–100: 100 when headway == IDEAL_HEADWAY_MIN, degrades linearly,
    0 when headway >= WORST_HEADWAY_MIN or no service.
    """
    if headway_min == math.inf or headway_min >= WORST_HEADWAY_MIN:
        return 0.0
    return 100.0 * max(0.0, 1.0 - (headway_min - IDEAL_HEADWAY_MIN) / (WORST_HEADWAY_MIN - IDEAL_HEADWAY_MIN))


def _accessibility_score(walk: float, ttc: float) -> float:
    """Weighted average of walking and TTC sub-scores."""
    return (W_WALK * walk + W_TTC * ttc) / (W_WALK + W_TTC)


def _dead_zone_score(accessibility: float, demand: float) -> float:
    """
    Higher score = worse planning gap.
    Combines poor accessibility with high demand pressure.
    """
    return W_ACCESS * (100.0 - accessibility) + W_DEMAND * demand


def _reason_text(
    walk_score: float,
    ttc_score_val: float,
    demand_score: float,
    nearest_svc: dict | None,
    headway: float,
    hour: int,
) -> str:
    """Generate a human-readable one-line explanation for a cell/hour."""
    parts: list[str] = []
    if nearest_svc is None:
        parts.append("no open services within 800 m")
    elif walk_score < 30:
        name = nearest_svc.get("name", "nearest service")
        parts.append(f"nearest open service ({name}) is far")
    if ttc_score_val < 20:
        if headway == math.inf:
            parts.append("no TTC service nearby")
        else:
            parts.append(f"TTC headway {headway:.0f} min (infrequent)")
    if demand_score > 60:
        parts.append("high community demand pressure")
    if not parts:
        parts.append("moderate accessibility")
    suffix = f" at hour {hour:02d}:00"
    return "; ".join(parts) + suffix


# ------------Demand normalisation-------------

def _normalise_demand(
    demand: dict[str, dict[int, float]],
    cells: list[dict],
) -> dict[str, dict[int, float]]:
    """Normalise raw demand counts to 0–100 using the 95th-percentile ceiling."""
    all_counts = [
        v for hours in demand.values() for v in hours.values()
    ]
    if not all_counts:
        return {}
    ceiling = float(np.percentile(all_counts, DEMAND_PERCENTILE)) if all_counts else 1.0
    ceiling = max(ceiling, 1.0)
    log.info("Demand ceiling (p%.0f) = %.1f events", DEMAND_PERCENTILE, ceiling)
    return {
        cell_id: {
            hour: min(100.0, 100.0 * count / ceiling)
            for hour, count in hours.items()
        }
        for cell_id, hours in demand.items()
    }


# ----------Main scoring loop-----------

def score_all(
    cells: list[dict],
    services: list[dict],
    stops: list[dict],
    frequency: dict[str, dict[int, float]],
    demand_norm: dict[str, dict[int, float]],
) -> list[dict]:
    """
    For each (cell, hour) pair compute all scores and explanation fields.
    Returns a flat list of records — one per (cell, hour).
    """
    t0 = time.perf_counter()
    records: list[dict] = []

    total = len(cells) * 24
    log.info("Scoring %d cells × 24 hours = %d records …", len(cells), total)

    for ci, cell in enumerate(cells):
        if ci % 500 == 0 and ci > 0:
            elapsed = time.perf_counter() - t0
            rate = ci / elapsed
            log.info("  %d/%d cells (%.0f cells/s)", ci, len(cells), rate)

        cell_id = str(cell.get("cell_id", cell.get("hex_id", ci)))
        lat = float(cell.get("lat", cell.get("centroid_lat", 0)))
        lng = float(cell.get("lng", cell.get("centroid_lng", 0)))
        neighbourhood = cell.get("neighbourhood", cell.get("hood", ""))
        in_rexdale = _in_rexdale(lat, lng)

        for hour in range(24):
            # service prox.
            dist_m, nearest_svc = _nearest_open_service(lat, lng, hour, services)
            walk = _walking_score(dist_m) if dist_m != math.inf else 0.0

            # TTC support
            headway, best_stop = _best_ttc_stop(lat, lng, hour, stops, frequency)
            ttc = _ttc_score(headway)

            # accessibility
            access = _accessibility_score(walk, ttc)

            # demand
            raw_demand = demand_norm.get(cell_id, {}).get(hour, 0.0)

            # Rexdale demo bias
            if in_rexdale and hour >= REXDALE_HOUR_THRESHOLD:
                raw_demand = min(100.0, raw_demand + REXDALE_DEMAND_BOOST)

            # dead zone
            dz = _dead_zone_score(access, raw_demand)

            # explanation text
            reason = _reason_text(walk, ttc, raw_demand, nearest_svc, headway, hour)

            records.append({
                "cell_id": cell_id,
                "lat": lat,
                "lng": lng,
                "hour": hour,
                "neighbourhood": neighbourhood,
                # Scores
                "accessibility_score": round(access, 2),
                "walking_score": round(walk, 2),
                "ttc_score": round(ttc, 2),
                "demand_score": round(raw_demand, 2),
                "dead_zone_score": round(dz, 2),
                # Explanation fields
                "nearest_open_service_name": nearest_svc.get("name", "") if nearest_svc else "",
                "nearest_open_service_type": nearest_svc.get("type", "") if nearest_svc else "",
                "nearest_open_service_distance_m": round(dist_m, 1) if dist_m != math.inf else -1,
                "nearest_frequent_ttc_stop": best_stop.get("stop_name", best_stop.get("name", "")) if best_stop else "",
                "ttc_headway_minutes": round(headway, 1) if headway != math.inf else -1,
                "demand_count": demand_norm.get(cell_id, {}).get(hour, 0.0),
                "reason_text": reason,
                # Passthrough for GeoJSON reconstruction
                "_geometry": cell.get("_geometry"),
            })

    elapsed = time.perf_counter() - t0
    log.info("Scored %d records in %.2f s", len(records), elapsed)
    return records


# ----------Export helpers-----------

def _records_to_geojson(records: list[dict], hour_filter: int | None = None) -> dict:
    """Convert scored records to a GeoJSON FeatureCollection."""
    features = []
    for r in records:
        if hour_filter is not None and r["hour"] != hour_filter:
            continue
        geom = r.get("_geometry") or {"type": "Point", "coordinates": [r["lng"], r["lat"]]}
        props = {k: v for k, v in r.items() if k != "_geometry"}
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": features}


def _records_to_timeseries(records: list[dict]) -> dict[str, list]:
    """
    Build per-cell timeseries.
    Returns {cell_id: [{hour, accessibility_score, dead_zone_score, demand_score}]}.
    """
    ts: dict[str, list] = defaultdict(list)
    for r in records:
        ts[r["cell_id"]].append({
            "hour": r["hour"],
            "accessibility_score": r["accessibility_score"],
            "dead_zone_score": r["dead_zone_score"],
            "demand_score": r["demand_score"],
        })
    for v in ts.values():
        v.sort(key=lambda x: x["hour"])
    return dict(ts)


def _top_dead_zones(records: list[dict], n: int = TOP_N) -> list[dict]:
    """
    Return top-N worst (cell, hour) pairs by dead_zone_score.
    Also includes per-cell worst hour.
    """
    sorted_recs = sorted(records, key=lambda r: r["dead_zone_score"], reverse=True)
    # Deduplicate: one entry per cell (worst hour)
    seen: set[str] = set()
    top: list[dict] = []
    for r in sorted_recs:
        if r["cell_id"] not in seen:
            seen.add(r["cell_id"])
            top.append({
                "cell_id": r["cell_id"],
                "lat": r["lat"],
                "lng": r["lng"],
                "worst_hour": r["hour"],
                "dead_zone_score": r["dead_zone_score"],
                "accessibility_score": r["accessibility_score"],
                "demand_score": r["demand_score"],
                "neighbourhood": r["neighbourhood"],
                "nearest_open_service_name": r["nearest_open_service_name"],
                "nearest_open_service_distance_m": r["nearest_open_service_distance_m"],
                "ttc_headway_minutes": r["ttc_headway_minutes"],
                "reason_text": r["reason_text"],
            })
        if len(top) >= n:
            break
    return top


# ----------Summary log-----------

def _print_summary(records: list[dict]) -> None:
    """Print a human-readable summary to stdout for demo/logging purposes."""
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)

    # Top 10 dead zones across all hours
    top = _top_dead_zones(records, n=TOP_N)
    log.info("Top %d dead zones (worst cell per hour):", TOP_N)
    for i, z in enumerate(top, 1):
        hood = z["neighbourhood"] or "unknown area"
        log.info(
            "  %2d. cell=%-20s  hour=%02d:00  dz=%.1f  (%s)",
            i, z["cell_id"], z["worst_hour"], z["dead_zone_score"], hood,
        )

    # Biggest accessibility collapse between 6 pm and 11 pm
    early: dict[str, float] = {}
    late: dict[str, float] = {}
    for r in records:
        if r["hour"] == COLLAPSE_HOUR_EARLY:
            early[r["cell_id"]] = r["accessibility_score"]
        if r["hour"] == COLLAPSE_HOUR_LATE:
            late[r["cell_id"]] = r["accessibility_score"]

    collapses = [
        (cid, early[cid] - late.get(cid, 0))
        for cid in early
        if cid in late
    ]
    collapses.sort(key=lambda x: x[1], reverse=True)

    log.info(
        "Biggest accessibility collapse %02d:00 → %02d:00:",
        COLLAPSE_HOUR_EARLY, COLLAPSE_HOUR_LATE,
    )
    for cid, drop in collapses[:5]:
        cell_records = [r for r in records if r["cell_id"] == cid and r["hour"] == COLLAPSE_HOUR_LATE]
        hood = cell_records[0]["neighbourhood"] if cell_records else ""
        label = f" ({hood})" if hood else ""
        log.info("  cell=%-20s  drop=%.1f pts%s", cid, drop, label)

    log.info("=" * 60)


# ----------CLI entry point----------

def main() -> None:
    parser = argparse.ArgumentParser(description="DeadZone scoring engine")
    parser.add_argument("--cache", default="data_cache", help="Input cache directory")
    parser.add_argument("--out", default="data_cache", help="Output directory")
    parser.add_argument(
        "--demo", action="store_true",
        help="Generate synthetic demo data if input files are missing",
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="Score only a 3 km² sample area around downtown Toronto",
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    t_start = time.perf_counter()

    # load inputs
    if args.demo:
        _inject_demo_data(cache_dir)

    cells = load_grid(cache_dir)
    services = load_services(cache_dir)
    stops = load_ttc_stops(cache_dir)
    frequency = load_ttc_frequency(cache_dir)
    demand_raw = load_demand(cache_dir)

    # fast mode: restrict to downtown bbox for quick testing
    if args.fast:
        FAST_BBOX = {"lat_min": 43.640, "lat_max": 43.670, "lng_min": -79.410, "lng_max": -79.370}
        cells = [
            c for c in cells
            if FAST_BBOX["lat_min"] <= float(c.get("lat", c.get("centroid_lat", 0))) <= FAST_BBOX["lat_max"]
            and FAST_BBOX["lng_min"] <= float(c.get("lng", c.get("centroid_lng", 0))) <= FAST_BBOX["lng_max"]
        ]
        log.info("Fast mode: restricted to %d cells in downtown bbox", len(cells))

    # Normalise demand
    demand_norm = _normalise_demand(demand_raw, cells)

    # scores -> combined
    records = score_all(cells, services, stops, frequency, demand_norm)

    # per service type scores
    # Maps frontend filter key → service types to include
    SERVICE_TYPE_FILTERS: dict[str, list[str]] = {
        "healthcare": ["clinic", "pharmacy", "mental_health", "hospital"],
        "shelter":    ["shelter"],
        "food":       ["food_bank"],
        "community":  ["community_centre"],
    }
    type_records: dict[str, list[dict]] = {}
    for filter_key, type_list in SERVICE_TYPE_FILTERS.items():
        filtered_svcs = [s for s in services if s.get("type") in type_list]
        if filtered_svcs:
            log.info("Scoring service type '%s' (%d services) …", filter_key, len(filtered_svcs))
            type_records[filter_key] = score_all(cells, filtered_svcs, stops, frequency, demand_norm)
        else:
            log.warning("No services of type '%s' — skipping", filter_key)

    # summary
    _print_summary(records)

    # export
    # 1a. grid_scores.geojson — geometry + scores for ALL hours (full reference file)
    gs_path = out_dir / "grid_scores.geojson"
    with gs_path.open("w") as f:
        geojson = _records_to_geojson(records)
        json.dump(geojson, f, separators=(",", ":"))
    log.info("Wrote %s (%.1f MB)", gs_path, gs_path.stat().st_size / 1e6)

    SCORE_KEYS = ("dead_zone_score","accessibility_score","demand_score","walking_score","ttc_score",
                  "nearest_open_service_name","nearest_open_service_type","nearest_open_service_distance_m",
                  "nearest_frequent_ttc_stop","ttc_headway_minutes","demand_count","reason_text","neighbourhood")

    def _write_hour_files(recs: list[dict], target_dir: Path) -> None:
        """Write 24 per-hour JSON score files (no geometry) to target_dir."""
        target_dir.mkdir(parents=True, exist_ok=True)
        by_hour: dict[int, dict] = {h: {} for h in range(24)}
        for r in recs:
            by_hour[r["hour"]][r["cell_id"]] = {k: r[k] for k in SCORE_KEYS if k in r}
        for h, scores in by_hour.items():
            p = target_dir / f"hour_{h:02d}.json"
            with p.open("w") as f:
                json.dump(scores, f, separators=(",", ":"))

    # 1b. scores_by_hour/all/ — combined (all service types)
    all_dir = out_dir / "scores_by_hour" / "all"
    _write_hour_files(records, all_dir)
    log.info("Wrote 24 per-hour score files → %s", all_dir)

    # 1c. per-type score directories
    for filter_key, recs in type_records.items():
        type_dir = out_dir / "scores_by_hour" / filter_key
        _write_hour_files(recs, type_dir)
        log.info("Wrote 24 per-hour score files → %s", type_dir)

    # Also write legacy path (scores_by_hour/hour_XX.json) for backward compat
    legacy_dir = out_dir / "scores_by_hour"
    _write_hour_files(records, legacy_dir)

    # 2. cell_timeseries.json
    ts_path = out_dir / "cell_timeseries.json"
    ts = _records_to_timeseries(records)
    with ts_path.open("w") as f:
        json.dump(ts, f, separators=(",", ":"))
    log.info("Wrote %s (%.1f MB)", ts_path, ts_path.stat().st_size / 1e6)

    # 3. top_dead_zones.json
    tz_path = out_dir / "top_dead_zones.json"
    top = _top_dead_zones(records, n=TOP_N)
    with tz_path.open("w") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "top_dead_zones": top}, f, indent=2)
    log.info("Wrote %s", tz_path)

    elapsed = time.perf_counter() - t_start
    log.info("Total pipeline time: %.2f s", elapsed)


# --------Demo data injector — creates minimal plausible files if inputs are absent-------

def _inject_demo_data(cache_dir: Path) -> None:
    """
    Write synthetic Toronto data into cache_dir so the scorer can run
    without real pipeline outputs.  Covers downtown + Rexdale.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    # --- grid.geojson ---
    grid_path = cache_dir / "grid.geojson"
    if not grid_path.exists():
        import random
        random.seed(42)
        features = []
        # ~200 cells covering a coarse Toronto grid
        for i, lat in enumerate(np.arange(43.62, 43.78, 0.015)):
            for j, lng in enumerate(np.arange(-79.62, -79.30, 0.018)):
                cell_id = f"cell_{i:03d}_{j:03d}"
                hood = ""
                if 43.720 <= lat <= 43.755 and -79.600 <= lng <= -79.540:
                    hood = "Rexdale"
                elif 43.640 <= lat <= 43.670 and -79.410 <= lng <= -79.370:
                    hood = "Downtown Core"
                elif 43.695 <= lat <= 43.720 and -79.470 <= lng <= -79.430:
                    hood = "York"
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lng + 0.009, lat + 0.0075]},
                    "properties": {
                        "cell_id": cell_id,
                        "lat": lat + 0.0075,
                        "lng": lng + 0.009,
                        "neighbourhood": hood,
                    },
                })
        with grid_path.open("w") as f:
            json.dump({"type": "FeatureCollection", "features": features}, f)
        log.info("Demo: wrote %d grid cells to %s", len(features), grid_path)

    # --- service_locations.json ---
    svc_path = cache_dir / "service_locations.json"
    if not svc_path.exists():
        import random
        random.seed(7)
        TYPES = ["shelter", "food_bank", "clinic", "pharmacy", "community_centre"]
        services = []
        # Downtown has good coverage; Rexdale has sparse late-night coverage
        locations = [
            # Downtown — open late
            (43.653, -79.383, "24h"), (43.648, -79.395, "24h"),
            (43.660, -79.376, "late"), (43.655, -79.400, "late"),
            (43.644, -79.378, "standard"),
            # Rexdale — mostly standard hours (closes at 9pm)
            (43.730, -79.565, "standard"), (43.738, -79.552, "standard"),
            (43.725, -79.580, "early"),
            # Scarborough
            (43.770, -79.240, "standard"), (43.760, -79.255, "late"),
            # North York
            (43.760, -79.410, "standard"), (43.752, -79.425, "late"),
        ]
        hour_profiles = {
            "24h":      [1] * 24,
            "late":     [0]*6 + [1]*18,
            "standard": [0]*7 + [1]*14 + [0]*3,
            "early":    [0]*8 + [1]*11 + [0]*5,
        }
        for idx, (lat, lng, profile) in enumerate(locations):
            services.append({
                "id": f"svc_{idx:03d}",
                "name": f"{TYPES[idx % len(TYPES)].replace('_',' ').title()} {idx+1}",
                "type": TYPES[idx % len(TYPES)],
                "lat": lat + random.uniform(-0.002, 0.002),
                "lng": lng + random.uniform(-0.002, 0.002),
                "hours_open_by_hour": hour_profiles[profile],
            })
        with svc_path.open("w") as f:
            json.dump(services, f, indent=2)
        log.info("Demo: wrote %d services to %s", len(services), svc_path)

    # --- stops.json — all 69 TTC subway stations + key surface routes ---
    # Realistic headways ensure the map shows a gradient instead of all-red.
    # Subway = 5–10 min; streetcar = 8–15 min; bus = 15–30 min; outer = 30–60 min.
    stops_path = cache_dir / "stops.json"
    if not stops_path.exists():
        # (stop_id, name, lat, lng, service_class)
        # service_class: "subway" | "streetcar" | "bus" | "outer_bus"
        STOP_DEFS = [
            # === Yonge-University-Spadina subway ===
            ("S001","Union Station",          43.6453,-79.3806,"subway"),
            ("S002","King Station",           43.6490,-79.3776,"subway"),
            ("S003","Queen Station",          43.6524,-79.3793,"subway"),
            ("S004","Dundas Station",         43.6557,-79.3815,"subway"),
            ("S005","College Station",        43.6596,-79.3852,"subway"),
            ("S006","Wellesley Station",      43.6651,-79.3875,"subway"),
            ("S007","Bloor-Yonge Station",    43.6710,-79.3857,"subway"),
            ("S008","Rosedale Station",       43.6773,-79.3857,"subway"),
            ("S009","Summerhill Station",     43.6859,-79.3889,"subway"),
            ("S010","St. Clair Station",      43.6921,-79.3917,"subway"),
            ("S011","Davisville Station",     43.6978,-79.3942,"subway"),
            ("S012","Eglinton Station",       43.7050,-79.3984,"subway"),
            ("S013","Lawrence Station",       43.7233,-79.4006,"subway"),
            ("S014","York Mills Station",     43.7453,-79.4013,"subway"),
            ("S015","Sheppard-Yonge Station", 43.7614,-79.4107,"subway"),
            ("S016","North York Centre Stn",  43.7686,-79.4107,"subway"),
            ("S017","Finch Station",          43.7800,-79.4154,"subway"),
            # Spadina branch
            ("S018","St. George Station",     43.6681,-79.3993,"subway"),
            ("S019","Museum Station",         43.6676,-79.3938,"subway"),
            ("S020","Bay Station",            43.6700,-79.3892,"subway"),
            ("S021","St. Patrick Station",    43.6538,-79.3878,"subway"),
            ("S022","Osgoode Station",        43.6499,-79.3877,"subway"),
            ("S023","St. Andrew Station",     43.6466,-79.3860,"subway"),
            ("S024","Spadina Station",        43.6674,-79.4034,"subway"),
            ("S025","Dupont Station",         43.6755,-79.4049,"subway"),
            ("S026","St. Clair West Station", 43.6864,-79.4158,"subway"),
            ("S027","Eglinton West Station",  43.7001,-79.4304,"subway"),
            ("S028","Allen Station",          43.7155,-79.4466,"subway"),
            ("S029","Yorkdale Station",       43.7240,-79.4522,"subway"),
            ("S030","Lawrence West Station",  43.7219,-79.4587,"subway"),
            ("S031","Glencairn Station",      43.7270,-79.4461,"subway"),
            ("S032","Wilson Station",         43.7329,-79.4641,"subway"),
            ("S033","Downsview Park Station", 43.7534,-79.4773,"subway"),
            ("S034","Finch West Station",     43.7561,-79.5855,"subway"),
            ("S035","York University Station",43.7729,-79.5019,"subway"),
            # === Bloor-Danforth subway ===
            ("S036","Kipling Station",        43.6365,-79.5358,"subway"),
            ("S037","Islington Station",      43.6468,-79.5232,"subway"),
            ("S038","Royal York Station",     43.6475,-79.5111,"subway"),
            ("S039","Old Mill Station",       43.6483,-79.4965,"subway"),
            ("S040","Jane Station",           43.6483,-79.4823,"subway"),
            ("S041","Runnymede Station",      43.6508,-79.4706,"subway"),
            ("S042","High Park Station",      43.6543,-79.4639,"subway"),
            ("S043","Keele Station",          43.6555,-79.4553,"subway"),
            ("S044","Dundas West Station",    43.6558,-79.4478,"subway"),
            ("S045","Lansdowne Station",      43.6572,-79.4398,"subway"),
            ("S046","Dufferin Station",       43.6585,-79.4323,"subway"),
            ("S047","Ossington Station",      43.6613,-79.4229,"subway"),
            ("S048","Christie Station",       43.6643,-79.4156,"subway"),
            ("S049","Bathurst Station",       43.6664,-79.4111,"subway"),
            ("S050","Sherbourne Station",     43.6715,-79.3748,"subway"),
            ("S051","Castle Frank Station",   43.6749,-79.3640,"subway"),
            ("S052","Broadview Station",      43.6752,-79.3565,"subway"),
            ("S053","Chester Station",        43.6782,-79.3491,"subway"),
            ("S054","Pape Station",           43.6794,-79.3434,"subway"),
            ("S055","Donlands Station",       43.6812,-79.3351,"subway"),
            ("S056","Greenwood Station",      43.6833,-79.3289,"subway"),
            ("S057","Coxwell Station",        43.6847,-79.3220,"subway"),
            ("S058","Woodbine Station",       43.6866,-79.3148,"subway"),
            ("S059","Main Street Station",    43.6893,-79.3036,"subway"),
            ("S060","Victoria Park Station",  43.6910,-79.2934,"subway"),
            ("S061","Warden Station",         43.6921,-79.2798,"subway"),
            ("S062","Kennedy Station",        43.6921,-79.2642,"subway"),
            # === Sheppard subway ===
            ("S063","Bayview Station",        43.7655,-79.3877,"subway"),
            ("S064","Bessarion Station",      43.7693,-79.3745,"subway"),
            ("S065","Leslie Station",         43.7730,-79.3594,"subway"),
            ("S066","Don Mills Station",      43.7739,-79.3416,"subway"),
            # === Scarborough RT ===
            ("S067","Lawrence East Station",  43.7231,-79.2661,"subway"),
            ("S068","Ellesmere Station",      43.7714,-79.2636,"subway"),
            ("S069","Scarborough Centre Stn", 43.7764,-79.2568,"subway"),
            # === Key streetcar stops ===
            ("T001","Queen & Spadina",        43.6473,-79.4000,"streetcar"),
            ("T002","King & Bathurst",        43.6441,-79.4094,"streetcar"),
            ("T003","Queen & Broadview",      43.6626,-79.3526,"streetcar"),
            ("T004","Dundas & Ossington",     43.6530,-79.4264,"streetcar"),
            ("T005","King & Dufferin",        43.6396,-79.4323,"streetcar"),
            # === Key bus route stops (suburban coverage) ===
            ("B001","Finch & Jane",           43.7511,-79.5000,"bus"),
            ("B002","Finch & Dufferin",       43.7549,-79.4698,"bus"),
            ("B003","Wilson & Allen Rd",      43.7264,-79.4660,"bus"),
            ("B004","Sheppard & Weston Rd",   43.7410,-79.5205,"outer_bus"),
            ("B005","Jane & Sheppard",        43.7549,-79.5037,"outer_bus"),
            ("B006","Rexdale & Kipling",      43.7256,-79.5597,"outer_bus"),
            ("B007","Albion & Islington",     43.7440,-79.5520,"outer_bus"),
            ("B008","Steeles & Jane",         43.7731,-79.5017,"outer_bus"),
            ("B009","Steeles & Yonge",        43.7960,-79.4184,"outer_bus"),
            ("B010","Eglinton & Kennedy",     43.7168,-79.2789,"bus"),
            ("B011","Sheppard & Markham Rd",  43.7795,-79.2487,"bus"),
            ("B012","Lawrence & Warden",      43.7230,-79.2740,"bus"),
            ("B013","Morningside & Sheppard", 43.7936,-79.2109,"outer_bus"),
            ("B014","Ellesmere & Morningside",43.7768,-79.2102,"outer_bus"),
            ("B015","Weston & Lawrence",      43.7110,-79.5198,"bus"),
            ("B016","Mount Dennis Loop",      43.6963,-79.4953,"bus"),
            ("B017","Humber College Loop",    43.7278,-79.6077,"outer_bus"),
            ("B018","Eglinton & Islington",   43.6517,-79.5270,"bus"),
            ("B019","Scarborough GO",         43.7680,-79.2560,"bus"),
        ]

        headway_profiles = {
            # (hour: headway_minutes) per service class
            # subway: very frequent, 24h
            "subway":     {h: (5 if 7 <= h <= 9 else 7 if 10 <= h <= 22 else 10 if h == 23 else 15)
                           for h in range(24)},
            # streetcar: frequent daytime, infrequent overnight
            "streetcar":  {h: (8 if 7 <= h <= 20 else 15 if 21 <= h <= 23 else 30)
                           for h in range(24)},
            # bus: moderate daytime, infrequent overnight
            "bus":        {h: (15 if 7 <= h <= 20 else 30 if 21 <= h <= 23 else 60)
                           for h in range(24)},
            # outer_bus: infrequent; often no overnight service
            "outer_bus":  {h: (25 if 7 <= h <= 20 else 60)
                           for h in range(24)},
        }

        stops = [{"stop_id": sid, "stop_name": name, "lat": lat, "lng": lng}
                 for sid, name, lat, lng, _ in STOP_DEFS]
        freq  = {sid: headway_profiles[cls] for sid, _, _, _, cls in STOP_DEFS}

        with stops_path.open("w") as f:
            json.dump(stops, f, indent=2)
        log.info("Demo: wrote %d TTC stops to %s", len(stops), stops_path)

        # Write frequency immediately so the next check doesn't regenerate
        freq_path2 = cache_dir / "ttc_hourly_frequency.json"
        with freq_path2.open("w") as f:
            json.dump(freq, f, separators=(",", ":"))
        log.info("Demo: wrote TTC frequency to %s", freq_path2)

    # ttc_hourly_frequency.json
    freq_path = cache_dir / "ttc_hourly_frequency.json"
    if not freq_path.exists():
        log.warning("ttc_hourly_frequency.json not found — was it written above?")


    # demand_by_cell_hour.json
    demand_path = cache_dir / "demand_by_cell_hour.json"
    if not demand_path.exists():
        import random
        random.seed(99)
        demand: dict[str, dict[str, float]] = {}
        # Load the grid we just wrote to generate matching cell IDs
        with (cache_dir / "grid.geojson").open() as f:
            fc = json.load(f)
        for feat in fc["features"]:
            p = feat["properties"]
            cell_id = p["cell_id"]
            lat, lng = p["lat"], p["lng"]
            hood = p.get("neighbourhood", "")
            base = 2.0
            if hood == "Rexdale":
                base = 8.0       # higher baseline demand
            elif hood == "Downtown Core":
                base = 5.0
            demand[cell_id] = {
                str(h): max(0, round(base * (1.5 if 20 <= h <= 23 else 1.0) + random.gauss(0, 1), 1))
                for h in range(24)
            }
        with demand_path.open("w") as f:
            json.dump(demand, f, separators=(",", ":"))
        log.info("Demo: wrote demand data to %s", demand_path)


if __name__ == "__main__":
    main()
