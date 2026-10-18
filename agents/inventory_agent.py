"""Inventory & Pricing Agent  (A2A server, port 8102)

Checks stock and calculates customer-specific prices. It does NOT own any
business logic itself: it uses the MCP server from Homework 1
(mcp_inventory/server.py, streamable HTTP on port 8200) "under the hood" -
A2A on the outside, MCP on the inside.

Run:  python -m agents.inventory_agent      (MCP server must be running)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastmcp import Client as MCPClient
from pydantic_ai import Agent, ModelRetry, RunContext

from agents import mock_support as m
from agents.models import PricedLine, PricingResult
from common.a2a_server import create_a2a_app, serve
from common.llm import build_model, model_settings
from common.logging_setup import setup_logging
from common.settings import MCP_INVENTORY_URL

NAME = "inventory-pricing-agent"
log = setup_logging(NAME)

SYSTEM_PROMPT = """You are the Inventory & Pricing Agent of an industrial automation supplier.
Input: structured order data (customer incl. pricing tier + line items with description/quantity/sku_hint),
provided as JSON after the line '### DATA'.
For EVERY line item:
1. Find the exact product with search_products (use sku if a sku_hint is given, otherwise search with
   2-4 significant words of the description, e.g. "proximity sensor M8"). If several products match, pick
   the one that matches the technical details best. If nothing matches, add the description to not_found.
2. Call price_line_item with sku, quantity and EXACTLY the customer's tier from the input.
Then call log_quote_event once (event_type QUOTE_PRICED, include customer_id and the net total in details).
Return the structured PricingResult; copy unit_price, discount_pct (= final_discount_rate_pct), net_total,
stock_qty, available and shortfall unchanged from the price_line_item results."""


async def _mcp(tool: str, args: dict[str, Any]) -> Any:
    """Call a tool on the MCP inventory server and return its JSON result."""
    args = {k: v for k, v in args.items() if v is not None}
    log.info("[MCP] -> %s(%s) @ %s", tool, json.dumps(args, ensure_ascii=False), MCP_INVENTORY_URL)
    try:
        async with MCPClient(MCP_INVENTORY_URL) as client:
            result = await client.call_tool(tool, args)
    except Exception as exc:  # tool errors go back to the LLM as observation
        log.warning("[MCP] <- %s ERROR %s", tool, exc)
        return {"error": str(exc)}
    text = "\n".join(c.text for c in (result.content or []) if getattr(c, "text", None))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"result": text}


def _mock(messages, info):
    prompt = m.user_prompt(messages)
    returns = m.tool_returns(messages)
    order = m.data_block(prompt)
    items = order["line_items"]
    tier = order["customer"]["tier"]
    done = [n for n, _ in returns]
    if "search_products" not in done:
        return m.call(*[("search_products", {"sku": i["sku_hint"]} if i.get("sku_hint") else
                         {"search": " ".join(re.findall(r"[A-Za-z0-9.\"]+", i["description"])[-3:])}) for i in items])
    searches = [m.as_json(c) for n, c in returns if n == "search_products"]
    if "price_line_item" not in done:
        calls = [("price_line_item", {"sku": s["products"][0]["sku"], "quantity": i["quantity"], "customer_tier": tier})
                 for s, i in zip(searches, items) if s.get("products")]
        return m.call(*calls)
    prices = [m.as_json(c) for n, c in returns if n == "price_line_item"]
    lines = [{"sku": p["stock_check"]["sku"], "name": p["stock_check"]["name"], "category": p["category"],
              "quantity": p["quantity"], "unit_price": p["unit_price"], "discount_pct": p["final_discount_rate_pct"],
              "net_total": p["net_total"], "stock_qty": p["stock_check"]["stock_qty"],
              "available": p["stock_check"]["available"], "shortfall": p["stock_check"]["shortfall"]} for p in prices]
    result = {"customer_id": order["customer"].get("customer_id"), "tier": tier, "lines": lines,
              "order_net_total": round(sum(l["net_total"] for l in lines), 2),
              "items_with_shortfall": [l["sku"] for l in lines if l["shortfall"] > 0],
              "not_found": [i["description"] for s, i in zip(searches, items) if not s.get("products")]}
    if "log_quote_event" not in done:
        return m.call(("log_quote_event", {"event_type": "QUOTE_PRICED", "message": f"priced order for {result['customer_id']}",
                                           "details": {"order_net_total": result["order_net_total"]}}))
    return m.final(info, result)


agent = Agent(build_model(_mock), system_prompt=SYSTEM_PROMPT, name=NAME, model_settings=model_settings(),
              output_type=PricingResult, retries=2)


@agent.output_validator
def _lines_from_tool_results(ctx: RunContext, output: PricingResult) -> PricingResult:
    """Numbers come from price_line_item (MCP formula engine), never from the LLM's own text."""
    try:
        order = m.data_block(m.prompt_text(ctx))
    except Exception:
        order = None
    tier = order["customer"]["tier"] if order else output.tier
    priced: dict[str, dict] = {}
    for r in m.tool_results(ctx.messages, "price_line_item"):
        if isinstance(r, dict) and isinstance(r.get("stock_check"), dict):
            priced[r["stock_check"]["sku"]] = r          # last call per SKU wins
    wrong_tier = [s for s, r in priced.items() if r.get("customer_tier") != tier]
    if wrong_tier:
        raise ModelRetry(f"price_line_item must be called with customer_tier='{tier}' - call it again for {wrong_tier}.")
    missing = [l.sku for l in output.lines if l.sku not in priced]
    if missing:
        raise ModelRetry(f"No price_line_item result for {missing} - call price_line_item for every line.")
    if order and len(output.lines) + len(output.not_found) < len(order["line_items"]):
        raise ModelRetry(f"The order has {len(order['line_items'])} line items, but only {len(output.lines)} are priced "
                         f"and {len(output.not_found)} listed as not_found. Price every line item.")
    if order:   # quantities must be exactly the ordered ones (not e.g. a shortfall)
        ordered = sorted(i["quantity"] for i in order["line_items"])
        priced_qty = sorted(priced[l.sku]["quantity"] for l in output.lines)
        remaining = list(ordered)
        for q in priced_qty:
            if q not in remaining:
                raise ModelRetry(f"Quantities must be exactly as ordered {ordered} - price_line_item was called with "
                                 f"{priced_qty}. Call price_line_item again with the ordered quantity of every line item.")
            remaining.remove(q)
    lines = []
    for sku in dict.fromkeys(l.sku for l in output.lines):
        r = priced[sku]
        lines.append(PricedLine(sku=sku, name=r["stock_check"]["name"], category=r.get("category") or "",
                                quantity=r["quantity"], unit_price=r["unit_price"],
                                discount_pct=r["final_discount_rate_pct"], net_total=r["net_total"],
                                stock_qty=r["stock_check"]["stock_qty"], available=r["stock_check"]["available"],
                                shortfall=r["stock_check"]["shortfall"]))
    customer_id = order["customer"].get("customer_id") if order else output.customer_id
    return PricingResult(customer_id=customer_id, tier=tier, lines=lines, not_found=output.not_found)


