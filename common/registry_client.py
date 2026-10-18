"""Client side of the agent registry.

* attach_registration(app, ...) - makes an A2A server register itself with the
  registry on startup, send heartbeats, re-register if the registry restarted and
  deregister on shutdown.
* search_agents / get_agent     - dynamic lookup used by the orchestrator and client.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from .settings import HEARTBEAT_SECONDS, REGISTRY_API_KEY, REGISTRY_URL

log = logging.getLogger("registry-client")
HEADERS = {"X-Registry-Key": REGISTRY_API_KEY}


async def _register(client: httpx.AsyncClient, public_url: str) -> str:
    resp = await client.post(f"{REGISTRY_URL}/agents/register", json={"url": public_url}, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()["name"]


async def _registration_loop(public_url: str) -> None:
    name: str | None = None
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                if name is None:
                    await asyncio.sleep(0.5)  # let uvicorn start serving the agent card first
                    name = await _register(client, public_url)
                    log.info("[REGISTRY] registered as '%s' (%s) at %s", name, public_url, REGISTRY_URL)
                else:
                    r = await client.post(f"{REGISTRY_URL}/agents/{name}/heartbeat", headers=HEADERS)
                    if r.status_code == 404:  # registry restarted -> register again
                        log.warning("[REGISTRY] unknown to registry, re-registering")
                        name = None
                        continue
                    r.raise_for_status()
                await asyncio.sleep(HEARTBEAT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # registry not up yet / network issue -> retry
                log.warning("[REGISTRY] not reachable (%s) - retrying in 3s", exc.__class__.__name__)
                name = None
                await asyncio.sleep(3)


def attach_registration(app: Any, public_url: str, agent_name: str) -> None:
    """Wrap the Starlette lifespan of an A2A app with self-registration."""
    original = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with original(a):
            task = asyncio.create_task(_registration_loop(public_url))
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(Exception):
                    async with httpx.AsyncClient(timeout=3) as c:
                        await c.delete(f"{REGISTRY_URL}/agents/{agent_name}", headers=HEADERS)

    app.router.lifespan_context = lifespan


async def search_agents(query: str | None = None, limit: int = 3, skill: str | None = None) -> list[dict]:
    """Keyword search (query) or exact skill-id lookup (skill). Results carry 'match': skill|keyword|fallback."""
    params = {"limit": limit, **({"skill": skill} if skill else {"q": query})}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{REGISTRY_URL}/agents/search", params=params)
        r.raise_for_status()
        body = r.json()
        return [{**a, "match": body.get("match")} for a in body["results"]]


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{REGISTRY_URL}/agents")
        r.raise_for_status()
        return r.json()["agents"]


async def get_agent(name: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{REGISTRY_URL}/agents/{name}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
