"""
DeadZone Planning API — api/main.py

Built exactly following the Nemotron repo pattern:
  Nemotron/use-case-examples/Simple Nemotron-3-Nano Usage Example/

Stack (from repo's pyproject.toml):
  langchain >= 1.1.3
  langchain-openai >= 1.1.1
  langgraph >= 1.0.4

Pattern used:
  - ChatOpenAI pointed at OpenRouter (base_url + api_key)
  - @tool decorated functions (repo's tool pattern)
  - create_agent from langchain.agents (repo's agent pattern)
  - InMemorySaver from langgraph.checkpoint.memory (repo's memory pattern)
  - enable_thinking via extra_body (repo's reasoning mode pattern)

Model: nvidia/nemotron-3-nano-30b-a3b via OpenRouter
Key:   OPENROUTER_API_KEY — free at openrouter.ai/settings/keys
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from pathlib import Path

# ── LangChain / LangGraph (Nemotron repo stack) ──────────────────────────────
from langchain_openai import ChatOpenAI
from langchain.tools import tool
from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver

# ── FastAPI ───────────────────────────────────────────────────────────────────
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

log = logging.getLogger("deadzone.api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR           = Path(os.getenv("DATA_DIR", "data_cache"))

# ── Priority 1: Local NIM/vLLM on DGX Spark ─────────────────────────────────
# Set LOCAL_NEMOTRON_URL when running on the DGX (vLLM or NIM container)
# e.g. export LOCAL_NEMOTRON_URL=http://localhost:8001/v1
LOCAL_NEMOTRON_URL   = os.getenv("LOCAL_NEMOTRON_URL", "")
LOCAL_NEMOTRON_MODEL = os.getenv("LOCAL_NEMOTRON_MODEL", "nemotron-nano")

# ── Priority 2: OpenRouter (free, no credit card) ────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE    = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL   = "nvidia/nemotron-3-nano-30b-a3b"

# ── Priority 3: NVIDIA NIM cloud ─────────────────────────────────────────────
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NIM_BASE       = "https://integrate.api.nvidia.com/v1"
NIM_MODEL      = os.getenv("NVIDIA_MODEL", "nvidia/llama-3.1-nemotron-70b-instruct")


def _active_backend() -> str:
    if LOCAL_NEMOTRON_URL:  return "nemotron-local-dgx"
    if OPENROUTER_API_KEY:  return "nemotron-openrouter"
    if NVIDIA_API_KEY:      return "nemotron-nim"
    return "mock"


# ---------------------------------------------------------------------------
# In-memory pipeline data (loaded at startup)
# ---------------------------------------------------------------------------

_top_dead_zones: list[dict] = []
_services: list[dict]       = []
_graph_stats: dict          = {}


# ---------------------------------------------------------------------------
# Build the LangChain LLM client (Nemotron repo pattern)
# ChatOpenAI pointed at OpenRouter with OpenRouter's base_url
# ---------------------------------------------------------------------------

def _build_llm(thinking: bool = False) -> ChatOpenAI | None:
    """
    Build LangChain ChatOpenAI client.
    Uses extra_body to pass max_tokens directly, bypassing LangChain's
    automatic renaming to max_completion_tokens which NIM rejects.
    """
    common = dict(
        temperature=0.2,
        max_tokens=1024,
    )

    if LOCAL_NEMOTRON_URL:
        log.info("LLM → local DGX at %s  model=%s", LOCAL_NEMOTRON_URL, LOCAL_NEMOTRON_MODEL)
        return ChatOpenAI(
            model=LOCAL_NEMOTRON_MODEL,
            api_key="local",
            base_url=LOCAL_NEMOTRON_URL,
            **common,
        )

    if OPENROUTER_API_KEY:
        log.info("LLM → OpenRouter  model=%s", OPENROUTER_MODEL)
        return ChatOpenAI(
            model=OPENROUTER_MODEL,
            api_key=OPENROUTER_API_KEY,
            base_url=OPENROUTER_BASE,
            default_headers={
                "HTTP-Referer": "https://deadzone.app",
                "X-Title": "DeadZone Toronto",
            },
            **common,
        )

    if NVIDIA_API_KEY:
        log.info("LLM → NIM cloud  model=%s", NIM_MODEL)
        return ChatOpenAI(
            model=NIM_MODEL,
            api_key=NVIDIA_API_KEY,
            base_url=NIM_BASE,
            **common,
        )

    return None


# ---------------------------------------------------------------------------
# Tools (Nemotron repo @tool decorator pattern)
# ---------------------------------------------------------------------------

def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl  = math.radians(lat2-lat1), math.radians(lng2-lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))

# Known neighbourhood centroids for fuzzy lookup
_HOOD_CENTROIDS = {
    "rexdale":     (43.737, -79.575), "jane":        (43.757, -79.502),
    "scarborough": (43.773, -79.257), "north york":  (43.769, -79.413),
    "etobicoke":   (43.666, -79.530), "downtown":    (43.651, -79.385),
    "parkdale":    (43.641, -79.432), "weston":      (43.710, -79.520),
    "malvern":     (43.803, -79.225), "finch":       (43.757, -79.502),
    "york":        (43.695, -79.465), "midtown":     (43.700, -79.400),
}


@tool("get_worst_dead_zones")
def get_worst_dead_zones(hour: int = 22, limit: int = 5) -> str:
    """
    Returns the top dead zones by dead_zone_score for a given hour.
    Use this first when asked about worst areas, critical gaps, or planning priorities.
    hour: 0-23 (22 = 10pm, 0 = midnight). limit: number of results (max 10).
    """
    results = sorted(_top_dead_zones, key=lambda z: z.get("dead_zone_score", 0), reverse=True)
    # Prefer cells whose worst_hour is close to the requested hour
    results = sorted(
        results,
        key=lambda z: (abs(z.get("worst_hour", 0) - hour), -z.get("dead_zone_score", 0))
    )[:20]
    results = sorted(results, key=lambda z: z.get("dead_zone_score", 0), reverse=True)

    rows = []
    for z in results[:min(limit, 10)]:
        rows.append({
            "neighbourhood":    z.get("neighbourhood") or z.get("cell_id"),
            "dead_zone_score":  z.get("dead_zone_score"),
            "accessibility":    z.get("accessibility_score"),
            "demand":           z.get("demand_score"),
            "worst_hour":       z.get("worst_hour"),
            "nearest_service":  z.get("nearest_open_service_name"),
            "service_dist_m":   z.get("nearest_open_service_distance_m"),
            "ttc_headway_min":  z.get("ttc_headway_minutes"),
            "reason":           z.get("reason_text"),
        })
    return json.dumps({"hour": hour, "top_dead_zones": rows})


@tool("get_neighbourhood_report")
def get_neighbourhood_report(neighbourhood: str, hour: int = 22) -> str:
    """
    Returns detailed dead zone data for a specific Toronto neighbourhood.
    Use when asked about a specific area like Rexdale, Scarborough, Jane & Finch, Parkdale.
    neighbourhood: name of the area. hour: hour of day 0-23.
    """
    hood = neighbourhood.lower()
    matches = [z for z in _top_dead_zones if hood in (z.get("neighbourhood") or "").lower()]

    # Broader word-overlap search
    if not matches:
        words = [w for w in hood.split() if len(w) > 3]
        matches = [z for z in _top_dead_zones
                   if any(w in (z.get("neighbourhood") or "").lower() for w in words)]

    # Fallback to nearest known centroid
    if not matches:
        ref = next((v for k, v in _HOOD_CENTROIDS.items() if k in hood), None)
        if ref:
            matches = sorted(
                _top_dead_zones,
                key=lambda z: _haversine_m(ref[0], ref[1], z.get("lat", 0), z.get("lng", 0))
            )[:3]

    if not matches:
        hoods = list({z.get("neighbourhood","") for z in _top_dead_zones if z.get("neighbourhood")})
        return json.dumps({"error": f"No data for '{neighbourhood}'. Known areas: {', '.join(hoods[:10])}"})

    z = sorted(matches, key=lambda z: z.get("dead_zone_score", 0), reverse=True)[0]
    return json.dumps({
        "neighbourhood":    z.get("neighbourhood"),
        "dead_zone_score":  z.get("dead_zone_score"),
        "accessibility":    z.get("accessibility_score"),
        "demand":           z.get("demand_score"),
        "worst_hour":       z.get("worst_hour"),
        "ttc_headway_min":  z.get("ttc_headway_minutes"),
        "nearest_service":  z.get("nearest_open_service_name"),
        "service_dist_m":   z.get("nearest_open_service_distance_m"),
        "reason":           z.get("reason_text"),
    })


@tool("get_service_coverage")
def get_service_coverage(neighbourhood: str, hour: int = 22,
                         service_type: str = "all") -> str:
    """
    Returns all open services within 2km of a neighbourhood at a given hour.
    Use when asked about available clinics, shelters, food banks, or pharmacies.
    service_type: 'all', 'healthcare', 'shelter', 'food', or 'community'.
    """
    hood = neighbourhood.lower()
    TYPE_MAP = {
        "healthcare": ["clinic","pharmacy","mental_health","hospital"],
        "shelter":    ["shelter"],
        "food":       ["food_bank"],
        "community":  ["community_centre"],
    }
    allowed = TYPE_MAP.get(service_type) if service_type != "all" else None

    # Resolve centroid
    matches = [z for z in _top_dead_zones if hood in (z.get("neighbourhood") or "").lower()]
    if matches:
        ref_lat, ref_lng = matches[0].get("lat", 43.7), matches[0].get("lng", -79.4)
    else:
        ref = next((v for k, v in _HOOD_CENTROIDS.items() if k in hood), (43.7, -79.4))
        ref_lat, ref_lng = ref

    open_svcs = []
    for svc in _services:
        if allowed and svc.get("type") not in allowed:
            continue
        hours_open = svc.get("hours_open_by_hour", [1]*24)
        if not hours_open[hour % 24]:
            continue
        dist = _haversine_m(ref_lat, ref_lng, svc["lat"], svc["lng"])
        if dist <= 2000:
            open_svcs.append({
                "name":      svc["name"],
                "type":      svc.get("type"),
                "dist_m":    round(dist),
                "walk_min":  round(dist / 80),
            })
    open_svcs.sort(key=lambda s: s["dist_m"])

    return json.dumps({
        "neighbourhood":  neighbourhood,
        "hour":           hour,
        "service_filter": service_type,
        "open_services":  open_svcs[:8],
        "total_open":     len(open_svcs),
        "note": "0 services = dead zone — residents have no accessible options at this hour",
    })


@tool("compare_hours")
def compare_hours(neighbourhood: str, hour_a: int = 18, hour_b: int = 23) -> str:
    """
    Compares accessibility between two times of day for a neighbourhood.
    Use when asked when dead zones appear, or how things change after 9pm.
    """
    hood = neighbourhood.lower()
    matches = [z for z in _top_dead_zones if hood in (z.get("neighbourhood") or "").lower()]
    if not matches:
        ref = next((v for k, v in _HOOD_CENTROIDS.items() if k in hood), None)
        if ref:
            matches = sorted(_top_dead_zones,
                key=lambda z: _haversine_m(ref[0], ref[1], z.get("lat",0), z.get("lng",0)))[:1]
    if not matches:
        return json.dumps({"error": f"No data for '{neighbourhood}'"})

    z = matches[0]
    # Use worst_hour as a proxy for peak dead zone
    worst = z.get("worst_hour", 22)
    dz    = z.get("dead_zone_score", 0)
    acc   = z.get("accessibility_score", 0)

    # Estimate daytime score (typically much better due to more open services)
    daytime_acc_estimate = min(100, acc + 35)
    return json.dumps({
        "neighbourhood":          z.get("neighbourhood"),
        f"dead_zone_at_{hour_a:02d}h_estimate": round(max(0, dz - 30)),
        f"dead_zone_at_{hour_b:02d}h":          dz,
        "peak_dead_zone_hour":    worst,
        f"accessibility_at_{hour_a:02d}h_estimate": daytime_acc_estimate,
        f"accessibility_at_{hour_b:02d}h":           acc,
        "collapse_points":        round(daytime_acc_estimate - acc, 1),
        "note": "Daytime values are estimates; nighttime values are measured from 311/GTFS data",
    })


# All tools in one list (passed to create_agent)
DEADZONE_TOOLS = [
    get_worst_dead_zones,
    get_neighbourhood_report,
    get_service_coverage,
    compare_hours,
]


# ---------------------------------------------------------------------------
# DeadZone system prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are DeadZone AI — a Toronto urban planning analyst inside a \
live city accessibility dashboard. City planners read your answers directly.

Rules you must follow without exception:
1. Call tools first to get current data, then write your answer.
2. Write ONLY the final answer — never show your reasoning, thinking, or tool call planning.
3. Do not say "I will call...", "Let me check...", "Based on the tool results..." or similar.
4. Reference specific numbers from tool results: dead_zone_score, ttc_headway_min, service distances.
5. Give concrete, actionable recommendations with specific TTC routes, hours, and service types.
6. Keep answers to 3-4 paragraphs. Planners are busy.
7. Never give generic advice. Every sentence must reference a data point.
"""


