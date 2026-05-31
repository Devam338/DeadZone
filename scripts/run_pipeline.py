"""
scripts/run_pipeline.py — Full DeadZone pipeline orchestrator.

Modes:
  default  : Download real data (GTFS + 311), build graph, score, export.
  --fast   : Downtown bbox only, seed services, no OSM download.
  --demo   : Pure seed data — no network calls, works offline.
  --skip-graph : Skip OSMnx/cuGraph step (saves ~5 min).

Usage:
    python scripts/run_pipeline.py
    python scripts/run_pipeline.py --fast
    python scripts/run_pipeline.py --demo
    python scripts/run_pipeline.py --skip-graph
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("deadzone.pipeline")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

STEPS = [
    ("Grid",       "pipeline/grid.py"),
    ("Services",   "pipeline/services.py"),
    ("GTFS",       "pipeline/gtfs.py"),
    ("Demand",     "pipeline/demand.py"),
    ("Graph",      "pipeline/graph.py"),
    ("Scoring",    "pipeline/score_accessibility.py"),
    ("Export",     "pipeline/export.py"),
]


def run_step(script: str, extra_args: list[str], cache: str) -> int:
    cmd = [sys.executable, script, "--cache", cache] + extra_args
    log.info("  $ %s", " ".join(cmd))
    t0 = time.perf_counter()
    result = subprocess.run(cmd, cwd=Path(__file__).parent.parent)
    elapsed = time.perf_counter() - t0
    status = "OK" if result.returncode == 0 else "FAILED"
    log.info("  → %s  (%.1f s)", status, elapsed)
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Run DeadZone pipeline")
    parser.add_argument("--cache", default="data_cache")
    parser.add_argument("--fast", action="store_true", help="Small area, fast mode")
    parser.add_argument("--demo", action="store_true", help="Seed data only, no network")
    parser.add_argument("--skip-graph", action="store_true", help="Skip graph build step")
    args = parser.parse_args()

    if args.demo:
        log.info("Demo mode — running generate_seed.py")
        result = subprocess.run(
            [sys.executable, "scripts/generate_seed.py", "--cache", args.cache]
            + (["--fast"] if args.fast else []),
            cwd=Path(__file__).parent.parent,
        )
        sys.exit(result.returncode)

    t_total = time.perf_counter()
    failures: list[str] = []

    for step_name, script in STEPS:
        if step_name == "Graph" and args.skip_graph:
            log.info("[SKIP] %s", step_name)
            continue

        log.info("=" * 50)
        log.info("STEP: %s", step_name)

        extra: list[str] = []
        if args.fast:
            extra.append("--fast")
        if args.demo and step_name in ("Services", "Demand"):
            extra.append("--seed")
        if step_name == "Scoring" and args.demo:
            extra.append("--demo")
        if step_name == "Export":
            # export takes --frontend not --cache
            rc = run_step(script, [], args.cache)
        else:
            rc = run_step(script, extra, args.cache)

        if rc != 0:
            failures.append(step_name)
            log.error("Step %s FAILED — continuing with remaining steps", step_name)

    log.info("=" * 50)
    elapsed = time.perf_counter() - t_total
    if failures:
        log.error("Pipeline completed with failures: %s  (%.1f s)", failures, elapsed)
        sys.exit(1)
    else:
        log.info("Pipeline completed successfully in %.1f s", elapsed)
        log.info("")
        log.info("Run the app:")
        log.info("  cd frontend && npm run dev")
        log.info("  python -m uvicorn api.main:app --reload")


if __name__ == "__main__":
    main()
