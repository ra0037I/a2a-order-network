"""Order Intake Agent  (A2A server, port 8101)

Turns an unstructured customer e-mail into a structured order:
customer master data (from a small CRM) + line items + requested delivery date.

Run:  python -m agents.intake_agent
"""
from __future__ import annotations

import json
import logging
import re

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from agents import mock_support as m
from agents.models import Customer, IntakeResult
from common.a2a_server import create_a2a_app, serve
from common.llm import build_model, model_settings
from common.logging_setup import setup_logging

NAME = "order-intake-agent"
log = setup_logging(NAME)

# --- mini CRM (customer master data) ---------------------------------------------------------
CRM = {
    "alpenwerk gmbh":         {"customer_id": "C-10023", "name": "Alpenwerk GmbH", "tier": "gold", "country": "AT", "payment_terms": "30 days net"},
    "tiroler anlagenbau ag":  {"customer_id": "C-10057", "name": "Tiroler Anlagenbau AG", "tier": "platinum", "country": "AT", "payment_terms": "45 days net"},
    "bergbahn service kg":    {"customer_id": "C-10102", "name": "Bergbahn Service KG", "tier": "standard", "country": "AT", "payment_terms": "14 days net"},
    "inntal robotics":        {"customer_id": "C-10140", "name": "Inntal Robotics", "tier": "silver", "country": "AT", "payment_terms": "30 days net"},
}

SYSTEM_PROMPT = """You are the Order Intake Agent of an industrial automation supplier.
Input: a raw customer e-mail. Your job:
1. Identify the customer COMPANY (e.g. the 'Company:' line or the company in the signature - not the
   person's name) and ALWAYS call lookup_customer - the pricing tier must come from the CRM,
   never from the e-mail text.
2. Extract every requested product as a line item with quantity and a short product description
   (keep technical details such as M8, 5.5 kW, 7", 5 m). If an article number (e.g. PLC-1200) is given, put it in sku_hint.
3. Extract the requested delivery date if mentioned (ISO format), else null.
Return the result as the structured IntakeResult (customer = the lookup_customer result)."""


def _mock(messages, info):
    prompt = m.user_prompt(messages)
    returns = m.tool_returns(messages)
    if not returns:
        company = re.search(r"(?im)^(?:company|firma)\s*:\s*(.+)$", prompt) or re.search(
            r"([A-Z][\w\s]+?(?:GmbH|AG|KG|Robotics))", prompt)
        return m.call(("lookup_customer", {"company_name": company.group(1).strip() if company else "unknown"}))
    customer = m.as_json(returns[0][1])
    items = [{"description": d.strip().rstrip("."), "quantity": int(q),
              "sku_hint": (re.search(r"\b[A-Z]{3}-[A-Z0-9]{3,5}\b", d) or [None])[0]}
             for q, d in re.findall(r"(?m)^\s*-\s*(\d+)\s*x\s+(.+)$", prompt)]
    date = re.search(r"\b(20\d\d-\d\d-\d\d)\b", prompt)
    return m.final(info, {"customer": customer, "line_items": items,
                          "requested_delivery": date.group(1) if date else None, "notes": "mock extraction"})


agent = Agent(build_model(_mock), system_prompt=SYSTEM_PROMPT, name=NAME, model_settings=model_settings(),
              output_type=IntakeResult, retries=2)


@agent.tool_plain
def lookup_customer(company_name: str) -> dict:
    """Look up a customer in the CRM by company name.

    Args:
        company_name: Company name as written in the e-mail, e.g. 'Alpenwerk GmbH'.
    Returns customer_id, official name, pricing tier (standard|silver|gold|platinum), country, payment terms.
    Unknown companies are returned as new prospects with tier 'standard'.
    """
    key = " ".join(company_name.lower().replace(",", " ").split())
    hit = CRM.get(key) or next((v for k, v in CRM.items() if k in key or key in k), None)
    result = hit or {"customer_id": None, "name": company_name, "tier": "standard", "country": None,
                     "payment_terms": "prepayment", "new_prospect": True}
    log.info("[TOOL] lookup_customer(%r) -> %s (%s)", company_name, result["customer_id"], result["tier"])
    return result



@agent.output_validator
def _customer_from_crm(ctx: RunContext, output: IntakeResult) -> IntakeResult:
    """Customer master data is taken 1:1 from the CRM tool result - the LLM must not retype it."""
    crm = None
    for msg in ctx.messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == "lookup_customer" and isinstance(part.content, dict):
                    crm = part.content
    if crm is None:
        raise ModelRetry("Call lookup_customer first - the customer data must come from the CRM.")
    if crm.get("new_prospect"):
        # the LLM may have looked up the sender's name - if a known customer company appears in the e-mail, use it
        mail = m.prompt_text(ctx).lower()
        known = next((v for k, v in CRM.items() if k in " ".join(mail.split())), None)
        if known:
            log.warning("[CRM] lookup for %r found nothing, but the e-mail names known customer %s - using CRM record",
                        crm.get("name"), known["customer_id"])
            crm = known
    output.customer = Customer.model_validate(crm)
    return output

app = create_a2a_app(
    agent, NAME,
    description="Extracts structured order data (customer, line items, quantities, delivery date) from "
                "unstructured customer e-mails and enriches it with CRM customer master data.",
    skills=[{
        "id": "order-extraction",
        "name": "Order extraction from e-mail",
        "description": "Parse a free-text customer e-mail or request into JSON line items and look up the customer tier in the CRM.",
        "tags": ["order intake", "extraction", "email parsing", "customer", "crm", "nlp"],
        "examples": ["Extract the ordered products and quantities from this e-mail: ..."],
        "input_modes": ["text/plain"],
        "output_modes": ["application/json"],
    }],
)

if __name__ == "__main__":
    serve(app, NAME)
