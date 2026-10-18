"""End-to-end test of the distributed system with the offline mock LLM:
starts all 6 processes via run_all.py, then sends demo e-mail 2 through the client -
once with an obedient LLM and once with one that tries to skip steps (workflow guard).

Skipped automatically if port 8000 is already in use (e.g. your system is running).
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.skipif(not _port_free(8000), reason="port 8000 busy - system already running")
@pytest.mark.parametrize("misbehave", ["0", "1"], ids=["obedient-llm", "llm-skips-steps"])
def test_full_order_to_quote_flow(misbehave):
    # misbehave=1: the mock orchestrator LLM skips the intake step and the approval -> the
    # workflow guard must block it and the quote must still be correct
    env = {**os.environ, "LLM_PROVIDER": "mock", "PYTHONIOENCODING": "utf-8", "MOCK_MISBEHAVE": misbehave}
    proc = subprocess.Popen([sys.executable, "run_all.py"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    try:
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline and not ready:
            line = proc.stdout.readline()
            ready = "ready ->" in line
        assert ready, "services did not start"
        out = subprocess.run([sys.executable, "client.py", "--demo", "2"], cwd=ROOT, env=env,
                             capture_output=True, text=True, encoding="utf-8", timeout=120).stdout
        assert "state=completed" in out
        assert "SAF-RLY01" in out and "75,650.00" in out and "99,978.50" in out          # category cap applied by MCP formula engine
        assert "production-planning-agent" in out               # shortfall -> production agent was discovered
        assert "PENDING APPROVAL" in out                         # human-in-the-loop threshold
        assert "tier platinum" in out                            # tier from CRM via intake agent
        assert "production of 360 pcs" in out                     # exact shortfall planned
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
