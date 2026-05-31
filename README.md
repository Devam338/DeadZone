# DeadZone

Maps Toronto's temporal public-service accessibility gaps after 9 PM.
Ingests TTC GTFS, 311 demand signals, and service location data to produce
a 24-hour animated dead-zone heatmap with an LLM planning assistant.

---

## Quick start (offline demo — zero real data)

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Generate seed data + score + export to frontend
python scripts/generate_seed.py

# 3. Install and start frontend
cd frontend && npm install && npm run dev

# 4. Start API (separate terminal, from project root)
python -m uvicorn api.main:app --reload --port 8000
```

Open http://localhost:3000

---

## Fast mode (smaller grid, ~30 seconds)

```bash
python scripts/generate_seed.py --fast
```

---

## Full pipeline (real TTC GTFS + 311 data, ~10–15 min first run)

```bash
python scripts/run_pipeline.py
```

Flags:
- `--fast`         Downtown bbox only, no OSM download
- `--skip-graph`   Skip cuGraph/NetworkX build (saves ~5 min)
- `--demo`         Alias for generate_seed.py

---

## Run individual pipeline steps

```bash
python pipeline/grid.py      --cache data_cache          # H3 hex grid
python pipeline/gtfs.py      --cache data_cache          # TTC GTFS → headways
python pipeline/services.py  --cache data_cache --seed   # Service locations
python pipeline/demand.py    --cache data_cache --seed   # 311 demand events
python pipeline/graph.py     --cache data_cache          # cuGraph/NetworkX
python pipeline/score_accessibility.py --cache data_cache --out data_cache
python pipeline/export.py    --cache data_cache          # → frontend/public/data/
```

---

## LLM setup (optional)

Set one env var before starting the API:

```bash
# NVIDIA Nemotron (preferred)
export NVIDIA_API_KEY=your_key_here

# OpenAI fallback
export OPENAI_API_KEY=your_key_here

# Neither → smart mock using real cell data (works fine for demo)
```

---

## Architecture

```
data pipeline (Python)
├── grid.py          H3 res-8 hexagons over Toronto (~2000 cells)
├── gtfs.py          TTC GTFS → stop-level hourly headway
├── services.py      Normalized service locations (seed or real)
├── demand.py        311 feed → demand counts per cell/hour
├── graph.py         cuGraph (GPU) / NetworkX (CPU) + OSMnx walk routing
├── score_accessibility.py   Dead zone scoring (transparent formula)
└── export.py        Copy outputs to frontend/public/data/

api (FastAPI)
└── main.py          POST /query → Nemotron / OpenAI / mock LLM

frontend (Next.js + Leaflet)
├── MapView          Dark hex heatmap, animated by hour
├── TimeSlider       0–23h slider + play button
├── DrillDown        Click-to-inspect cell panel
└── PlanningBox      Natural language planning queries + brief export
```

## Scoring formula

```
walking_score    = 100 × max(0, 1 − dist_m / 800)
ttc_score        = 100 × max(0, 1 − (headway − 10) / 50)
accessibility    = (0.6 × walking + 0.4 × ttc)
dead_zone_score  = 0.65 × (100 − accessibility) + 0.35 × demand_score
```

All weights are named constants at the top of `pipeline/score_accessibility.py`.

## GPU compute story

The pipeline detects RAPIDS cuGraph at runtime:
- **GPU box**: cuGraph adjacency graph on CUDA, dramatically faster edge construction.
- **CPU fallback**: NetworkX + scipy — same code path, slightly slower, no code changes needed.

Check graph backend in the header badge on the map UI.
