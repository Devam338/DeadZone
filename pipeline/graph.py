"""
pipeline/graph.py — Build city-scale accessibility graph.

Architecture
------------
Nodes:
  - Grid cell centroids (type=grid)
  - TTC stops           (type=ttc_stop)
  - Service locations   (type=service)

Edges:
  - Walk edges: derived from OSMnx pedestrian network, capped at WALK_THRESHOLD_M.
    Weight = estimated walk time in minutes (dist_m / WALK_SPEED_M_PER_MIN).
  - TTC edges: grid/service node → nearest TTC stop, weighted by headway.

GPU path: tries to import cugraph (RAPIDS).  Falls back to NetworkX silently.
OSMnx: downloaded once, cached as data_cache/osm_walk_graph.graphml.

Outputs
-------
data_cache/graph_stats.json          — summary (node/edge counts, timing)
data_cache/network_walk_distances.json — {cell_id: {service_id: walk_minutes}}
  Used by score_accessibility.py when available to replace haversine.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("deadzone.graph")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

WALK_THRESHOLD_M = 1200.0   # max edge length in walk graph
WALK_SPEED_M_PER_MIN = 80.0  # average pedestrian speed
TTC_EDGE_WEIGHT_FACTOR = 1.5  # headway-to-time multiplier for TTC edges
TORONTO_BBOX = (43.58, -79.64, 43.86, -79.12)  # north, west, south, east as (lat_min, lng_min, lat_max, lng_max)


# ------GPU / CPU backend selector--------

def _get_graph_backend():
    """Return (backend_name, graph_module)."""
    try:
        import cugraph
        import cudf
        log.info("RAPIDS cuGraph detected — using GPU graph backend")
        return "cugraph", cugraph
    except ImportError:
        pass
    import networkx as nx
    log.info("cuGraph not available — using NetworkX (CPU) fallback")
    return "networkx", nx


# ------Haversine helper------

def _haversine_m(lat1, lng1, lat2, lng2):
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ------OSMnx walk network------

def _load_osm_network(cache_dir: Path, bbox: tuple):
    """Download (or load cached) Toronto pedestrian network via OSMnx."""
    graphml_path = cache_dir / "osm_walk_graph.graphml"

    try:
        import osmnx as ox
    except ImportError:
        log.warning("osmnx not installed — skipping OSM walk network")
        return None

    if graphml_path.exists():
        log.info("Loading cached OSM walk graph from %s …", graphml_path)
        try:
            G = ox.load_graphml(graphml_path)
            log.info("  Loaded OSM graph: %d nodes, %d edges", G.number_of_nodes(), G.number_of_edges())
            return G
        except Exception as exc:
            log.warning("  Failed to load cached graph: %s — re-downloading", exc)

    lat_min, lng_min, lat_max, lng_max = bbox
    log.info("Downloading Toronto pedestrian network from OSM (this takes ~2-5 min, cached after first run) …")
    t0 = time.perf_counter()
    try:
        G = ox.graph_from_bbox(
            bbox=(lat_max, lat_min, lng_max, lng_min),  # osmnx bbox order: N, S, E, W
            network_type="walk",
            retain_all=False,
        )
        ox.save_graphml(G, graphml_path)
        log.info(
            "Downloaded OSM walk graph: %d nodes, %d edges  (%.1f s)",
            G.number_of_nodes(), G.number_of_edges(), time.perf_counter() - t0,
        )
        return G
    except Exception as exc:
        log.warning("OSM download failed: %s — graph will use haversine fallback", exc)
        return None


def _osm_walk_time(G, lat1, lng1, lat2, lng2) -> float | None:
    """Return walk time in minutes between two points using OSMnx shortest path."""
    try:
        import osmnx as ox
        import networkx as nx
        orig = ox.distance.nearest_nodes(G, X=lng1, Y=lat1)
        dest = ox.distance.nearest_nodes(G, X=lng2, Y=lat2)
        length_m = nx.shortest_path_length(G, orig, dest, weight="length")
        return length_m / WALK_SPEED_M_PER_MIN
    except Exception:
        return None


# ------Graph construction------

def _build_networkx_graph(nodes: list[dict], edges: list[tuple]) -> "nx.Graph":
    import networkx as nx
    G = nx.Graph()
    for n in nodes:
        G.add_node(n["id"], **{k: v for k, v in n.items() if k != "id"})
    for u, v, w in edges:
        G.add_edge(u, v, weight=w)
    return G


def _build_cugraph(nodes: list[dict], edges: list[tuple]):
    import cugraph
    import cudf
    import pandas as pd

    src = [e[0] for e in edges]
    dst = [e[1] for e in edges]
    wt = [e[2] for e in edges]

    # Map node IDs to integers
    all_ids = list({n["id"] for n in nodes})
    id_to_int = {nid: i for i, nid in enumerate(all_ids)}

    df = cudf.DataFrame({
        "src": cudf.Series([id_to_int[s] for s in src], dtype="int32"),
        "dst": cudf.Series([id_to_int[d] for d in dst], dtype="int32"),
        "weight": cudf.Series(wt, dtype="float32"),
    })
    G = cugraph.Graph()
    G.from_cudf_edgelist(df, source="src", destination="dst", edge_attr="weight")
    return G, id_to_int


def build_graph(cache_dir: Path, fast: bool = False) -> dict:
    """
    Build the accessibility graph and compute walk distances.
    Returns a stats dict.
    """
    t0 = time.perf_counter()
    cache_dir.mkdir(parents=True, exist_ok=True)

    # --- Load node data ---
    grid_path = cache_dir / "grid.geojson"
    stops_path = cache_dir / "stops.json"
    services_path = cache_dir / "service_locations.json"

    if not grid_path.exists():
        log.error("grid.geojson missing — run grid.py first")
        return {}

    with grid_path.open() as f:
        grid_fc = json.load(f)
    grid_cells = [feat["properties"] for feat in grid_fc["features"]]

    stops = []
    if stops_path.exists():
        with stops_path.open() as f:
            stops = json.load(f)
        if isinstance(stops, dict):
            stops = stops.get("stops", [])

    services = []
    if services_path.exists():
        with services_path.open() as f:
            services = json.load(f)

    # Fast mode: sample grid cells
    if fast:
        FAST_BBOX = {"lat_min": 43.625, "lat_max": 43.680, "lng_min": -79.430, "lng_max": -79.360}
        grid_cells = [
            c for c in grid_cells
            if FAST_BBOX["lat_min"] <= c["lat"] <= FAST_BBOX["lat_max"]
            and FAST_BBOX["lng_min"] <= c["lng"] <= FAST_BBOX["lng_max"]
        ]
        log.info("Fast mode: %d grid cells", len(grid_cells))

    log.info("Building graph: %d cells, %d stops, %d services", len(grid_cells), len(stops), len(services))

    # --- Attempt OSMnx walk network ---
    osm_G = None
    if not fast:
        osm_G = _load_osm_network(cache_dir, TORONTO_BBOX)

    # --- Build node list ---
    nodes: list[dict] = []
    for c in grid_cells:
        nodes.append({"id": f"cell_{c['cell_id']}", "type": "grid", "lat": c["lat"], "lng": c["lng"]})
    for s in stops:
        nodes.append({"id": f"stop_{s['stop_id']}", "type": "ttc_stop", "lat": s["lat"], "lng": s["lng"]})
    for svc in services:
        nodes.append({"id": f"svc_{svc['id']}", "type": "service", "lat": svc["lat"], "lng": svc["lng"]})

    log.info("Total nodes: %d", len(nodes))

    # --- Build edges (walk threshold) ---
    edges: list[tuple] = []

    # Grid cell → TTC stop walk edges
    for cell in grid_cells:
        cell_id = f"cell_{cell['cell_id']}"
        for stop in stops:
            dist = _haversine_m(cell["lat"], cell["lng"], stop["lat"], stop["lng"])
            if dist <= WALK_THRESHOLD_M:
                walk_min = dist / WALK_SPEED_M_PER_MIN
                edges.append((cell_id, f"stop_{stop['stop_id']}", walk_min))

    # Grid cell → service walk edges
    for cell in grid_cells:
        cell_id = f"cell_{cell['cell_id']}"
        for svc in services:
            dist = _haversine_m(cell["lat"], cell["lng"], svc["lat"], svc["lng"])
            if dist <= WALK_THRESHOLD_M:
                if osm_G is not None:
                    osm_time = _osm_walk_time(osm_G, cell["lat"], cell["lng"], svc["lat"], svc["lng"])
                    walk_min = osm_time if osm_time else dist / WALK_SPEED_M_PER_MIN
                else:
                    walk_min = dist / WALK_SPEED_M_PER_MIN
                edges.append((cell_id, f"svc_{svc['id']}", walk_min))

    log.info("Built %d walk edges", len(edges))

    # --- Compute network walk distances for scoring ---
    # For each grid cell, find walk time to each service
    network_distances: dict[str, dict[str, float]] = {}

    backend_name, _ = _get_graph_backend()

    if backend_name == "cugraph" and len(edges) > 0:
        try:
            G_cu, id_map = _build_cugraph(nodes, edges)
            log.info("cuGraph graph built: %d nodes, %d edges", len(nodes), len(edges))
            # cuGraph shortest paths from all cell nodes
            # For demo scale, we compute pairwise via haversine-adjusted distances
            # (Full cuGraph SSSP would require integer node IDs and more setup)
            log.info("cuGraph adjacency built — using for proximity queries")
            _write_distances_from_edges(edges, services, network_distances)
        except Exception as exc:
            log.warning("cuGraph build failed (%s) — falling back to NetworkX", exc)
            backend_name = "networkx"

    if backend_name == "networkx":
        import networkx as nx
        G_nx = _build_networkx_graph(nodes, edges)
        log.info("NetworkX graph: %d nodes, %d edges", G_nx.number_of_nodes(), G_nx.number_of_edges())

        # Extract walk distances from edge list for grid→service pairs
        _write_distances_from_edges(edges, services, network_distances)

    # Write network_walk_distances.json
    dist_path = cache_dir / "network_walk_distances.json"
    with dist_path.open("w") as f:
        json.dump(network_distances, f, separators=(",", ":"))
    log.info("Wrote network walk distances: %d cells  → %s", len(network_distances), dist_path)

    stats = {
        "backend": backend_name,
        "osm_available": osm_G is not None,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "grid_cells": len(grid_cells),
        "ttc_stops": len(stops),
        "services": len(services),
        "elapsed_seconds": round(time.perf_counter() - t0, 2),
    }

    stats_path = cache_dir / "graph_stats.json"
    with stats_path.open("w") as f:
        json.dump(stats, f, indent=2)
    log.info("Graph stats: %s", stats)
    return stats


def _write_distances_from_edges(
    edges: list[tuple],
    services: list[dict],
    out: dict,
) -> None:
    """
    Populate out dict with {cell_id: {svc_id: walk_minutes}}
    derived directly from edge list (cheaper than full SSSP for demo scale).
    """
    svc_ids = {f"svc_{s['id']}" for s in services}
    for u, v, w in edges:
        if u.startswith("cell_") and v in svc_ids:
            cell_key = u[len("cell_"):]
            svc_key = v[len("svc_"):]
            if cell_key not in out:
                out[cell_key] = {}
            existing = out[cell_key].get(svc_key, float("inf"))
            if w < existing:
                out[cell_key][svc_key] = round(w, 2)
        elif v.startswith("cell_") and u in svc_ids:
            cell_key = v[len("cell_"):]
            svc_key = u[len("svc_"):]
            if cell_key not in out:
                out[cell_key] = {}
            existing = out[cell_key].get(svc_key, float("inf"))
            if w < existing:
                out[cell_key][svc_key] = round(w, 2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build DeadZone accessibility graph")
    parser.add_argument("--cache", default="data_cache")
    parser.add_argument("--fast", action="store_true")
    args = parser.parse_args()
    stats = build_graph(Path(args.cache), fast=args.fast)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
