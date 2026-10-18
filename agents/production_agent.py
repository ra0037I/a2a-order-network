"""Production Planning Agent  (A2A server, port 8103)

Plans production orders for items that cannot be delivered from stock and
returns realistic ship dates based on line capacity, backlog, setup and QA times.

Run:  python -m agents.production_agent
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta

from pydantic_ai import Agent, ModelRetry, RunContext

from agents import mock_support as m
from agents.models import ProductionOrder, ProductionResult
from common.a2a_server import create_a2a_app, serve
from common.llm import build_model, model_settings
from common.logging_setup import setup_logging

NAME = "production-planning-agent"
log = setup_logging(NAME)

# --- mini MES master data --------------------------------------------------------------------
PREFIX_TO_CATEGORY = {"PLC": "Controllers", "HMI": "HMI", "SEN": "Sensors", "DRV": "Drives",
                      "CAB": "Cables", "NET": "Networking", "SAF": "Safety"}
LINES = {
    # category: line, units/day, setup days, current backlog (working days), QA/test days
    "Controllers": {"line": "L1-Electronics", "units_per_day": 25, "setup_days": 2, "backlog_days": 3, "qa_days": 1},
    "HMI":         {"line": "L1-Electronics", "units_per_day": 15, "setup_days": 2, "backlog_days": 3, "qa_days": 1},
    "Networking":  {"line": "L1-Electronics", "units_per_day": 30, "setup_days": 2, "backlog_days": 3, "qa_days": 1},
    "Sensors":     {"line": "L2-Sensors",     "units_per_day": 400, "setup_days": 1, "backlog_days": 2, "qa_days": 0},
    "Drives":      {"line": "L3-Power",       "units_per_day": 20, "setup_days": 2, "backlog_days": 5, "qa_days": 1},
    "Cables":      {"line": "L4-Cables",      "units_per_day": 1500, "setup_days": 0, "backlog_days": 1, "qa_days": 0},
    "Safety":      {"line": "L5-Safety",      "units_per_day": 60, "setup_days": 1, "backlog_days": 4, "qa_days": 2},
}
SHIPPING_DAYS_FROM_STOCK = 2

SYSTEM_PROMPT = """You are the Production Planning Agent (MES) of an industrial automation manufacturer.
Input: priced order lines as JSON after the line '### DATA'; some lines have shortfall > 0.
- For every line with shortfall > 0 call plan_production(sku, shortfall).
- Call ship_date_from_stock() once to know when in-stock items can ship.
Return the structured ProductionResult. complete_order_ship_date = the latest of stock_ship_date and all
production earliest_ship_date values."""


def _add_working_days(start: date, days: int) -> date:
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def _mock(messages, info):
    prompt = m.user_prompt(messages)
    returns = m.tool_returns(messages)
    data = m.data_block(prompt)
    short = [l for l in data["lines"] if l.get("shortfall", 0) > 0]
    done = [n for n, _ in returns]
    if not done:
        return m.call(("ship_date_from_stock", {}), *[("plan_production", {"sku": l["sku"], "quantity": l["shortfall"]}) for l in short])
    stock_date = next(m.as_json(c) for n, c in returns if n == "ship_date_from_stock")["ship_date"]
    orders = [m.as_json(c) for n, c in returns if n == "plan_production"]
    complete = max([stock_date] + [o["earliest_ship_date"] for o in orders])
    return m.final(info, {"stock_ship_date": stock_date, "production_orders": orders,
                          "complete_order_ship_date": complete, "partial_delivery_possible": bool(orders)})


agent = Agent(build_model(_mock), system_prompt=SYSTEM_PROMPT, name=NAME, model_settings=model_settings(),
              output_type=ProductionResult, retries=2)


@agent.output_validator
def _plan_from_tool_results(ctx: RunContext, output: ProductionResult) -> ProductionResult:
    """Every shortfall must be planned with exactly its shortfall quantity; dates come from the tools."""
    try:
        pricing = m.data_block(m.prompt_text(ctx))
    except Exception:
        return output
    required = {l["sku"]: l["shortfall"] for l in pricing.get("lines", []) if l.get("shortfall", 0) > 0}
    plans: dict[str, dict] = {}
    for r in m.tool_results(ctx.messages, "plan_production"):
        if isinstance(r, dict) and "error" not in r:
            plans[r["sku"]] = r                            # last call per SKU wins
    wrong = [f"plan_production(sku='{s}', quantity={q})" for s, q in required.items()
             if plans.get(s, {}).get("quantity") != q]
    if wrong:
        raise ModelRetry("Plan exactly the shortfall quantities from the input. Still missing or wrong: " + ", ".join(wrong))
    stock = m.tool_results(ctx.messages, "ship_date_from_stock")
    if not stock:
        raise ModelRetry("Call ship_date_from_stock() once.")
    stock_date = stock[-1]["ship_date"]
    orders = [ProductionOrder(**{k: plans[s][k] for k in ProductionOrder.model_fields}) for s in required]
    complete = max([stock_date] + [o.earliest_ship_date for o in orders])
    in_stock_items = any(l.get("stock_qty", 0) > 0 for l in pricing.get("lines", []))
    return ProductionResult(stock_ship_date=stock_date, production_orders=orders, complete_order_ship_date=complete,
                            partial_delivery_possible=bool(orders) and in_stock_items)


@agent.tool_plain
def plan_production(sku: str, quantity: int) -> dict:
    """Create a production plan for a quantity of an article and return the earliest completion and ship date.

    Takes the line's current backlog, setup time, daily capacity and QA/test time into account
    (working days, Mon-Fri).

    Args:
        sku: Article number, e.g. 'SAF-RLY01'.
        quantity: Units to produce (the stock shortfall).
    """
    category = PREFIX_TO_CATEGORY.get(sku.split("-")[0].upper())
    if category is None or quantity <= 0:
        return {"error": f"cannot plan production for sku={sku}, quantity={quantity}"}
    line = LINES[category]
    production_days = math.ceil(quantity / line["units_per_day"])
    total = line["backlog_days"] + line["setup_days"] + production_days + line["qa_days"]
    completion = _add_working_days(date.today(), total)
    ship = _add_working_days(completion, 1)
    result = {"sku": sku.upper(), "quantity": quantity, "category": category, "line": line["line"],
              "backlog_days": line["backlog_days"], "setup_days": line["setup_days"],
              "production_days": production_days, "qa_days": line["qa_days"], "total_working_days": total,
              "earliest_completion": completion.isoformat(), "earliest_ship_date": ship.isoformat()}
    log.info("[TOOL] plan_production(%s, %s) -> %s on %s, ship %s", sku, quantity, total, line["line"], ship)
    return result


@agent.tool_plain
def ship_date_from_stock() -> dict:
    """Earliest ship date for items that are available from stock (picking + packing)."""
    d = _add_working_days(date.today(), SHIPPING_DAYS_FROM_STOCK)
    log.info("[TOOL] ship_date_from_stock() -> %s", d)
    return {"ship_date": d.isoformat(), "working_days": SHIPPING_DAYS_FROM_STOCK}


app = create_a2a_app(
    agent, NAME,
    description="Production planning / MES agent: schedules production orders for stock shortfalls and "
                "calculates lead times and earliest ship dates based on line capacity and backlog.",
    skills=[{
        "id": "production-scheduling",
        "name": "Production scheduling and lead times",
        "description": "Plan production for missing quantities and return lead time, earliest completion and ship date.",
        "tags": ["production", "planning", "lead time", "delivery date", "ship date", "capacity", "shortfall", "mes"],
        "examples": ["SAF-RLY01 has a shortfall of 360 units - when can we deliver?"],
        "input_modes": ["application/json", "text/plain"],
        "output_modes": ["application/json"],
    }],
)

if __name__ == "__main__":
    serve(app, NAME)
