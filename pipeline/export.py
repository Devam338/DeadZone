"""
pipeline/export.py — Copy pipeline outputs to frontend/public/data/.

Validates that all required files exist in data_cache/, then copies
them to frontend/public/data/ so the Next.js app can serve them as
static assets.  Also writes a manifest.json for cache-busting.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger("deadzone.export")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

REQUIRED_FILES = [
    "grid_scores.geojson",
    "cell_timeseries.json",
    "top_dead_zones.json",
    "service_locations.json",
    "stops.json",
    "grid.geojson",
]

OPTIONAL_FILES = [
    "graph_stats.json",
    "network_walk_distances.json",
]

SCORE_HOURS_DIR = "scores_by_hour"


def export_to_frontend(cache_dir: Path, frontend_data_dir: Path) -> None:
    t0 = time.perf_counter()
    frontend_data_dir.mkdir(parents=True, exist_ok=True)

    missing = [f for f in REQUIRED_FILES if not (cache_dir / f).exists()]
    if missing:
        log.error("Missing required files: %s", missing)
        raise FileNotFoundError(f"Run the full pipeline first. Missing: {missing}")

    manifest: dict = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": {},
    }

    for fname in REQUIRED_FILES + OPTIONAL_FILES:
        src = cache_dir / fname
        if not src.exists():
            continue
        dst = frontend_data_dir / fname
        shutil.copy2(src, dst)
        size_kb = round(dst.stat().st_size / 1024, 1)
        manifest["files"][fname] = {"size_kb": size_kb}
        log.info("  Copied %-40s  %7.1f KB", fname, size_kb)

    # Copy per-hour score files (legacy flat + per-type subdirs)
    src_hours = cache_dir / SCORE_HOURS_DIR
    if src_hours.exists():
        dst_hours = frontend_data_dir / SCORE_HOURS_DIR
        dst_hours.mkdir(exist_ok=True)
        total = 0
        # Copy flat legacy files
        for f in src_hours.glob("hour_*.json"):
            shutil.copy2(f, dst_hours / f.name)
            total += 1
        # Copy per-type subdirectories
        for subdir in src_hours.iterdir():
            if subdir.is_dir():
                dst_sub = dst_hours / subdir.name
                dst_sub.mkdir(exist_ok=True)
                for f in subdir.glob("hour_*.json"):
                    shutil.copy2(f, dst_sub / f.name)
                    total += 1
        log.info("  Copied %d per-hour score files (all types) → %s", total, dst_hours)

    manifest_path = frontend_data_dir / "manifest.json"
    with manifest_path.open("w") as f:
        json.dump(manifest, f, indent=2)

    log.info("Export complete → %s  (%.2f s)", frontend_data_dir, time.perf_counter() - t0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export pipeline outputs to frontend")
    parser.add_argument("--cache", default="data_cache")
    parser.add_argument("--frontend", default="frontend/public/data")
    args = parser.parse_args()
    export_to_frontend(Path(args.cache), Path(args.frontend))


if __name__ == "__main__":
    main()
