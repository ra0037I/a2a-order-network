"""Append-only audit log stored as JSON Lines in a local file.

The file is written by the `append_audit_log` tool and exposed read-only to
MCP clients as the resource `audit://events` (and `audit://events/{n}`).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
AUDIT_LOG_PATH = Path(os.getenv("AUDIT_LOG_PATH", BASE_DIR / "data" / "audit.log"))

ALLOWED_LEVELS = {"INFO", "WARNING", "ERROR", "SECURITY"}
MAX_MESSAGE_LEN = 1000
_lock = threading.Lock()


def _next_seq() -> int:
    if not AUDIT_LOG_PATH.exists():
        return 1
    with AUDIT_LOG_PATH.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip()) + 1


def append_event(
    event_type: str,
    message: str,
    level: str = "INFO",
    actor: str = "agent",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    level = (level or "INFO").upper()
    if level not in ALLOWED_LEVELS:
        raise ValueError(f"level must be one of {sorted(ALLOWED_LEVELS)}")
    if not event_type or not event_type.strip():
        raise ValueError("event_type must not be empty")

    # Sanitize: single line, bounded length -> no log injection / log flooding
    clean = lambda s: " ".join(str(s).split())[:MAX_MESSAGE_LEN]

    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        entry = {
            "seq": _next_seq(),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": level,
            "event_type": clean(event_type).upper().replace(" ", "_"),
            "actor": clean(actor),
            "message": clean(message),
            "details": details or {},
        }
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_events(last_n: int | None = None) -> str:
    if not AUDIT_LOG_PATH.exists():
        return "(audit log is empty)"
    lines = [l for l in AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    if last_n:
        lines = lines[-last_n:]
    return "\n".join(lines) if lines else "(audit log is empty)"