@agent.tool_plain
async def search_products(search: str | None = None, sku: str | None = None, category: str | None = None) -> dict:
    """Search the product inventory (via MCP tool query_inventory).

    Args:
        search: Words that must appear in the product name, e.g. 'safety relay' or 'proximity sensor M8'.
        sku: Exact article number, e.g. 'PLC-1200'.
        category: Controllers | HMI | Sensors | Drives | Cables | Networking | Safety.
    """
    return await _mcp("query_inventory", {"search": search, "sku": sku, "category": category, "limit": 5})


@agent.tool_plain
async def price_line_item(sku: str, quantity: int, customer_tier: str = "standard") -> dict:
    """Calculate the discounted price and stock availability of one order line
    (via MCP tool calculate_tiered_discount - graduated volume tiers, loyalty bonus, category caps).

    Args:
        sku: Exact article number from search_products.
        quantity: Ordered units.
        customer_tier: standard | silver | gold | platinum.
    """
    return await _mcp("calculate_tiered_discount", {"sku": sku, "quantity": quantity, "customer_tier": customer_tier})


@agent.tool_plain
async def log_quote_event(event_type: str, message: str, level: str = "INFO", details: dict | None = None) -> dict:
    """Write an entry into the audit log (via MCP tool append_audit_log).

    Args:
        event_type: e.g. QUOTE_PRICED, LOW_STOCK_ALERT.
        message: One-line description.
        level: INFO | WARNING | ERROR.
        details: Structured data such as customer_id and totals.
    """
    return await _mcp("append_audit_log", {"event_type": event_type, "message": message, "level": level,
                                           "actor": NAME, "details": details})


app = create_a2a_app(
    agent, NAME,
    description="Checks stock levels and calculates customer-specific prices with tiered volume discounts "
                "for industrial automation products (uses the MCI-Inventory MCP server internally).",
    skills=[{
        "id": "stock-and-pricing",
        "name": "Stock check and tiered pricing",
        "description": "For a list of line items: identify SKUs, check inventory availability and shortfalls, "
                       "compute discounted net prices for the customer tier.",
        "tags": ["inventory", "stock", "availability", "pricing", "price", "discount", "quote", "sku"],
        "examples": ["Price these line items for a gold customer and check stock: ..."],
        "input_modes": ["application/json", "text/plain"],
        "output_modes": ["application/json"],
    }],
)

if __name__ == "__main__":
    serve(app, NAME)
