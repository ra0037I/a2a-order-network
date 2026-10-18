"""Structured outputs of the specialised agents.

Each sub-agent uses one of these models as its pydantic-ai `output_type`, so its
A2A result is always validated JSON (fasta2a sends it as a data part) instead of
free prose. The orchestrator parses the same models to check the workflow.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Tier = Literal["standard", "silver", "gold", "platinum"]


# ---------------- order-intake-agent ----------------
class Customer(BaseModel):
    customer_id: str | None = Field(description="CRM customer id, null for unknown prospects")
    name: str
    tier: Tier = Field(description="pricing tier from lookup_customer")
    country: str | None = None
    payment_terms: str | None = None
    new_prospect: bool = False


class LineItem(BaseModel):
    description: str = Field(description="short product description incl. technical details (M8, 5.5 kW, 7\")")
    quantity: int = Field(gt=0)
    sku_hint: str | None = Field(default=None, description="article number if the customer wrote one, e.g. PLC-1500")


class IntakeResult(BaseModel):
    customer: Customer
    line_items: list[LineItem] = Field(min_length=1)
    requested_delivery: str | None = Field(default=None, description="ISO date or null")
    notes: str = ""


# ---------------- inventory-pricing-agent ----------------
class PricedLine(BaseModel):
    sku: str
    name: str
    category: str
    quantity: int
    unit_price: float
    discount_pct: float = Field(description="final_discount_rate_pct from price_line_item")
    net_total: float
    stock_qty: int
    available: bool
    shortfall: int = Field(ge=0)


class PricingResult(BaseModel):
    customer_id: str | None
    tier: Tier
    lines: list[PricedLine]
    order_net_total: float = 0.0
    items_with_shortfall: list[str] = []
    not_found: list[str] = Field(default_factory=list, description="descriptions without matching product")

    @model_validator(mode="after")
    def _derive_totals(self) -> "PricingResult":
        # deterministic: never trust the LLM's arithmetic for totals / shortfall list
        self.order_net_total = round(sum(l.net_total for l in self.lines), 2)
        self.items_with_shortfall = [l.sku for l in self.lines if l.shortfall > 0]
        return self


# ---------------- production-planning-agent ----------------
class ProductionOrder(BaseModel):
    sku: str
    quantity: int
    line: str
    production_days: int
    earliest_completion: str
    earliest_ship_date: str


class ProductionResult(BaseModel):
    stock_ship_date: str
    production_orders: list[ProductionOrder]
    complete_order_ship_date: str
    partial_delivery_possible: bool
