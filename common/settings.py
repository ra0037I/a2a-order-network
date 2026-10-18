"""Central configuration (read from .env / environment).

Every service only needs to know the REGISTRY_URL - all other agent locations
are discovered at runtime through the registry.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# All keys always come from .env - it wins over (possibly stale) terminal variables.
# Only the tests switch this off (DOTENV_OVERRIDE=0) so that they can force LLM_PROVIDER=mock.
DOTENV_OVERRIDE = os.getenv("DOTENV_OVERRIDE", "1") != "0"
load_dotenv(BASE_DIR / ".env", override=DOTENV_OVERRIDE)

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://127.0.0.1:8000").rstrip("/")
REGISTRY_API_KEY = os.getenv("REGISTRY_API_KEY", "dev-registry-key")
HEARTBEAT_SECONDS = float(os.getenv("HEARTBEAT_SECONDS", "15"))
REGISTRY_TTL_SECONDS = float(os.getenv("REGISTRY_TTL_SECONDS", "45"))

MCP_INVENTORY_URL = os.getenv("MCP_INVENTORY_URL", "http://127.0.0.1:8200/mcp")

A2A_TIMEOUT_SECONDS = float(os.getenv("A2A_TIMEOUT_SECONDS", "240"))

# Default ports (each agent is its own process)
DEFAULT_PORTS = {
    "registry": 8000,
    "orchestrator-agent": 8100,
    "order-intake-agent": 8101,
    "inventory-pricing-agent": 8102,
    "production-planning-agent": 8103,
    "mcp-inventory": 8200,
}

# Approval policy (human-in-the-loop)
APPROVAL_THRESHOLD_EUR = float(os.getenv("APPROVAL_THRESHOLD_EUR", "10000"))


def agent_port(agent_name: str) -> int:
    return int(os.getenv("AGENT_PORT", DEFAULT_PORTS[agent_name]))


def agent_bind_host() -> str:
    return os.getenv("AGENT_BIND_HOST", "127.0.0.1")


def agent_public_url(agent_name: str) -> str:
    """URL under which OTHER services reach this agent (override with AGENT_PUBLIC_URL)."""
    return os.getenv("AGENT_PUBLIC_URL", f"http://127.0.0.1:{agent_port(agent_name)}").rstrip("/")
