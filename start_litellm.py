"""Start the local LiteLLM proxy with the keys from .env (works from any folder / terminal).

    python start_litellm.py            (stop with Ctrl+C)

Checks before starting:
  * OPENAI_API_KEY and LITELLM_MASTER_KEY are set in .env
  * LITELLM_KEY (used by agent.py) == LITELLM_MASTER_KEY (used by the proxy)
  * port 4000 is free (no old proxy still running)
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
PORT = int(os.getenv("LITELLM_PORT", "4000"))


def fail(msg: str) -> None:
    print(f"[start_litellm] ERROR: {msg}")
    sys.exit(1)


def main() -> None:
    load_dotenv(BASE / ".env", override=True)  # .env wins over stale terminal variables
    openai_key = os.getenv("OPENAI_API_KEY", "")
    master = os.getenv("LITELLM_MASTER_KEY", "")
    agent_key = os.getenv("LITELLM_KEY", "")

    if not openai_key.startswith("sk-"):
        fail("OPENAI_API_KEY missing in .env")
    if not master.startswith("sk-") or master == "sk-1234":
        fail("LITELLM_MASTER_KEY missing in .env (must start with 'sk-' and must not be sk-1234)")
    if agent_key != master:
        fail("LITELLM_KEY and LITELLM_MASTER_KEY in .env differ - agent.py would be rejected (No connected db)")

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            fail(f"port {PORT} is already in use - an old LiteLLM proxy is probably still running. "
                 f"Stop it (Ctrl+C in its terminal or close that terminal) and try again.")

    exe = shutil.which("litellm", path=str(Path(sys.executable).parent)) or shutil.which("litellm")
    cmd = [exe] if exe else [sys.executable, "-m", "litellm.proxy.proxy_cli"]
    cmd += ["--config", str(BASE / "litellm_config.yaml"), "--port", str(PORT)]
    print(f"[start_litellm] master key {master[:6]}...  OpenAI key {openai_key[:6]}...  -> http://localhost:{PORT}")
    try:
        subprocess.run(cmd, cwd=BASE, env=os.environ.copy())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
