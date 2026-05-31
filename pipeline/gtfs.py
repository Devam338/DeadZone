"""
pipeline/gtfs.py — Parse TTC GTFS feed into stop-level hourly headway.

Downloads the TTC GTFS zip from Toronto Open Data, extracts stop_times,
trips, stops, and calendar, then computes trips_per_hour per stop per
hour-of-day, converting to headway_minutes.

Outputs
-------
data_cache/stops.json                  — stop metadata [{stop_id, stop_name, lat, lng}]
data_cache/ttc_hourly_frequency.json   — {stop_id: {hour: headway_minutes}}
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import requests

log = logging.getLogger("deadzone.gtfs")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

# Toronto Open Data GTFS package ID
GTFS_URL = "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/ttc-routes-and-schedules/resource/0f5c7a65-6e89-4ff0-80cf-37e9e23e66a1/download/ttc_google_transit.zip"
GTFS_FALLBACK_URL = "https://transitfeeds.com/p/toronto-transit-commission/30/latest/download"

# Headway cap: stops with no service in a given hour get this value
NO_SERVICE_HEADWAY = 999.0
MAX_USEFUL_HEADWAY = 60.0  # anything >= 60 min treated as "infrequent"!!!!!!!


def _download_gtfs(cache_dir: Path) -> Path:
    """Download GTFS zip, cache it locally. Returns path to zip file."""
    zip_path = cache_dir / "ttc_gtfs.zip"
    if zip_path.exists():
        log.info("Using cached GTFS zip at %s", zip_path)
        return zip_path

    log.info("Downloading TTC GTFS from Toronto Open Data …")
    cache_dir.mkdir(parents=True, exist_ok=True)
    for url in [GTFS_URL, GTFS_FALLBACK_URL]:
        try:
            resp = requests.get(url, timeout=60, stream=True)
            resp.raise_for_status()
            with zip_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
            log.info("Downloaded GTFS zip (%.1f MB)", zip_path.stat().st_size / 1e6)
            return zip_path
        except Exception as exc:
            log.warning("Download from %s failed: %s", url, exc)

    raise RuntimeError("Could not download TTC GTFS. Use --demo flag to generate seed data.")


def _read_csv_from_zip(zf: zipfile.ZipFile, name: str) -> list[dict]:
    """Read a CSV file from the zip into a list of dicts."""
    # Some zips have subdirectories
    candidates = [n for n in zf.namelist() if n.endswith(name)]
    if not candidates:
        raise KeyError(f"{name} not found in GTFS zip")
    with zf.open(candidates[0]) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        return list(reader)


def _parse_time_seconds(t: str) -> int:
    """Parse HH:MM:SS (possibly > 24h for overnight trips) to seconds."""
    parts = t.strip().split(":")
    if len(parts) != 3:
        return 0
    h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    return h * 3600 + m * 60 + s


def parse_gtfs(cache_dir: Path) -> tuple[list[dict], dict[str, dict[int, float]]]:
    """
    Parse GTFS and return (stops_list, frequency_dict).

    frequency_dict: {stop_id: {hour_0_23: headway_minutes}}
    For hours with no service the headway is NO_SERVICE_HEADWAY.
    """
    t0 = time.perf_counter()
    zip_path = _download_gtfs(cache_dir)

    with zipfile.ZipFile(zip_path) as zf:
        log.info("Parsing stops …")
        raw_stops = _read_csv_from_zip(zf, "stops.txt")

        log.info("Parsing trips …")
        raw_trips = _read_csv_from_zip(zf, "trips.txt")

        log.info("Parsing stop_times (%d+ rows expected) …", 500_000)
        raw_st = _read_csv_from_zip(zf, "stop_times.txt")

        # Try to load calendar to identify weekday service
        try:
            raw_cal = _read_csv_from_zip(zf, "calendar.txt")
        except KeyError:
            raw_cal = []

    # Build set of weekday service IDs
    weekday_service_ids: set[str] = set()
    for row in raw_cal:
        if row.get("monday") == "1":
            weekday_service_ids.add(row["service_id"])
    # If no calendar, use all service IDs (calendar_dates only feeds)
    use_all = len(weekday_service_ids) == 0

    # Map trip_id → service_id
    trip_to_service: dict[str, str] = {r["trip_id"]: r["service_id"] for r in raw_trips}

    # Count trips per stop per hour-of-day (mod 24)
    # trips_per_hour[stop_id][hour] = count
    trips_per_hour: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    log.info("Aggregating %d stop_time rows …", len(raw_st))
    for row in raw_st:
        if row.get("pickup_type") == "1":  # no pickup
            continue
        trip_id = row["trip_id"]
        svc = trip_to_service.get(trip_id, "")
        if not use_all and svc not in weekday_service_ids:
            continue
        dep = row.get("departure_time", row.get("arrival_time", ""))
        if not dep:
            continue
        sec = _parse_time_seconds(dep)
        hour = (sec // 3600) % 24
        stop_id = row["stop_id"]
        trips_per_hour[stop_id][hour] += 1

    # Convert to headway
    frequency: dict[str, dict[int, float]] = {}
    for stop_id, hours in trips_per_hour.items():
        hw: dict[int, float] = {}
        for h in range(24):
            tph = hours.get(h, 0)
            hw[h] = round(60.0 / tph, 1) if tph > 0 else NO_SERVICE_HEADWAY
        frequency[stop_id] = hw

    # Build stops list
    stops = [
        {
            "stop_id": s["stop_id"],
            "stop_name": s.get("stop_name", ""),
            "lat": float(s["stop_lat"]),
            "lng": float(s["stop_lon"]),
        }
        for s in raw_stops
        if s.get("stop_lat") and s.get("stop_lon")
    ]

    log.info(
        "Parsed %d stops, %d with frequency data  (%.1f s)",
        len(stops), len(frequency), time.perf_counter() - t0,
    )
    return stops, frequency


def write_outputs(cache_dir: Path, stops: list[dict], frequency: dict[str, dict[int, float]]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)

    stops_path = cache_dir / "stops.json"
    with stops_path.open("w") as f:
        json.dump(stops, f, separators=(",", ":"))
    log.info("Wrote %s (%.1f MB)", stops_path, stops_path.stat().st_size / 1e6)

    freq_path = cache_dir / "ttc_hourly_frequency.json"
    with freq_path.open("w") as f:
        json.dump(frequency, f, separators=(",", ":"))
    log.info("Wrote %s (%.1f MB)", freq_path, freq_path.stat().st_size / 1e6)


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse TTC GTFS feed")
    parser.add_argument("--cache", default="data_cache")
    args = parser.parse_args()

    cache_dir = Path(args.cache)
    stops, frequency = parse_gtfs(cache_dir)
    write_outputs(cache_dir, stops, frequency)


if __name__ == "__main__":
    main()
