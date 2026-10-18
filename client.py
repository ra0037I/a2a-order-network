"""Client: sends a customer e-mail into the agent network.

The client only knows the registry URL. It
  1. lists all live agents in the registry,
  2. searches the registry for the order-to-quote capability (-> orchestrator),
  3. sends the e-mail to that agent via A2A and prints the resulting quote.

Usage:
    python client.py --demo 1            # demo_emails/01_*.txt  (1, 2, 3 or all)
    python client.py --file my_mail.txt
    python client.py "Company: Inntal Robotics ... - 5 x PLC-1200"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from common import registry_client
from common.a2a_client import send_message
from common.settings import BASE_DIR, LOG_DIR, REGISTRY_URL

DEMO_DIR = BASE_DIR / "demo_emails"


def setup() -> tuple[logging.Logger, Path]:
    log_file = LOG_DIR / f"client_run_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = logging.Formatter("%(asctime)s [client] %(message)s", "%H:%M:%S")
    log = logging.getLogger("client")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return log, log_file


async def run(emails: list[tuple[str, str]], log: logging.Logger) -> None:
    agents = await registry_client.list_agents()
    log.info("Registry %s - %d live agents:", REGISTRY_URL, len(agents))
    for a in agents:
        log.info("   %-27s %-24s skills=%s", a["name"], a["url"], [s["id"] for s in a["skills"]])

    hits = await registry_client.search_agents("order to quote orchestration", limit=1)
    if not hits:
        log.error("no orchestrator found in the registry - is run_all.py running?")
        return
    orchestrator = hits[0]
    log.info("Registry lookup 'order to quote orchestration' -> %s @ %s", orchestrator["name"], orchestrator["url"])

    for title, text in emails:
        log.info("=" * 100)
        log.info("REQUEST: %s", title)
        log.info("=" * 100)
        log.info("\n%s", text.strip())
        log.info("-" * 100)
        res = await send_message(orchestrator["url"], text)
        log.info("A2A task %s finished: state=%s after %.1fs", res["task_id"], res["state"], res["duration_s"])
        log.info("QUOTE:\n%s\n", res["text"])


def main() -> None:
    ap = argparse.ArgumentParser(description="Send a customer request into the A2A agent network")
    ap.add_argument("text", nargs="*", help="request text")
    ap.add_argument("--demo", help="1, 2, 3 or all")
    ap.add_argument("--file", type=Path)
    args = ap.parse_args()

    files = sorted(DEMO_DIR.glob("*.txt"))
    if args.demo:
        chosen = files if args.demo == "all" else [f for f in files if f.name.startswith(f"{int(args.demo):02d}_")]
        emails = [(f.name, f.read_text(encoding="utf-8")) for f in chosen]
    elif args.file:
        emails = [(args.file.name, args.file.read_text(encoding="utf-8"))]
    elif args.text:
        emails = [("command line", " ".join(args.text))]
    else:
        ap.error("use --demo N, --file or a request text")

    log, log_file = setup()
    asyncio.run(run(emails, log))
    log.info("log written to %s", log_file.relative_to(BASE_DIR))


if __name__ == "__main__":
    main()
