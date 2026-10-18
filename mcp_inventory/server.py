"""MCP server "MCI-Inventory-Tools" (reused from Homework 1, Option A).

In Homework 2 it runs as its own network service (streamable HTTP, port 8200) and is
used "under the hood" by the inventory-pricing-agent.

Exposes three MCP tools plus resources and a prompt:

  Tools
    query_inventory            - SQL lookup (sqlite3) on the product inventory
    calculate_tiered_discount  - formula engine for graduated volume discounts
    append_audit_log           - appends an event to a local audit log file

  Resources
    audit://events             - full audit log (the file written by append_audit_log)
    audit://events/{n}         - last n audit events (resource template)
    inventory://schema         - DB schema + categories

  Prompts
    sales_quote_workflow       - template instructing an agent how to build a quote

Run:
    python server.py                 # stdio transport (spawned by agent.py)
    python server.py --http          # streamable HTTP on http://127.0.0.1:8000/mcp
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

import audit_log
import inventory_db
from discount_engine import LOYALTY_BONUS, compute_tiered_discount

# ---------------------------------------------------------------------------
# Logging: NEVER log to stdout - in stdio mode stdout is the MCP channel.
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(LOG_DIR / "mcp_inventory.log", encoding="utf-8")],
)
log = logging.getLogger("mcp-server")

mcp = FastMCP(
    "MCI-Inventory-Tools",
    instructions=(
        "Tools for an industrial automation supplier: look up product inventory, "
        "compute tiered volume discounts and write audit events."
    ),
)


# ===========================================================================
# TOOL 1 - SQL database lookup
# ===========================================================================
@mcp.tool()
def query_inventory(
    sku: str | None = None,
    search: str | None = None,
    category: str | None = None,
    low_stock_only: bool = False,
    min_stock: int | None = None,
    sort_by: str = "sku",
    limit: int = 20,
) -> dict[str, Any]:
    """Look up products in the inventory database (SQLite).

    All filters are optional and combined with AND.

    Args:
        sku: Exact article number, e.g. 'PLC-1200'.
        search: Free-text match on product name or SKU, e.g. 'sensor'.
        category: One of Controllers, HMI, Sensors, Drives, Cables, Networking, Safety.
        low_stock_only: If true, only products at or below their reorder level.
        min_stock: Only products with at least this many units in stock.
        sort_by: sku | name | category | unit_price | stock_qty.
        limit: Max rows to return (1-100).

    Returns: matching products with unit_price (EUR, net), stock_qty, warehouse,
    needs_reorder and stock_value_eur.
    """
    log.info("query_inventory sku=%s search=%s category=%s low_stock_only=%s min_stock=%s",
             sku, search, category, low_stock_only, min_stock)
    rows = inventory_db.query_products(sku, search, category, low_stock_only, min_stock, sort_by, limit)
    return {"count": len(rows), "products": rows}


# ===========================================================================
# TOOL 2 - Formula engine: tiered discounts
# ===========================================================================
@mcp.tool()
def calculate_tiered_discount(
    quantity: int,
    sku: str | None = None,
    unit_price: float | None = None,
    customer_tier: str = "standard",
    category: str | None = None,
) -> dict[str, Any]:
    """Compute the discounted price of an order line using graduated volume tiers.

    Rules: graduated volume bands (1-9: 0%, 10-49: 5%, 50-99: 10%, 100-249: 15%,
    250+: 20%, each unit priced in its own band) -> blended rate; plus loyalty bonus
    (standard 0, silver +2, gold +4, platinum +6 percentage points); plus order-value
    bonus (+1.5 pp from 10,000 EUR, +3 pp from 50,000 EUR); capped per category
    (Controllers 18%, HMI 20%, Drives 22%, Safety 15%, others 30%).

    Args:
        quantity: Number of units ordered (> 0).
        sku: Article number - price and category are then taken from the inventory DB.
        unit_price: Net unit price in EUR (only needed if no sku is given).
        customer_tier: standard | silver | gold | platinum.
        category: Product category (only needed if no sku is given).

    Returns: full calculation breakdown incl. per-band discounts, final discount rate,
    net total and a stock availability check.
    """
    log.info("calculate_tiered_discount qty=%s sku=%s unit_price=%s tier=%s", quantity, sku, unit_price, customer_tier)
    stock_info = None
    if sku:
        product = inventory_db.get_product(sku)
        if product is None:
            raise ToolError(f"Unknown SKU '{sku}'. Use query_inventory to find valid SKUs.")
        unit_price = product["unit_price"]
        category = product["category"]
        stock_info = {
            "sku": product["sku"],
            "name": product["name"],
            "stock_qty": product["stock_qty"],
            "available": product["stock_qty"] >= quantity,
            "shortfall": max(0, quantity - product["stock_qty"]),
        }
    if unit_price is None:
        raise ToolError("Provide either a sku or a unit_price.")

    try:
        result = compute_tiered_discount(quantity, unit_price, customer_tier, category)
    except ValueError as exc:  # invalid business input -> clean error for the LLM
        raise ToolError(str(exc)) from exc
    if stock_info:
        result["stock_check"] = stock_info
    return result


# ===========================================================================
# TOOL 3 - Audit log (writes to a local file that is also exposed as resource)
# ===========================================================================
@mcp.tool()
def append_audit_log(
    event_type: str,
    message: str,
    level: str = "INFO",
    actor: str = "react-agent",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append an event to the append-only audit log file (resource audit://events).

    Use this to record business-relevant actions, e.g. a generated quote, a stock
    shortfall or a rejected request.

    Args:
        event_type: Short event code, e.g. QUOTE_CREATED, LOW_STOCK_ALERT, INVENTORY_QUERY.
        message: Human-readable one-line description.
        level: INFO | WARNING | ERROR | SECURITY.
        actor: Who triggered the event.
        details: Optional structured data (sku, quantity, totals, ...).
    """
    try:
        entry = audit_log.append_event(event_type, message, level, actor, details)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    log.info("append_audit_log seq=%s type=%s", entry["seq"], entry["event_type"])
    return {"status": "logged", "entry": entry, "resource_uri": "audit://events"}