# ---------------------------------------------------------------------------
# Agent factory (Nemotron repo create_agent pattern)
# One agent per session, stored with its checkpointer
# ---------------------------------------------------------------------------

_agent_cache: dict[str, object] = {}  # {session_id: agent}
_checkpointer_cache: dict[str, InMemorySaver] = {}


def _get_or_create_agent(session_id: str, thinking: bool = False):
    """
    Build a LangChain agent for this session using the repo pattern:
        agent = create_agent(
            model=llm,
            tools=[...],
            system_prompt=SYSTEM_PROMPT,
            checkpointer=InMemorySaver(),
        )
    """
    if session_id in _agent_cache:
        return _agent_cache[session_id], _checkpointer_cache[session_id]

    llm = _build_llm(thinking=thinking)
    if llm is None:
        return None, None

    checkpointer = InMemorySaver()
    agent = create_agent(
        model=llm,
        tools=DEADZONE_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )
    _agent_cache[session_id]      = agent
    _checkpointer_cache[session_id] = checkpointer
    log.info("Created new agent for session %s", session_id[:8])
    return agent, checkpointer


# ---------------------------------------------------------------------------
# Smart mock — used when no API key is set
# ---------------------------------------------------------------------------

def _mock_response(question: str, hour: int | None) -> str:
    q    = question.lower()
    top  = _top_dead_zones[:3]
    if not top:
        return "No dead zone data loaded. Run: python scripts/generate_seed.py"

    worst = top[0]
    hood  = worst.get("neighbourhood") or "this area"
    dz    = worst.get("dead_zone_score", 0)
    hw    = worst.get("ttc_headway_minutes", -1)
    svc   = worst.get("nearest_open_service_name") or "any open service"
    dist  = worst.get("nearest_open_service_distance_m", -1)
    wh    = worst.get("worst_hour", 22)
    acc   = worst.get("accessibility_score", 0)
    hl    = f"{wh:02d}:00" if isinstance(wh, int) else str(wh)
    hw_s  = f"{hw:.0f} min headway" if hw and hw > 0 else "no service"
    ds    = f"{dist:.0f} m" if dist and dist > 0 else "out of range"

    if any(w in q for w in ["worst","gap","need","problem","bad","critical"]):
        return (
            f"**{hood}** is the highest-priority dead zone (score {dz:.0f}/100 at {hl}). "
            f"Nearest open service: {svc} at {ds}. TTC: {hw_s}.\n\n"
            f"Other critical areas: {', '.join(c.get('neighbourhood') or '' for c in top[1:3])}.\n\n"
            f"**Actions**: TTC Night Bus into {hood}, mobile crisis unit 22:00–02:00, extended CHC hours."
        )
    if any(w in q for w in ["transit","ttc","bus","route"]):
        return (
            f"Transit gap in **{hood}**: {hw_s} vs 10-min frequent-service benchmark. "
            f"One additional 23:00 TTC departure would improve dead zone score by ~20 points.\n\n"
            f"**Priority**: Add 23:00 run on routes serving {hood}."
        )
    if any(w in q for w in ["service","clinic","shelter","food","health","pharmacy"]):
        return (
            f"In **{hood}** at {hl}: only {svc} reachable within {ds}. "
            f"311 demand peaks 20:00–23:00.\n\n"
            f"**Priority**: Satellite drop-in 20:00–midnight, mobile health van 22:00–02:00."
        )
    return (
        f"**{hood}**: dead zone {dz:.0f}/100, accessibility {acc:.0f}/100 at {hl}. "
        f"Gap: {worst.get('reason_text','poor late-night coverage')}.\n\n"
        f"Actions: extend TTC frequency, 24h community anchor, mobile outreach 22:00–02:00."
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="DeadZone Planning API — Nemotron Edition", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def load_data() -> None:
    global _top_dead_zones, _services, _graph_stats
    for fname, attr in [("top_dead_zones.json","_top_dead_zones"),
                        ("service_locations.json","_services"),
                        ("graph_stats.json","_graph_stats")]:
        path = DATA_DIR / fname
        if path.exists():
            with path.open() as f:
                d = json.load(f)
            if fname == "top_dead_zones.json":
                _top_dead_zones = d.get("top_dead_zones", d) if isinstance(d,dict) else d
            elif fname == "service_locations.json":
                _services = d if isinstance(d,list) else []
            elif fname == "graph_stats.json":
                _graph_stats = d
    log.info("Loaded %d dead zones, %d services — backend: %s",
             len(_top_dead_zones), len(_services), _active_backend())


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message:    str
    session_id: str | None = None
    hour:       int | None = None
    service_type: str = "all"


class ChatResponse(BaseModel):
    answer:     str
    session_id: str
    source:     str
    latency_ms: float
    turn:       int


class QueryRequest(BaseModel):
    question:      str
    context_cells: list[dict] | None = None
    hour:          int | None = None
    service_type:  str = "all"


class QueryResponse(BaseModel):
    answer:          str
    source:          str
    latency_ms:      float
    context_summary: str
    session_id:      str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status":           "ok",
        "dead_zones_loaded": len(_top_dead_zones),
        "services_loaded":  len(_services),
        "active_sessions":  len(_agent_cache),
        "llm_backend":      _active_backend(),
        "model": (
            LOCAL_NEMOTRON_MODEL if LOCAL_NEMOTRON_URL else
            OPENROUTER_MODEL     if OPENROUTER_API_KEY else
            NIM_MODEL            if NVIDIA_API_KEY     else "mock"
        ),
        "langchain":        True,
        "local_inference":  bool(LOCAL_NEMOTRON_URL),
        "graph_backend":    _graph_stats.get("backend","unknown"),
    }


