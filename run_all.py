"""Start the complete distributed system locally - every component as its OWN process
on its OWN port.

    port 8000  agent registry               (python -m registry.service)
    port 8200  MCP inventory server (HW1)   (python mcp_inventory/server.py --http)
    port 8100  orchestrator-agent           (python -m agents.orchestrator_agent)
    port 8101  order-intake-agent           (python -m agents.intake_agent)
    port 8102  inventory-pricing-agent      (python -m agents.inventory_agent)
    port 8103  production-planning-agent    (python -m agents.production_agent)

Run:  python run_all.py          (Ctrl+C stops everything)
Then in a second terminal:  python client.py --demo 1
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
PY = sys.executable

SERVICES = [
    # name, port, command
    ("registry", 8000, [PY, "-m", "registry.service"]),
    ("mcp-inventory", 8200, [PY, "mcp_inventory/server.py", "--http", "--port", "8200"]),
    ("order-intake-agent", 8101, [PY, "-m", "agents.intake_agent"]),
    ("inventory-pricing-agent", 8102, [PY, "-m", "agents.inventory_agent"]),
    ("production-planning-agent", 8103, [PY, "-m", "agents.production_agent"]),
    ("orchestrator-agent", 8100, [PY, "-m", "agents.orchestrator_agent"]),
]


def _pump(proc: subprocess.Popen, name: str) -> None:
    for line in iter(proc.stdout.readline, ""):
        if line.strip():
            print(line.rstrip(), flush=True)


def preflight() -> None:
    """With LLM_PROVIDER=litellm: check that the proxy is reachable and accepts LITELLM_KEY before starting."""
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env", override=os.getenv("DOTENV_OVERRIDE", "1") != "0")  # .env wins (tests: off)
    if os.getenv("LLM_PROVIDER", "openai").strip().lower() != "litellm":
        return
    base = os.getenv("LITELLM_API_BASE", "http://localhost:4000").rstrip("/")
    key = os.getenv("LITELLM_KEY", "")
    try:
        r = httpx.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=5)
    except httpx.HTTPError:
        sys.exit(f"[run_all] ERROR: no LiteLLM proxy reachable at {base}. Start it first:  python start_litellm.py")
    if r.status_code != 200:
        sys.exit(f"[run_all] ERROR: LiteLLM proxy at {base} rejected LITELLM_KEY (HTTP {r.status_code}: {r.text[:120]}). "
                 "LITELLM_KEY in .env must equal the proxy's master key (LITELLM_MASTER_KEY of the proxy you started).")
    model = os.getenv("LITELLM_DEFAULT_MODEL", "gpt-4o-mini")
    models = [m.get("id") for m in r.json().get("data", [])]
    if model not in models:
        sys.exit(f"[run_all] ERROR: model '{model}' not offered by the proxy (available: {models}).")
    print(f"[run_all] LiteLLM proxy OK at {base} (model {model})", flush=True)


def main() -> None:
    preflight()
    procs: list[tuple[str, subprocess.Popen]] = []
    for name, port, cmd in SERVICES:
        env = {**os.environ, "AGENT_PORT": str(port), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               "PYDANTIC_AI_NO_BANNER": "1"}
        p = subprocess.Popen(cmd, cwd=BASE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=_pump, args=(p, name), daemon=True).start()
        procs.append((name, p))
        print(f"[run_all] started {name:<26} pid={p.pid} port={port}", flush=True)
        if name == "registry":
            time.sleep(1.5)

    # wait until all agents have registered
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            agents = httpx.get("http://127.0.0.1:8000/agents", timeout=2).json()["agents"]
            if len(agents) >= 4:
                print("[run_all] all agents registered: " + ", ".join(a["name"] for a in agents), flush=True)
                print("[run_all] ready -> in a second terminal run:  python client.py --demo 1", flush=True)
                break
        except Exception:
            pass
        if any(p.poll() is not None for _, p in procs):
            break
        time.sleep(1)
    else:
        print("[run_all] WARNING: not all agents registered within 40s - check the output above", flush=True)

    def stop(*_):
        print("\n[run_all] stopping all services ...", flush=True)
        for _, p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    while True:
        for name, p in procs:
            if p.poll() is not None:
                print(f"[run_all] {name} exited with code {p.returncode} - stopping", flush=True)
                stop()
        time.sleep(1)


if __name__ == "__main__":
    main()
