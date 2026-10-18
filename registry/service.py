"""Agent Registry - the "missing link" (lecture part 2).

A small directory service in which A2A agents publish themselves. Agents only
send their base URL; the registry fetches and validates the official A2A agent
card from <url>/.well-known/agent-card.json, so the registry content always
matches what the agent really exposes.

Endpoints
  POST   /agents/register          {"url": "..."}   (header X-Registry-Key)
  POST   /agents/{name}/heartbeat                    (header X-Registry-Key)
  DELETE /agents/{name}                              (header X-Registry-Key)
  GET    /agents                   all live agents (?include_stale=true)
  GET    /agents/search?skill=...  exact skill-id match (used by the orchestrator)
  GET    /agents/search?q=...      keyword search over name, description, skills (fallback: best candidates)
  GET    /agents/{name}            one agent incl. full agent card
  GET    /health

Run:  python -m registry.service        (port 8000)
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, HttpUrl

from common.logging_setup import setup_logging
from common.settings import DEFAULT_PORTS, REGISTRY_API_KEY, REGISTRY_TTL_SECONDS

log = setup_logging("registry")
app = FastAPI(title="A2A Agent Registry", version="1.0.0")

_agents: dict[str, dict[str, Any]] = {}  # name -> entry (in-memory, agents re-register via heartbeat)


class RegisterRequest(BaseModel):
    url: HttpUrl


def require_key(x_registry_key: str = Header(default="")) -> None:
    if not secrets.compare_digest(x_registry_key, REGISTRY_API_KEY):
        raise HTTPException(status_code=401, detail="invalid registry key")


def _is_alive(entry: dict) -> bool:
    return time.time() - entry["last_heartbeat"] <= REGISTRY_TTL_SECONDS


def _public(entry: dict, full: bool = False) -> dict:
    card = entry["card"]
    out = {
        "name": entry["name"],
        "url": entry["url"],
        "description": card.get("description", ""),
        "version": card.get("version"),
        "skills": [
            {k: s.get(k) for k in ("id", "name", "description", "tags", "examples") if s.get(k) is not None}
            for s in card.get("skills", [])
        ],
        "status": "alive" if _is_alive(entry) else "stale",
        "last_heartbeat_s_ago": round(time.time() - entry["last_heartbeat"], 1),
    }
    if full:
        out["agent_card"] = card
    return out


_STOP = {"the", "a", "an", "and", "or", "for", "of", "to", "with", "from", "in", "on", "agent", "agents", "that", "can"}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = set()
    for w in words:
        if w in _STOP or len(w) < 2:
            continue
        out.add(w)
        if len(w) > 4 and w.endswith("s"):
            out.add(w[:-1])
        if len(w) > 5 and w.endswith("ing"):
            out.add(w[:-3])
    return out


def _score(entry: dict, query_tokens: set[str]) -> float:
    card = entry["card"]
    weighted = [(entry["name"].replace("-", " "), 3.0), (card.get("description", ""), 1.5)]
    for s in card.get("skills", []):
        weighted += [(s.get("name", ""), 2.0), (" ".join(s.get("tags", [])), 3.0),
                     (s.get("description", ""), 1.0), (" ".join(s.get("examples", []) or []), 0.5)]
    score = 0.0
    for text, weight in weighted:
        score += weight * len(query_tokens & _tokens(text))
    return score


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "agents_alive": sum(_is_alive(e) for e in _agents.values())}


@app.post("/agents/register", dependencies=[Depends(require_key)])
async def register(req: RegisterRequest) -> dict:
    base_url = str(req.url).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{base_url}/.well-known/agent-card.json")
            r.raise_for_status()
            card = r.json()
    except Exception as exc:
        log.warning("registration rejected for %s: agent card not reachable (%s)", base_url, exc)
        raise HTTPException(status_code=400, detail=f"agent card not reachable at {base_url}: {exc}")

    name = card.get("name")
    if not name or not re.fullmatch(r"[a-zA-Z0-9_.-]{2,64}", name):
        raise HTTPException(status_code=400, detail="agent card has no valid name")

    is_new = name not in _agents
    now = time.time()
    _agents[name] = {"name": name, "url": base_url, "card": card,
                     "registered_at": _agents.get(name, {}).get("registered_at", now), "last_heartbeat": now}
    log.info("%s agent '%s' at %s with skills %s", "REGISTERED" if is_new else "re-registered", name, base_url,
             [s.get("id") for s in card.get("skills", [])])
    return {"status": "registered", "name": name}


@app.post("/agents/{name}/heartbeat", dependencies=[Depends(require_key)])
def heartbeat(name: str) -> dict:
    if name not in _agents:
        raise HTTPException(status_code=404, detail="unknown agent - please register")
    _agents[name]["last_heartbeat"] = time.time()
    return {"status": "ok"}


@app.delete("/agents/{name}", dependencies=[Depends(require_key)])
def deregister(name: str) -> dict:
    if _agents.pop(name, None) is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    log.info("DEREGISTERED agent '%s'", name)
    return {"status": "deregistered"}


@app.get("/agents")
def list_all(include_stale: bool = False) -> dict:
    entries = [_public(e) for e in _agents.values() if include_stale or _is_alive(e)]
    return {"count": len(entries), "agents": entries}


@app.get("/agents/search")
def search(q: str | None = Query(None, min_length=2), skill: str | None = Query(None, min_length=2),
           limit: int = Query(3, ge=1, le=20)) -> dict:
    """Find live agents.

    * skill=<id>  exact match on a skill id of the agent card (deterministic, used by the orchestrator)
    * q=<text>    keyword ranking over name, description and skills. If no word matches, the best live
                  candidates are still returned (match="fallback") so the caller can choose by description.
    """
    if not q and not skill:
        raise HTTPException(status_code=422, detail="provide q or skill")
    alive = [e for e in _agents.values() if _is_alive(e)]

    if skill:
        hits = [e for e in alive if any(s.get("id") == skill for s in e["card"].get("skills", []))]
        log.info("SEARCH skill=%r -> %s", skill, [e["name"] for e in hits])
        return {"query": skill, "match": "skill", "results": [{**_public(e), "score": 100.0} for e in hits[:limit]]}

    qt = _tokens(q)
    scored = sorted(((_score(e, qt), e) for e in alive), key=lambda x: -x[0])
    hits = [(s, e) for s, e in scored if s > 0][:limit]
    match = "keyword"
    if not hits:  # tolerant: nothing matched -> offer the candidates anyway
        hits, match = scored[:limit], "fallback"
    log.info("SEARCH q=%r (%s) -> %s", q, match, [(e["name"], s) for s, e in hits])
    return {"query": q, "match": match, "results": [{**_public(e), "score": s} for s, e in hits]}


@app.get("/agents/{name}")
def get_one(name: str) -> dict:
    entry = _agents.get(name)
    if entry is None or not _is_alive(entry):
        raise HTTPException(status_code=404, detail="unknown or stale agent")
    return _public(entry, full=True)


if __name__ == "__main__":
    import os
    port = int(os.getenv("AGENT_PORT", DEFAULT_PORTS["registry"]))
    host = os.getenv("AGENT_BIND_HOST", "127.0.0.1")
    log.info("starting agent registry on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
