from __future__ import annotations

import logging
import os
import sys

from .settings import LOG_DIR

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


def setup_logging(service: str) -> logging.Logger:
    """Console + logs/<service>.log, one consistent format for all services."""
    fmt = logging.Formatter(f"%(asctime)s [{service}] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file_handler = logging.FileHandler(LOG_DIR / f"{service}.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file_handler)
    for noisy in ("httpx", "httpcore", "openai", "mcp", "fastmcp", "uvicorn.access", "docket", "fakeredis"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(service)
