"""Formula engine for tiered volume discounts.

Pricing rules (all percentages on the net unit price):

1. Graduated volume tiers (works like tax brackets: each unit is discounted
   at the rate of the band it falls into, not the whole order at the top rate):

        units    1 –   9  ->  0 %
        units   10 –  49  ->  5 %
        units   50 –  99  -> 10 %
        units  100 – 249  -> 15 %
        units  250 +      -> 20 %

   The result is a *blended* volume discount rate.

2. Customer loyalty bonus (percentage points added on top):
        standard +0 pp | silver +2 pp | gold +4 pp | platinum +6 pp

3. Order value bonus: +1.5 pp if the gross order value >= 10 000 EUR,
   +3 pp if >= 50 000 EUR.

4. Category caps - total discount may not exceed the category maximum
   (low-margin categories are capped lower):
        Controllers 18 % | HMI 20 % | Drives 22 % | Safety 15 % | default 30 %

Final:  discount_rate = min(blended + loyalty + value_bonus, cap)
        net_total     = gross_total * (1 - discount_rate)
"""
from __future__ import annotations

from typing import Any

VOLUME_TIERS: list[tuple[int, int | None, float]] = [
    # (from_unit, to_unit (inclusive, None = open end), rate)
    (1, 9, 0.00),
    (10, 49, 0.05),
    (50, 99, 0.10),
    (100, 249, 0.15),
    (250, None, 0.20),
]

LOYALTY_BONUS = {"standard": 0.00, "silver": 0.02, "gold": 0.04, "platinum": 0.06}

ORDER_VALUE_BONUS = [  # (min gross value, bonus) - highest matching wins
    (50_000.0, 0.03),
    (10_000.0, 0.015),
]

CATEGORY_CAPS = {"controllers": 0.18, "hmi": 0.20, "drives": 0.22, "safety": 0.15}
DEFAULT_CAP = 0.30


def _r(x: float, nd: int = 2) -> float:
    return round(x + 0.0, nd)


def compute_tiered_discount(
    quantity: int,
    unit_price: float,
    customer_tier: str = "standard",
    category: str | None = None,
) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    if unit_price < 0:
        raise ValueError("unit_price must be >= 0")
    tier_key = (customer_tier or "standard").strip().lower()
    if tier_key not in LOYALTY_BONUS:
        raise ValueError(f"unknown customer_tier '{customer_tier}'. Allowed: {', '.join(LOYALTY_BONUS)}")

    gross_total = quantity * unit_price

    # 1. graduated volume tiers
    breakdown = []
    volume_discount_amount = 0.0
    for start, end, rate in VOLUME_TIERS:
        if quantity < start:
            break
        upper = quantity if end is None else min(quantity, end)
        units = upper - start + 1
        band_gross = units * unit_price
        band_discount = band_gross * rate
        volume_discount_amount += band_discount
        breakdown.append({
            "band": f"{start}-{end}" if end is not None else f"{start}+",
            "units": units,
            "rate_pct": _r(rate * 100, 1),
            "band_gross": _r(band_gross),
            "band_discount": _r(band_discount),
        })
    blended_rate = volume_discount_amount / gross_total if gross_total else 0.0

    # 2. loyalty, 3. order value bonus
    loyalty = LOYALTY_BONUS[tier_key]
    value_bonus = next((b for threshold, b in ORDER_VALUE_BONUS if gross_total >= threshold), 0.0)

    # 4. category cap
    cap = CATEGORY_CAPS.get((category or "").strip().lower(), DEFAULT_CAP)
    uncapped = blended_rate + loyalty + value_bonus
    final_rate = min(uncapped, cap)

    discount_amount = gross_total * final_rate
    net_total = gross_total - discount_amount

    return {
        "quantity": quantity,
        "unit_price": _r(unit_price),
        "customer_tier": tier_key,
        "category": category,
        "gross_total": _r(gross_total),
        "volume_tiers": breakdown,
        "blended_volume_rate_pct": _r(blended_rate * 100, 3),
        "loyalty_bonus_pct": _r(loyalty * 100, 1),
        "order_value_bonus_pct": _r(value_bonus * 100, 1),
        "uncapped_rate_pct": _r(uncapped * 100, 3),
        "category_cap_pct": _r(cap * 100, 1),
        "cap_applied": uncapped > cap,
        "final_discount_rate_pct": _r(final_rate * 100, 3),
        "discount_amount": _r(discount_amount),
        "net_total": _r(net_total),
        "effective_unit_price": _r(net_total / quantity, 4),
        "currency": "EUR",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(compute_tiered_discount(300, 24.90, "gold", "Sensors"), indent=2, ensure_ascii=False))
