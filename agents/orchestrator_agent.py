"""Orchestrator (Root) Agent  (A2A server, port 8100)

Coordinates the order-to-quote process. It has NO hard-coded knowledge of the
other agents: at runtime it looks up the agent that offers a required
capability (skill id) in the registry (find_agents), resolves that agent's URL
from the registry and delegates the sub-task via the A2A protocol (ask_agent).

The LLM plans and writes the quote, but the workflow is enforced in code
(workflow guard):
  * find_agents only accepts the three capability ids - the LLM cannot "search wrong"
  * stock-and-pricing is refused until order-extraction has delivered a valid order
    (incl. the CRM pricing tier); the validated order JSON is attached to the request
  * production-scheduling is refused unless pricing reported a shortfall
  * check_approval_policy takes the net total from the pricing result, not from the LLM
  * the final answer is rejected (ModelRetry) until all required steps are done

Run:  python -m agents.orchestrator_agent
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal

from pydantic import ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from agents import mock_support as m
from agents.models import IntakeResult, PricingResult, ProductionResult
from common import registry_client
from common.a2a_client import send_message
from common.a2a_server import create_a2a_app, serve
from common.llm import build_model, model_settings
from common.logging_setup import setup_logging
from common.settings import APPROVAL_THRESHOLD_EUR

NAME = "orchestrator-agent"
log = setup_logging(NAME)

Capability = Literal["order-extraction", "stock-and-pricing", "production-scheduling"]
RESULT_MODELS = {"order-extraction": IntakeResult, "stock-and-pricing": PricingResult,
                 "production-scheduling": ProductionResult}

SYSTEM_PROMPT = f"""You are the Root/Orchestrator Agent for the order-to-quote process of an industrial
automation supplier. You do not know any other agent in advance - always discover them by capability:

1. find_agents("order-extraction"), then ask_agent(<that agent>, <the full customer e-mail>).
2. find_agents("stock-and-pricing"), then ask_agent(<that agent>, "Check stock and price this order.").
   The validated order data from step 1 is attached automatically.
3. Only if the pricing result has items_with_shortfall: find_agents("production-scheduling"), then
   ask_agent(<that agent>, "Plan production for the shortfalls."). The pricing data is attached automatically.
4. Call check_approval_policy().
5. Then answer with the single word DONE. The quote document is generated automatically from the validated
   results of the agents - do not write prices, totals or dates yourself.
