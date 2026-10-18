"""Minimal A2A (Agent-to-Agent protocol) client over JSON-RPC 2.0.

Flow:  message/send  ->  task (state "submitted")  ->  poll tasks/get until
       "completed" | "failed" | "canceled"  ->  read the result from the
       task artifacts (or the last agent message in the history).

Parsing is tolerant to both the A2A 0.x shape ({"kind": "text"} parts, task as
result) and the A2A 1.0 shape (plain {"text"} parts, {"task": {...}} result).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import httpx

from .settings import A2A_TIMEOUT_SECONDS

log = logging.getLogger("a2a-client")
TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}


class A2AError(RuntimeError):
    pass


def _rpc(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}


def _parts_text(parts: list[dict] | None) -> str:
    texts = []
    for p in parts or []:
        if "text" in p:
            texts.append(str(p["text"]))
        elif "data" in p:  # structured agent output (fasta2a wraps it as {"result": ...})
            data = p["data"]
            if isinstance(data, dict) and set(data) == {"result"}:
                data = data["result"]
            texts.append(json.dumps(data, ensure_ascii=False))
    return "\n".join(texts).strip()


def extract_result_text(task: dict) -> str:
    for artifact in reversed(task.get("artifacts") or []):
        text = _parts_text(artifact.get("parts"))
        if text:
            return text
    for msg in reversed(task.get("history") or []):
        if msg.get("role") in ("agent", "ROLE_AGENT"):
            text = _parts_text(msg.get("parts"))
            if text:
                return text
    status_msg = (task.get("status") or {}).get("message")
    return _parts_text(status_msg.get("parts")) if status_msg else ""


async def fetch_agent_card(base_url: str, client: httpx.AsyncClient | None = None) -> dict:
    own = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        resp = await client.get(base_url.rstrip("/") + "/.well-known/agent-card.json")
        resp.raise_for_status()
        return resp.json()
    finally:
        if own:
            await client.aclose()


async def send_message(agent_url: str, text: str, timeout: float = A2A_TIMEOUT_SECONDS,
                       poll_interval: float = 0.5) -> dict[str, Any]:
    """Send a text message to an A2A agent and wait for the task to finish.

    Returns {"state", "text", "task_id", "duration_s"}.
    """
    started = time.monotonic()
    message = {
        "role": "user",
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    async with httpx.AsyncClient(base_url=agent_url.rstrip("/"), timeout=httpx.Timeout(60, connect=10)) as client:
        resp = await client.post("/", json=_rpc("message/send", {"message": message}))
        if resp.status_code == 422 or (resp.status_code < 300 and "error" in resp.json()):
            # strict A2A 1.0 servers reject the legacy "kind" fields -> retry without them
            message.pop("kind")
            message["parts"] = [{"text": text}]
            resp = await client.post("/", json=_rpc("message/send", {"message": message}))
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise A2AError(f"message/send failed: {body['error']}")
        result = body["result"]
        task = result.get("task", result)
        if task.get("kind") == "message" or "status" not in task:  # agent answered directly with a message
            return {"state": "completed", "text": _parts_text(task.get("parts")), "task_id": None,
                    "duration_s": round(time.monotonic() - started, 2)}
        task_id = task["id"]

        while task["status"]["state"] not in TERMINAL_STATES:
            if time.monotonic() - started > timeout:
                raise A2AError(f"task {task_id} timed out after {timeout:.0f}s (state={task['status']['state']})")
            await asyncio.sleep(poll_interval)
            r = await client.post("/", json=_rpc("tasks/get", {"id": task_id}))
            r.raise_for_status()
            rb = r.json()
            if "error" in rb:
                raise A2AError(f"tasks/get failed: {rb['error']}")
            task = rb["result"].get("task", rb["result"])

    state = task["status"]["state"]
    return {"state": state, "text": extract_result_text(task), "task_id": task_id,
            "duration_s": round(time.monotonic() - started, 2)}
