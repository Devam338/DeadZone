"""
scripts/generate_seed.py — Generate a complete offline demo dataset.

Runs all seed generators and the scoring engine so the app works
with zero network calls and zero real data.

Usage:
    python scripts/generate_seed.py
    python scripts/generate_seed.py --fast   # smaller grid, faster
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("deadzone.seed")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate DeadZone demo dataset")
    parser.add_argument("--cache", default="data_cache")
    parser.add_argument("--fast", action="store_true", help="Smaller grid, faster run")
    args = parser.parse_args()

    cache_dir = Path(args.cache)
    cache_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    log.info("Generating DeadZone seed dataset → %s", cache_dir)

    # 1. Grid
    log.info("[1/5] Building H3 grid …")
    from pipeline.grid import build_grid, FAST_RESOLUTION, DEFAULT_RESOLUTION
    resolution = FAST_RESOLUTION if args.fast else DEFAULT_RESOLUTION
    build_grid(cache_dir, resolution=resolution)

    # 2. Services (seed)
    log.info("[2/5] Generating service locations …")
    from pipeline.services import build_services
    build_services(cache_dir, use_seed=True)

    # 3. Demand (seed)
    log.info("[3/5] Generating demand data …")
    from pipeline.demand import build_demand
    build_demand(cache_dir, use_seed=True, resolution=resolution)

    # 4. Scoring — use the demo injector inside score_accessibility
    log.info("[4/5] Running scoring engine …")
    # We already have grid + services + demand; just need stops + frequency.
    # The score_accessibility --demo flag handles that.
    import subprocess, sys
    cmd = [
        sys.executable, "pipeline/score_accessibility.py",
        "--cache", str(cache_dir),
        "--out", str(cache_dir),
        "--demo",
    ]
    if args.fast:
        cmd.append("--fast")
    result = subprocess.run(cmd, cwd=Path(__file__).parent.parent)
    if result.returncode != 0:
        log.error("Scoring engine failed")
        sys.exit(1)

    # 5. Export to frontend
    log.info("[5/5] Exporting to frontend …")
    from pipeline.export import export_to_frontend
    export_to_frontend(cache_dir, Path("frontend/public/data"))

    log.info("Seed dataset complete in %.1f s", time.perf_counter() - t0)
    log.info("")
    log.info("Next steps:")
    log.info("  cd frontend && npm install && npm run dev")
    log.info("  python -m uvicorn api.main:app --reload  (in a separate terminal)")


if __name__ == "__main__":
    main()
