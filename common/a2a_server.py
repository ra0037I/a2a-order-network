"""Helpers to expose a pydantic-ai agent as an A2A server (like lecture Sample 6)
that registers itself with the agent registry."""
from __future__ import annotations

import logging

import uvicorn
from fasta2a.pydantic_ai import agent_to_a2a

from .llm import model_label
from .registry_client import attach_registration
from .settings import agent_bind_host, agent_port, agent_public_url

log = logging.getLogger("a2a-server")


def _log_failed_tasks() -> None:
    """fasta2a marks a task as 'failed' and re-raises without logging - make the cause visible."""
    from fasta2a.pydantic_ai._bridge import AgentWorker
    if getattr(AgentWorker.run_task, "_logged", False):
        return
    original = AgentWorker.run_task

    async def run_task(self, params):
        try:
            return await original(self, params)
        except Exception as exc:
            log.error("[TASK FAILED] %s: %s", type(exc).__name__, str(exc)[:500])
            raise

    run_task._logged = True
    AgentWorker.run_task = run_task


_log_failed_tasks()


def create_a2a_app(agent, name: str, description: str, skills: list[dict], version: str = "1.0.0"):
    public_url = agent_public_url(name)
    app = agent_to_a2a(
        agent,
        name=name,
        url=public_url,
        version=version,
        description=description,
        skills=skills,
        provider={"organization": "MCI Industrial Computing - HW2", "url": "https://www.mci.edu"},
    )
    attach_registration(app, public_url, name)
    return app


def serve(app, name: str) -> None:
    host, port = agent_bind_host(), agent_port(name)
    log.info("starting %s on %s:%s (public url %s, LLM %s)", name, host, port, agent_public_url(name), model_label())
    uvicorn.run(app, host=host, port=port, log_level="warning")