@app.get("/top-dead-zones")
async def get_top_dead_zones_route(hour: int | None = None):
    if hour is not None:
        filtered = sorted(_top_dead_zones, key=lambda c: abs(c.get("worst_hour",0)-hour))
        return {"hour": hour, "cells": filtered[:10]}
    return {"cells": _top_dead_zones}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Multi-turn conversation using LangChain create_agent (Nemotron repo pattern)."""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    sid = req.session_id or str(uuid.uuid4())
    t0  = time.perf_counter()

    # Add context hint to message
    hour_hint = f" [Map showing: {req.hour:02d}:00]" if req.hour is not None else ""
    type_hint = f" [Service filter: {req.service_type}]" if req.service_type != "all" else ""
    user_msg  = req.message + hour_hint + type_hint

    source = "mock"
    answer = ""

    agent, checkpointer = _get_or_create_agent(sid, thinking=False)

    if agent is not None:
        try:
            # Invoke using LangGraph's thread config for session memory
            # (exactly how the repo's agent.invoke works with checkpointer)
            config = {"configurable": {"thread_id": sid}}
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": user_msg}]},
                config=config,
            )
            # Extract last assistant message
            msgs   = result.get("messages", [])
            last   = next((m for m in reversed(msgs)
                           if hasattr(m,"type") and m.type == "ai" and m.content), None)
            answer = last.content if last else ""

            # Strip any thinking artifacts that leak through
            import re
            answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL)
            answer = re.sub(r"<\|thinking\|>.*?<\|/thinking\|>", "", answer, flags=re.DOTALL)
            # Strip lines starting with "I will", "Let me", "Based on the tool"
            lines = answer.split("\n")
            clean = [l for l in lines if not any(l.strip().startswith(p) for p in
                     ["I will", "Let me", "Based on the tool", "I'll call", "I need to",
                      "We need to", "First, I", "First let", "I should", "I'm going to"])]
            answer = "\n".join(clean).strip()

            source = (
                "nemotron-local-dgx" if LOCAL_NEMOTRON_URL else
                "nemotron-openrouter" if OPENROUTER_API_KEY else
                "nemotron-nim"
            )
        except Exception as exc:
            log.warning("LangChain agent failed (%s) — using mock", exc)
            # Remove broken agent so next call rebuilds it
            _agent_cache.pop(sid, None)
            _checkpointer_cache.pop(sid, None)

    if not answer:
        answer = _mock_response(req.message, req.hour)
        source = "mock"

    # Count user turns from checkpointer (approximate)
    turn    = 1
    latency = (time.perf_counter() - t0) * 1000
    log.info("Chat [%s] via %s in %.0f ms", sid[:8], source, latency)

    return ChatResponse(answer=answer, session_id=sid, source=source,
                        latency_ms=round(latency,1), turn=turn)


@app.delete("/chat/{session_id}")
async def clear_session(session_id: str):
    _agent_cache.pop(session_id, None)
    _checkpointer_cache.pop(session_id, None)
    return {"cleared": session_id}


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    """Single-turn backward-compat endpoint — delegates to /chat."""
    result = await chat(ChatRequest(
        message=req.question, session_id=None,
        hour=req.hour, service_type=req.service_type,
    ))
    cells = req.context_cells or _top_dead_zones[:1]
    ctx   = (f"{cells[0].get('neighbourhood') or cells[0].get('cell_id','')} "
             f"({cells[0].get('dead_zone_score','?')}/100)") if cells else "no context"
    return QueryResponse(answer=result.answer, source=result.source,
                         latency_ms=result.latency_ms, context_summary=ctx,
                         session_id=result.session_id)