Rules: only use agent names returned by find_agents; never invent prices, stock or dates; if a tool returns
a WORKFLOW error, follow its instruction. Approval threshold: {APPROVAL_THRESHOLD_EUR:.0f} EUR."""


# ---------------------------------------------------------------------------
# Workflow state, derived from the run's own message history (stateless server)
# ---------------------------------------------------------------------------
def _completed_steps(messages) -> dict[str, Any]:
    """{skill: validated result model} for every successful ask_agent call so far, plus 'approval'."""
    done: dict[str, Any] = {}
    for msg in messages:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            if not isinstance(part, ToolReturnPart) or not isinstance(part.content, dict):
                continue
            c = part.content
            if part.tool_name == "ask_agent" and c.get("valid") and c.get("skill") in RESULT_MODELS:
                done[c["skill"]] = RESULT_MODELS[c["skill"]].model_validate_json(c["response"])
            elif part.tool_name == "check_approval_policy" and "status" in c:
                done["approval"] = c
    return done


def _missing_steps(done: dict[str, Any]) -> list[str]:
    missing = []
    if "order-extraction" not in done:
        missing.append('find_agents("order-extraction") + ask_agent with the customer e-mail')
    if "stock-and-pricing" not in done:
        missing.append('find_agents("stock-and-pricing") + ask_agent')
    elif done["stock-and-pricing"].items_with_shortfall and "production-scheduling" not in done:
        missing.append('find_agents("production-scheduling") + ask_agent (pricing reported shortfalls)')
    if "approval" not in done:
        missing.append("check_approval_policy()")
    return missing


# ---------------------------------------------------------------------------
# Offline mock (LLM_PROVIDER=mock). MOCK_MISBEHAVE=1 simulates an LLM that skips
# steps, to demonstrate that the workflow guard catches it.
# ---------------------------------------------------------------------------
def _mock(messages, info):
    prompt = m.user_prompt(messages)
    r = m.tool_returns(messages)
    calls = [n for n, _ in r]
    asks = [m.as_json(c) for t, c in r if t == "ask_agent"]
    valid = {a["skill"]: a for a in asks if a.get("valid")}
    last_find = next((m.as_json(c) for t, c in reversed(r) if t == "find_agents"), None)
    misbehave = os.getenv("MOCK_MISBEHAVE") == "1"

    def ask(skill, request):
        return m.call(("ask_agent", {"agent_name": last_find["agents"][0]["name"], "request": request}))

    if misbehave and not r:  # skip intake, go straight to pricing
        return m.call(("find_agents", {"capability": "stock-and-pricing"}))
    if misbehave and calls == ["find_agents"]:
        return ask("stock-and-pricing", prompt)
    if "order-extraction" not in valid:
        if not calls or calls[-1] != "find_agents" or last_find["capability"] != "order-extraction":
            return m.call(("find_agents", {"capability": "order-extraction"}))
        return ask("order-extraction", prompt)
    if "stock-and-pricing" not in valid:
        if last_find["capability"] != "stock-and-pricing" or calls[-1] != "find_agents":
            return m.call(("find_agents", {"capability": "stock-and-pricing"}))
        return ask("stock-and-pricing", "Check stock and price this order.")
    pricing = json.loads(valid["stock-and-pricing"]["response"])
    if pricing["items_with_shortfall"] and "production-scheduling" not in valid:
        if last_find["capability"] != "production-scheduling" or calls[-1] != "find_agents":
            return m.call(("find_agents", {"capability": "production-scheduling"}))
        return ask("production-scheduling", "Plan production for the shortfalls.")
    if "check_approval_policy" not in calls and (not misbehave or m.retried(messages)):
        return m.call(("check_approval_policy", {}))
    approval = next((m.as_json(c) for t, c in r if t == "check_approval_policy"), {"status": "?"})
    lines = "\n".join(f"  {l['sku']:<10} {l['quantity']:>4} x {l['unit_price']:>8.2f}  -{l['discount_pct']:.2f}%  "
                      f"net {l['net_total']:>10.2f}  {'in stock' if l['available'] else 'shortfall ' + str(l['shortfall'])}"
                      for l in pricing["lines"])
    plan = json.loads(valid["production-scheduling"]["response"]) if "production-scheduling" in valid else None
    delivery = (f"complete order ships {plan['complete_order_ship_date']} (from stock: {plan['stock_ship_date']})"
                if plan else "all items from stock")
    used = [a["agent"] for a in asks if a.get("valid")]
    return m.answer(f"QUOTE (mock) for {pricing['customer_id']} ({pricing['tier']})\n{lines}\n"
                    f"Order net total: {pricing['order_net_total']:.2f} EUR\nDelivery: {delivery}\n"
                    f"Approval: {approval['status']}\nAgents involved: {', '.join(used)}")


agent = Agent(build_model(_mock), system_prompt=SYSTEM_PROMPT, name=NAME, model_settings=model_settings(),
              retries=4)


@agent.tool_plain
async def find_agents(capability: Capability) -> dict:
    """Look up live agents in the registry that offer a capability.

    Args:
        capability: One of
            'order-extraction'      - turns the customer e-mail into structured order data incl. CRM tier
            'stock-and-pricing'     - finds SKUs, checks stock, calculates tiered prices
            'production-scheduling' - plans production for shortfalls, returns ship dates
    """
    results = await registry_client.search_agents(skill=capability, limit=3)
    if not results:  # tolerant fallback: offer the best candidates by description
        results = await registry_client.search_agents(query=capability.replace("-", " "), limit=3)
    compact = [{"name": a["name"], "description": a["description"], "match": a.get("match"),
                "skills": [s.get("id") for s in a["skills"]]} for a in results if a["name"] != NAME]
    log.info("[REGISTRY] find_agents(%r) -> %s", capability, [(a["name"], a["match"]) for a in compact])
    if not compact:
        return {"capability": capability, "agents": [], "hint": "no live agent offers this capability - report it"}
    return {"capability": capability, "agents": compact}


@agent.tool
async def ask_agent(ctx: RunContext, agent_name: str, request: str) -> dict:
    """Delegate a task to another agent via the A2A protocol.

    The agent's address is resolved from the registry at call time. For stock-and-pricing and
    production-scheduling the validated results of the previous steps are attached automatically.

    Args:
        agent_name: Exact agent name as returned by find_agents.
        request: Task for that agent (for order-extraction: the full customer e-mail).
    """
    entry = await registry_client.get_agent(agent_name)
    if entry is None:
        return {"agent": agent_name, "valid": False, "error": "agent not found or not alive in registry"}
    skill = next((s["id"] for s in entry["skills"] if s.get("id") in RESULT_MODELS), None)
    done = _completed_steps(ctx.messages)

    # ---------------- workflow guard ----------------
    if skill in done:   # every step runs exactly once - a second call could overwrite a valid result
        log.warning("[GUARD] blocked %s: %s already completed - keeping the first validated result", agent_name, skill)
        return {"agent": agent_name, "skill": skill, "valid": False,
                "error": f"WORKFLOW: {skill} is already completed, its validated result is kept. "
                         f"Do not call it again. Still missing: {_missing_steps(done) or 'nothing - answer DONE'}"}
    if skill == "stock-and-pricing":
        intake = done.get("order-extraction")
        if intake is None:
            log.warning("[GUARD] blocked %s: order-extraction has not run yet", agent_name)
            return {"agent": agent_name, "skill": skill, "valid": False,
                    "error": "WORKFLOW: pricing needs the validated order incl. CRM customer tier. "
                             'First call find_agents("order-extraction") and ask that agent with the full e-mail.'}
        request = f"{request}\n\n{m.DATA_MARKER} (order data from order-extraction agent)\n{intake.model_dump_json()}"
    elif skill == "production-scheduling":
        pricing = done.get("stock-and-pricing")
        if pricing is None or not pricing.items_with_shortfall:
            reason = "pricing has not run yet" if pricing is None else "pricing reported no shortfall"
            log.warning("[GUARD] blocked %s: %s", agent_name, reason)
            return {"agent": agent_name, "skill": skill, "valid": False,
                    "error": f"WORKFLOW: production planning not allowed - {reason}."}
        request = f"{request}\n\n{m.DATA_MARKER} (pricing result from stock-and-pricing agent)\n{pricing.model_dump_json()}"

    url = entry["url"]
    log.info("[A2A] -> %s (%s) @ %s  (request: %d chars)", agent_name, skill, url, len(request))
    try:
        res = await send_message(url, request)
    except Exception as exc:
        log.warning("[A2A] <- %s FAILED: %s", agent_name, exc)
        return {"agent": agent_name, "skill": skill, "valid": False, "error": str(exc)}

    out = {"agent": agent_name, "skill": skill, "state": res["state"], "response": res["text"], "valid": False}
    if res["state"] == "completed" and skill in RESULT_MODELS:
        try:
            RESULT_MODELS[skill].model_validate_json(res["text"])
            out["valid"] = True
        except ValidationError as exc:
            out["error"] = f"agent returned invalid {RESULT_MODELS[skill].__name__}: {exc.errors()[:2]}"
    log.info("[A2A] <- %s state=%s valid=%s in %.1fs: %s", agent_name, res["state"], out["valid"],
             res["duration_s"], res["text"][:250].replace("\n", " "))
    return out


@agent.tool
def check_approval_policy(ctx: RunContext) -> dict:
    """Human-in-the-loop policy: quotes above the threshold need a sales manager's sign-off.
    Uses the order net total from the validated pricing result."""
    pricing = _completed_steps(ctx.messages).get("stock-and-pricing")
    if pricing is None:
        return {"error": "WORKFLOW: no pricing result yet - run stock-and-pricing first."}
    total = pricing.order_net_total
    needs = total > APPROVAL_THRESHOLD_EUR
    result = {"order_net_total_eur": total, "threshold_eur": APPROVAL_THRESHOLD_EUR,
              "requires_human_approval": needs,
              "status": "PENDING APPROVAL by sales manager" if needs else "AUTO-APPROVED (below threshold)"}
    log.info("[POLICY] check_approval_policy(%.2f) -> %s", total, result["status"])
    return result


def _eur(x: float) -> str:
    return f"{x:,.2f}"


def render_quote(done: dict[str, Any], agents_used: list[str]) -> str:
    """The quote is built in code from validated agent results - no number is written by an LLM."""
    intake: IntakeResult = done["order-extraction"]
    pricing: PricingResult = done["stock-and-pricing"]
    plan: ProductionResult | None = done.get("production-scheduling")
    approval = done["approval"]
    c = intake.customer
    ship = {o.sku: o for o in plan.production_orders} if plan else {}
    rows = [f"{'SKU':<10} {'Product':<32} {'Qty':>5} {'List EUR':>10} {'Disc %':>7} {'Net EUR':>12}  Availability"]
    for l in pricing.lines:
        avail = "in stock" if l.shortfall == 0 else (
            f"{l.stock_qty} in stock, {l.shortfall} produced, ships {ship[l.sku].earliest_ship_date}" if l.sku in ship
            else f"shortfall {l.shortfall}")
        rows.append(f"{l.sku:<10} {l.name[:32]:<32} {l.quantity:>5} {_eur(l.unit_price):>10} {l.discount_pct:>7.2f} "
                    f"{_eur(l.net_total):>12}  {avail}")
    out = [f"QUOTE - {c.name} (customer {c.customer_id or 'new prospect'}, tier {c.tier})",
           f"Payment terms: {c.payment_terms or '-'}   Requested delivery: {intake.requested_delivery or '-'}", "",
           *rows, "", f"ORDER NET TOTAL: {_eur(pricing.order_net_total)} EUR"]
    if pricing.not_found:
        out.append("NOT FOUND (no matching product): " + "; ".join(pricing.not_found))
    out += ["", "DELIVERY"]
    if plan:
        out.append(f"- Items from stock ship {plan.stock_ship_date}")
        for o in plan.production_orders:
            out.append(f"- {o.sku}: production of {o.quantity} pcs on {o.line} ({o.production_days} production days), "
                       f"ships {o.earliest_ship_date}")
        ok = ""
        if intake.requested_delivery:
            ok = " - meets requested date" if plan.complete_order_ship_date <= intake.requested_delivery \
                else " - LATER than requested date"
        out.append(f"- Complete order ships {plan.complete_order_ship_date}{ok}")
    else:
        out.append("- All items available from stock")
    out += ["", f"APPROVAL: {approval['status']} (threshold {_eur(approval['threshold_eur'])} EUR)",
            f"AGENTS INVOLVED: {', '.join(agents_used)}"]
    return "\n".join(out)


def _agents_used(messages) -> list[str]:
    names = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == "ask_agent" and isinstance(part.content, dict) \
                        and part.content.get("valid") and part.content["agent"] not in names:
                    names.append(part.content["agent"])
    return names


@agent.output_validator
def _ensure_workflow_complete(ctx: RunContext, output: str) -> str:
    done = _completed_steps(ctx.messages)
    missing = _missing_steps(done)
    if missing:
        log.warning("[GUARD] final answer rejected, missing: %s", missing)
        raise ModelRetry("WORKFLOW incomplete - before answering you must still: " + "; ".join(missing))
    quote = render_quote(done, _agents_used(ctx.messages))
    log.info("[QUOTE] generated from validated results (net total %.2f EUR)", done["stock-and-pricing"].order_net_total)
    return quote


app = create_a2a_app(
    agent, NAME,
    description="Root agent for the order-to-quote process: turns a customer e-mail into a priced, scheduled and "
                "approval-checked quote by dynamically discovering and coordinating specialised agents.",
    skills=[{
        "id": "order-to-quote",
        "name": "Order-to-quote orchestration",
        "description": "End-to-end processing of a customer request into a complete quote (extraction, pricing, "
                       "stock, production lead times, approval policy).",
        "tags": ["orchestrator", "root", "order to quote", "quote", "coordination", "workflow"],
        "examples": ["Create a quote for this customer e-mail: ..."],
        "input_modes": ["text/plain"],
        "output_modes": ["text/plain"],
    }],
)

if __name__ == "__main__":
    serve(app, NAME)
