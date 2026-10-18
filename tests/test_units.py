"""Unit tests for registry, A2A client parsing and the deterministic agent tools.

Run:  python -m pytest -q
"""
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from registry import service
from common.a2a_client import extract_result_text


def _card(name, desc, tags):
    return {"name": name, "description": desc, "version": "1.0.0",
            "skills": [{"id": name + "-skill", "name": desc, "description": desc, "tags": tags}]}


@pytest.fixture
def registry():
    service._agents.clear()
    now = time.time()
    for name, desc, tags in [
        ("order-intake-agent", "extract order data from customer e-mails", ["extraction", "email", "order intake"]),
        ("inventory-pricing-agent", "stock check and pricing", ["inventory", "stock", "pricing"]),
        ("production-planning-agent", "production planning and lead times", ["production", "lead time"]),
    ]:
        service._agents[name] = {"name": name, "url": f"http://x/{name}", "card": _card(name, desc, tags),
                                 "registered_at": now, "last_heartbeat": now}
    return TestClient(service.app)


def test_search_ranks_best_agent_first(registry):
    r = registry.get("/agents/search", params={"q": "inventory stock check and pricing"}).json()
    assert r["results"][0]["name"] == "inventory-pricing-agent"
    r = registry.get("/agents/search", params={"q": "when can production deliver - lead time"}).json()
    assert r["results"][0]["name"] == "production-planning-agent"


def test_stale_agents_are_hidden(registry):
    service._agents["production-planning-agent"]["last_heartbeat"] = time.time() - 3600
    names = [a["name"] for a in registry.get("/agents").json()["agents"]]
    assert "production-planning-agent" not in names
    assert registry.get("/agents/production-planning-agent").status_code == 404


def test_write_endpoints_require_key(registry):
    assert registry.post("/agents/register", json={"url": "http://127.0.0.1:9"}).status_code == 401
    assert registry.post("/agents/order-intake-agent/heartbeat").status_code == 401
    ok = registry.post("/agents/order-intake-agent/heartbeat", headers={"X-Registry-Key": service.REGISTRY_API_KEY})
    assert ok.status_code == 200
    unknown = registry.post("/agents/nope/heartbeat", headers={"X-Registry-Key": service.REGISTRY_API_KEY})
    assert unknown.status_code == 404


def test_register_rejects_unreachable_agent(registry):
    r = registry.post("/agents/register", json={"url": "http://127.0.0.1:9"},
                      headers={"X-Registry-Key": service.REGISTRY_API_KEY})
    assert r.status_code == 400


def test_a2a_result_parsing_old_and_new_shape():
    new = {"artifacts": [{"artifactId": "1", "parts": [{"text": "hello"}]}]}
    old = {"history": [{"role": "agent", "parts": [{"kind": "text", "text": "hi"}]}]}
    assert extract_result_text(new) == "hello"
    assert extract_result_text(old) == "hi"


def test_production_plan_uses_working_days():
    from agents.production_agent import plan_production
    plan = plan_production("SAF-RLY01", 360)
    # backlog 4 + setup 1 + ceil(360/60)=6 + QA 2 = 13 working days
    assert plan["total_working_days"] == 13
    assert date.fromisoformat(plan["earliest_ship_date"]).weekday() < 5
    assert "error" in plan_production("XYZ-1", 5)


def test_crm_lookup():
    from agents.intake_agent import lookup_customer
    assert lookup_customer("Tiroler Anlagenbau AG")["tier"] == "platinum"
    assert lookup_customer("alpenwerk gmbh")["customer_id"] == "C-10023"
    assert lookup_customer("Unknown Corp").get("new_prospect") is True


def test_search_by_skill_id_is_exact(registry):
    r = registry.get("/agents/search", params={"skill": "inventory-pricing-agent-skill"}).json()
    assert r["match"] == "skill" and [a["name"] for a in r["results"]] == ["inventory-pricing-agent"]
    assert registry.get("/agents/search", params={"skill": "does-not-exist"}).json()["results"] == []


def test_search_falls_back_to_candidates(registry):
    r = registry.get("/agents/search", params={"q": "safety relay supply"}).json()
    assert r["match"] == "fallback" and len(r["results"]) == 3