# ===========================================================================
# RESOURCES (read-only data)
# ===========================================================================
@mcp.resource("audit://events", mime_type="application/jsonl")
def audit_events() -> str:
    """The complete audit log file (JSON Lines)."""
    return audit_log.read_events()


@mcp.resource("audit://events/{n}", mime_type="application/jsonl")
def audit_events_last(n: int) -> str:
    """The last n audit log events."""
    return audit_log.read_events(int(n))


@mcp.resource("inventory://schema", mime_type="text/plain")
def inventory_schema() -> str:
    """Schema of the inventory database and the available categories."""
    return inventory_db.schema_description()


# ===========================================================================
# PROMPT (workflow template)
# ===========================================================================
@mcp.prompt()
def sales_quote_workflow(customer: str, request: str) -> str:
    """Template that tells an agent how to process a customer quote request."""
    tiers = ", ".join(LOYALTY_BONUS)
    return (
        f"You are a sales engineer. Customer: {customer}. Request: {request}\n"
        "1. Use query_inventory to identify the exact SKU and check stock.\n"
        f"2. Use calculate_tiered_discount with sku, quantity and customer tier ({tiers}).\n"
        "3. If stock is insufficient, log a LOW_STOCK_ALERT (level WARNING).\n"
        "4. Log the quote with append_audit_log (event_type QUOTE_CREATED, include totals in details).\n"
        "5. Answer with a short quote summary: product, quantity, gross, discount %, net total, availability."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCI Inventory MCP server")
    parser.add_argument("--http", action="store_true", help="serve via streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    inventory_db.get_connection().close()  # create + seed DB on startup
    if args.http:
        log.info("Starting MCP server (HTTP) on http://%s:%s/mcp", args.host, args.port)
        mcp.run(transport="http", host=args.host, port=args.port, show_banner=False)
    else:
        log.info("Starting MCP server (stdio)")
        mcp.run(show_banner=False)
