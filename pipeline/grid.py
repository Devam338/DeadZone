"""
pipeline/grid.py — Build H3 hexagon grid over Toronto.

Outputs
-------
data_cache/grid.geojson  — FeatureCollection of hex polygons with centroid props
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("deadzone.grid")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

# Toronto bounding box (with a small buffer beyond city limits)
TORONTO_BBOX = {
    "lat_min": 43.58,
    "lat_max": 43.86,
    "lng_min": -79.64,
    "lng_max": -79.12,
}

# H3 resolution: 8 ≈ 0.74 km² per hex, good city-scale granularity
# Use 7 (≈5.2 km²) for --fast mode
DEFAULT_RESOLUTION = 8
FAST_RESOLUTION = 7


# ------H3 compatibility shim (handles both v3 and v4 APIs)--------

def _import_h3():
    import h3 as _h3
    return _h3


def _polyfill(bbox: dict, resolution: int) -> set:
    h3 = _import_h3()
    polygon = {
        "type": "Polygon",
        "coordinates": [[
            [bbox["lng_min"], bbox["lat_min"]],
            [bbox["lng_max"], bbox["lat_min"]],
            [bbox["lng_max"], bbox["lat_max"]],
            [bbox["lng_min"], bbox["lat_max"]],
            [bbox["lng_min"], bbox["lat_min"]],
        ]],
    }
    # Try v4 API first, then v3
    if hasattr(h3, "geo_to_cells"):
        return h3.geo_to_cells(polygon, resolution)
    return h3.polyfill_geojson(polygon, resolution)


def _cell_to_latlng(h3, cell: str) -> tuple[float, float]:
    if hasattr(h3, "cell_to_latlng"):
        return h3.cell_to_latlng(cell)
    return h3.h3_to_geo(cell)


def _cell_to_boundary(h3, cell: str) -> list[tuple[float, float]]:
    """Returns list of (lat, lng) tuples."""
    if hasattr(h3, "cell_to_boundary"):
        return h3.cell_to_boundary(cell)
    return h3.h3_to_geo_boundary(cell)


# ------Toronto shoreline filter — removes Lake Ontario cells---------
# The bbox includes parts of Lake Ontario south of the city.
# We approximate the shoreline with a step function: below these lat thresholds
# (per lng band) the cell centroid is in the water.

def _is_water_cell(lat: float, lng: float) -> bool:
    """Return True if cell centroid is in Lake Ontario, not on Toronto land."""
    # Anything this far south is definitely in the lake
    if lat < 43.595:
        return True
    # Etobicoke / Mimico waterfront: shore is around 43.608
    if lng < -79.50 and lat < 43.612:
        return True
    # Humber Bay / Exhibition area
    if -79.50 <= lng < -79.44 and lat < 43.620:
        return True
    # Central waterfront (Queens Quay / Harbourfront area)
    if -79.44 <= lng < -79.36 and lat < 43.630:
        return True
    # Port Lands / Cherry Beach
    if -79.36 <= lng < -79.30 and lat < 43.635:
        return True
    # Scarborough (shore curves northward east of Port Union)
    if -79.30 <= lng < -79.20 and lat < 43.650:
        return True
    if lng >= -79.20 and lat < 43.730:
        return True
    return False


# ---------------------------------------------------------------------------
# Neighbourhood lookup — comprehensive coverage of Toronto
# Every part of the city should resolve to a name; a cell that misses all
# boxes falls back to the nearest district label.
# Format: (name, lat_min, lat_max, lng_min, lng_max)
# ---------------------------------------------------------------------------

NEIGHBOURHOODS = [
    # Core / Inner city
    ("Financial District",  43.644, 43.652, -79.385, -79.370),
    ("Downtown Core",       43.638, 43.660, -79.406, -79.368),
    ("Harbourfront",        43.630, 43.643, -79.400, -79.360),
    ("Kensington Market",   43.652, 43.665, -79.408, -79.392),
    ("Chinatown",           43.651, 43.660, -79.400, -79.388),
    ("Distillery District", 43.648, 43.660, -79.360, -79.345),
    ("St. Lawrence",        43.647, 43.658, -79.374, -79.358),
    ("Corktown",            43.648, 43.658, -79.360, -79.345),
    # West
    ("Parkdale",            43.632, 43.650, -79.448, -79.420),
    ("Roncesvalles",        43.640, 43.660, -79.458, -79.440),
    ("High Park",           43.645, 43.666, -79.470, -79.454),
    ("Junction",            43.660, 43.680, -79.478, -79.455),
    ("Bloor West Village",  43.647, 43.665, -79.478, -79.458),
    ("Etobicoke North",     43.740, 43.800, -79.570, -79.490),
    ("Etobicoke South",     43.625, 43.670, -79.570, -79.490),
    ("Mimico",              43.612, 43.632, -79.510, -79.470),
    ("Long Branch",         43.598, 43.620, -79.570, -79.525),
    ("Rexdale",             43.715, 43.758, -79.615, -79.535),
    ("Humber Summit",       43.756, 43.800, -79.570, -79.515),
    ("Thistletown",         43.710, 43.740, -79.580, -79.540),
    # Central / Midtown
    ("Annex",               43.664, 43.676, -79.415, -79.394),
    ("Midtown",             43.675, 43.700, -79.412, -79.385),
    ("Forest Hill",         43.685, 43.710, -79.420, -79.400),
    ("Davisville",          43.698, 43.714, -79.400, -79.375),
    ("Eglinton",            43.703, 43.718, -79.415, -79.375),
    ("Lawrence Park",       43.718, 43.740, -79.410, -79.375),
    ("Leaside",             43.706, 43.726, -79.360, -79.335),
    # North
    ("North York Centre",   43.755, 43.790, -79.425, -79.388),
    ("Willowdale",          43.770, 43.815, -79.430, -79.390),
    ("Don Mills",           43.730, 43.770, -79.360, -79.310),
    ("York Mills",          43.745, 43.770, -79.400, -79.360),
    ("Downsview",           43.735, 43.765, -79.485, -79.440),
    ("Jane & Finch",        43.755, 43.780, -79.510, -79.475),
    ("Black Creek",         43.720, 43.755, -79.510, -79.470),
    # East
    ("Leslieville",         43.655, 43.673, -79.335, -79.308),
    ("Riverside",           43.650, 43.665, -79.346, -79.330),
    ("East Danforth",       43.671, 43.690, -79.340, -79.290),
    ("Danforth",            43.676, 43.696, -79.360, -79.340),
    ("East York",           43.686, 43.720, -79.344, -79.285),
    ("The Beaches",         43.666, 43.686, -79.310, -79.270),
    ("Upper Beaches",       43.682, 43.700, -79.305, -79.270),
    ("Woodbine",            43.674, 43.698, -79.320, -79.300),
    # Scarborough
    ("Scarborough Town",    43.770, 43.800, -79.270, -79.220),
    ("Scarborough",         43.735, 43.775, -79.280, -79.180),
    ("Malvern",             43.790, 43.830, -79.240, -79.180),
    ("Rouge",               43.810, 43.850, -79.190, -79.125),
    ("Agincourt",           43.785, 43.825, -79.300, -79.250),
    ("Wexford",             43.735, 43.768, -79.315, -79.268),
    ("Cliffside",           43.715, 43.745, -79.270, -79.220),
    # York / Inner west
    ("York",                43.678, 43.710, -79.490, -79.444),
    ("Weston",              43.697, 43.730, -79.522, -79.490),
    ("Mount Dennis",        43.693, 43.718, -79.498, -79.468),
]


def _neighbourhood(lat: float, lng: float) -> str:
    """Return the name of the neighbourhood the point falls in, or a district fallback."""
    for name, lat_min, lat_max, lng_min, lng_max in NEIGHBOURHOODS:
        if lat_min <= lat <= lat_max and lng_min <= lng <= lng_max:
            return name
    # Fallback: broad district labels so nothing shows as a raw hex code
    if lng < -79.49:
        if lat > 43.73:
            return "NW Toronto"
        return "Etobicoke"
    if lng > -79.25:
        return "East Scarborough"
    if lat > 43.78:
        return "North Toronto"
    if lat > 43.72:
        return "North York"
    if lat < 43.65:
        return "South Toronto"
    return "Central Toronto"


# --------Main build function---------

def build_grid(cache_dir: Path, resolution: int = DEFAULT_RESOLUTION) -> list[dict]:
    t0 = time.perf_counter()
    h3 = _import_h3()

    cells = _polyfill(TORONTO_BBOX, resolution)
    log.info("H3 resolution %d: %d cells over Toronto bbox", resolution, len(cells))

    water_removed = 0
    features = []
    for cell in cells:
        lat, lng = _cell_to_latlng(h3, cell)

        # Skip cells whose centroid is in Lake Ontario (obviously no accessiblity)
        if _is_water_cell(lat, lng):
            water_removed += 1
            continue

        boundary = _cell_to_boundary(h3, cell)
        # GeoJSON polygon coords are [lng, lat]
        ring = [[b[1], b[0]] for b in boundary]
        ring.append(ring[0])  # close polygon

        hood = _neighbourhood(lat, lng)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
                "cell_id": cell,
                "hex_id": cell,
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "resolution": resolution,
                "neighbourhood": hood,
            },
        })

    fc = {"type": "FeatureCollection", "features": features}
    out_path = cache_dir / "grid.geojson"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(fc, f, separators=(",", ":"))

    log.info("Wrote %d land cells to %s  (%d water cells removed)  (%.2f s)",
             len(features), out_path, water_removed, time.perf_counter() - t0)
    return features


# --------CLI---------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build H3 hex grid over Toronto")
    parser.add_argument("--cache", default="data_cache")
    parser.add_argument("--fast", action="store_true", help="Use lower resolution for fast mode")
    args = parser.parse_args()

    res = FAST_RESOLUTION if args.fast else DEFAULT_RESOLUTION
    build_grid(Path(args.cache), resolution=res)


if __name__ == "__main__":
    main()