def test_workflow_guard_missing_steps():
    from agents.models import IntakeResult, PricingResult
    from agents.orchestrator_agent import _missing_steps
    intake = IntakeResult.model_validate({"customer": {"customer_id": "C-1", "name": "X", "tier": "gold"},
                                          "line_items": [{"description": "relay", "quantity": 5}]})
    line = {"sku": "SAF-RLY01", "name": "Relay", "category": "Safety", "quantity": 500, "unit_price": 178,
            "discount_pct": 15, "net_total": 75650, "stock_qty": 140, "available": False, "shortfall": 360}
    pricing = PricingResult.model_validate({"customer_id": "C-1", "tier": "gold", "lines": [line],
                                            "order_net_total": 0, "items_with_shortfall": []})
    assert pricing.order_net_total == 75650 and pricing.items_with_shortfall == ["SAF-RLY01"]  # recomputed
    assert len(_missing_steps({})) == 3
    missing = _missing_steps({"order-extraction": intake, "stock-and-pricing": pricing})
    assert any("production-scheduling" in s for s in missing) and any("approval" in s for s in missing)
    assert _missing_steps({"order-extraction": intake, "stock-and-pricing": pricing,
                           "production-scheduling": object(), "approval": {"status": "x"}}) == []


def _history(text):
    """A2A delivers the task as message history (no user_prompt) - test exactly that path."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    return [ModelRequest(parts=[UserPromptPart(content=text)])]


def test_intake_uses_known_company_even_if_llm_looks_up_person():
    """LLM looks up the sender 'Markus Huber' -> validator must still use the CRM record of Alpenwerk GmbH."""
    import asyncio
    from pydantic_ai.models.function import FunctionModel
    from agents import mock_support as m
    from agents.intake_agent import agent

    def wrong_lookup(messages, info):
        if not m.tool_returns(messages):
            return m.call(("lookup_customer", {"company_name": "Markus Huber"}))
        return m.final(info, {"customer": {"customer_id": None, "name": "Markus Huber", "tier": "standard"},
                              "line_items": [{"description": "proximity sensor M8", "quantity": 300}]})

    mail = "Company: Alpenwerk GmbH\n- 300 x inductive proximity sensor M8\nBest regards\nMarkus Huber"
    with agent.override(model=FunctionModel(wrong_lookup)):
        result = asyncio.run(agent.run(message_history=_history(mail)))
    assert result.output.customer.customer_id == "C-10023" and result.output.customer.tier == "gold"


def test_production_forces_exact_shortfall_quantity():
    """LLM plans 220 instead of 360 -> validator rejects, LLM retries with 360, result comes from the tool."""
    import asyncio, json
    from pydantic_ai.models.function import FunctionModel
    from agents import mock_support as m
    from agents.production_agent import agent

    pricing = {"lines": [{"sku": "SAF-RLY01", "quantity": 500, "stock_qty": 140, "shortfall": 360},
                         {"sku": "DRV-0550", "quantity": 12, "stock_qty": 12, "shortfall": 0}]}

    def sloppy(messages, info):
        qty = 360 if m.retried(messages) else 220
        done = [n for n, _ in m.tool_returns(messages)]
        if done.count("plan_production") < (2 if m.retried(messages) else 1):
            return m.call(("plan_production", {"sku": "SAF-RLY01", "quantity": qty}), ("ship_date_from_stock", {}))
        return m.final(info, {"stock_ship_date": "1999-01-01", "production_orders": [], "complete_order_ship_date": "1999-01-01",
                              "partial_delivery_possible": False})

    task = "Plan production.\n\n### DATA (pricing)\n" + json.dumps(pricing)
    with agent.override(model=FunctionModel(sloppy)):
        out = asyncio.run(agent.run(message_history=_history(task))).output
    assert [(o.sku, o.quantity) for o in out.production_orders] == [("SAF-RLY01", 360)]
    assert out.stock_ship_date != "1999-01-01" and out.partial_delivery_possible is True


def test_inventory_numbers_come_from_tool_not_llm(monkeypatch):
    """LLM writes a wrong net_total into its answer -> validator replaces it with the price_line_item result."""
    import asyncio, json
    from pydantic_ai.models.function import FunctionModel
    from agents import mock_support as m
    import agents.inventory_agent as inv
    from mcp_inventory.discount_engine import compute_tiered_discount

    async def fake_mcp(tool, args):
        if tool == "calculate_tiered_discount":
            r = compute_tiered_discount(args["quantity"], 178.0, args["customer_tier"], "Safety")
            r["stock_check"] = {"sku": args["sku"], "name": "Safety Relay Cat. 4", "stock_qty": 140,
                                "available": False, "shortfall": max(0, args["quantity"] - 140)}
            return r
        return {"status": "ok"}
    monkeypatch.setattr(inv, "_mcp", fake_mcp)

    def lying(messages, info):
        if not m.tool_returns(messages):
            return m.call(("price_line_item", {"sku": "SAF-RLY01", "quantity": 500, "customer_tier": "platinum"}))
        line = {"sku": "SAF-RLY01", "name": "x", "category": "Safety", "quantity": 500, "unit_price": 178,
                "discount_pct": 30, "net_total": 1.0, "stock_qty": 140, "available": False, "shortfall": 360}
        return m.final(info, {"customer_id": "C-10057", "tier": "platinum", "lines": [line]})

    order = {"customer": {"customer_id": "C-10057", "name": "T", "tier": "platinum"},
             "line_items": [{"description": "safety relay", "quantity": 500}]}
    task = "Price this.\n\n### DATA (order)\n" + json.dumps(order)
    with inv.agent.override(model=FunctionModel(lying)):
        out = asyncio.run(inv.agent.run(message_history=_history(task))).output
    assert out.lines[0].net_total == 75650.0 and out.lines[0].discount_pct == 15.0 and out.order_net_total == 75650.0


def test_inventory_rejects_shortfall_quantity_instead_of_order_quantity(monkeypatch):
    """LLM prices only the shortfall (360) instead of the ordered 500 -> retry with 500."""
    import asyncio, json
    from pydantic_ai.models.function import FunctionModel
    from agents import mock_support as m
    import agents.inventory_agent as inv
    from mcp_inventory.discount_engine import compute_tiered_discount

    async def fake_mcp(tool, args):
        if tool == "calculate_tiered_discount":
            r = compute_tiered_discount(args["quantity"], 178.0, args["customer_tier"], "Safety")
            r["stock_check"] = {"sku": args["sku"], "name": "Safety Relay Cat. 4", "stock_qty": 140,
                                "available": False, "shortfall": max(0, args["quantity"] - 140)}
            return r
        return {"status": "ok"}
    monkeypatch.setattr(inv, "_mcp", fake_mcp)

    def shortfall_only(messages, info):
        qty = 500 if m.retried(messages) else 360
        if len(m.tool_returns(messages)) < (2 if m.retried(messages) else 1):
            return m.call(("price_line_item", {"sku": "SAF-RLY01", "quantity": qty, "customer_tier": "platinum"}))
        line = {"sku": "SAF-RLY01", "name": "x", "category": "Safety", "quantity": qty, "unit_price": 178,
                "discount_pct": 15, "net_total": 0, "stock_qty": 140, "available": False, "shortfall": 0}
        return m.final(info, {"customer_id": "C-10057", "tier": "platinum", "lines": [line]})

    order = {"customer": {"customer_id": "C-10057", "name": "T", "tier": "platinum"},
             "line_items": [{"description": "safety relay", "quantity": 500}]}
    with inv.agent.override(model=FunctionModel(shortfall_only)):
        out = asyncio.run(inv.agent.run(message_history=_history("Price.\n\n### DATA\n" + json.dumps(order)))).output
    assert out.lines[0].quantity == 500 and out.order_net_total == 75650.0 and out.lines[0].shortfall == 360


def test_orchestrator_blocks_second_pricing_call(monkeypatch):
    """A completed step must not be re-run (the re-run could overwrite the valid result with wrong quantities)."""
    import asyncio, json
    from types import SimpleNamespace
    from pydantic_ai.messages import ModelRequest, ToolReturnPart
    import agents.orchestrator_agent as orch

    line = {"sku": "SAF-RLY01", "name": "Relay", "category": "Safety", "quantity": 500, "unit_price": 178,
            "discount_pct": 15, "net_total": 75650, "stock_qty": 140, "available": False, "shortfall": 360}
    pricing = {"customer_id": "C-1", "tier": "platinum", "lines": [line]}
    history = [ModelRequest(parts=[ToolReturnPart(tool_name="ask_agent", tool_call_id="t1", content={
        "agent": "inventory-pricing-agent", "skill": "stock-and-pricing", "valid": True, "response": json.dumps(pricing)})])]

    async def fake_get_agent(name):
        return {"name": name, "url": "http://127.0.0.1:1", "skills": [{"id": "stock-and-pricing"}]}

    async def must_not_send(*a, **k):
        raise AssertionError("pricing agent must not be called a second time")
    monkeypatch.setattr(orch.registry_client, "get_agent", fake_get_agent)
    monkeypatch.setattr(orch, "send_message", must_not_send)

    out = asyncio.run(orch.ask_agent(SimpleNamespace(messages=history), "inventory-pricing-agent", "price 360 relays"))
    assert out["valid"] is False and "already completed" in out["error"]
