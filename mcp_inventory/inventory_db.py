"""SQLite product inventory used by the `query_inventory` MCP tool.

The database file is created and seeded automatically on first use, so the
project runs without any manual DB setup.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("INVENTORY_DB_PATH", BASE_DIR / "data" / "inventory.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku            TEXT PRIMARY KEY,
    name           TEXT    NOT NULL,
    category       TEXT    NOT NULL,
    unit_price     REAL    NOT NULL CHECK (unit_price >= 0),
    stock_qty      INTEGER NOT NULL CHECK (stock_qty >= 0),
    reorder_level  INTEGER NOT NULL DEFAULT 0,
    warehouse      TEXT    NOT NULL
);
"""

# Demo data: an industrial automation supplier
SEED_PRODUCTS: list[tuple] = [
    # sku,        name,                                   category,       price,   stock, reorder, warehouse
    ("PLC-1200",  "Compact PLC CPU 1214C",                "Controllers",   389.00,    42,   20, "Innsbruck"),
    ("PLC-1500",  "Advanced PLC CPU 1516-3 PN/DP",        "Controllers",  2450.00,     7,   10, "Innsbruck"),
    ("HMI-0700",  "7\" Touch Panel HMI",                  "HMI",           690.00,    18,   10, "Vienna"),
    ("HMI-1200",  "12\" Comfort Panel HMI",               "HMI",          1580.00,     4,    5, "Vienna"),
    ("SEN-IND08", "Inductive Proximity Sensor M8",        "Sensors",        24.90,  1250,  300, "Innsbruck"),
    ("SEN-PHO18", "Photoelectric Sensor M18",             "Sensors",        58.50,   640,  200, "Linz"),
    ("SEN-TMP01", "PT100 Temperature Transmitter",        "Sensors",       112.00,    95,  100, "Linz"),
    ("DRV-0075",  "Frequency Inverter 0.75 kW",           "Drives",        315.00,    60,   25, "Vienna"),
    ("DRV-0550",  "Frequency Inverter 5.5 kW",            "Drives",        980.00,    12,   15, "Vienna"),
    ("CAB-ETH05", "Industrial Ethernet Cable M12 5 m",    "Cables",         19.80,  3400,  500, "Innsbruck"),
    ("CAB-PWR10", "Power Cable 4G1.5 10 m",               "Cables",         27.40,  1800,  400, "Linz"),
    ("NET-SW08",  "Managed Industrial Switch 8-Port",     "Networking",    445.00,    33,   15, "Vienna"),
    ("NET-GW01",  "OPC UA / MQTT IoT Gateway",            "Networking",    720.00,     9,   10, "Innsbruck"),
    ("SAF-RLY01", "Safety Relay Cat. 4",                  "Safety",        178.00,   140,   50, "Linz"),
]

ALLOWED_SORT = {"sku", "name", "category", "unit_price", "stock_qty"}


def get_connection() -> sqlite3.Connection:
    """Open the DB, creating and seeding it if necessary."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?)", SEED_PRODUCTS)
        conn.commit()
    return conn


def query_products(
    sku: str | None = None,
    search: str | None = None,
    category: str | None = None,
    low_stock_only: bool = False,
    min_stock: int | None = None,
    sort_by: str = "sku",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Parameterised product lookup (no string-concatenated user input -> no SQL injection)."""
    clauses: list[str] = []
    params: list[Any] = []

    if sku:
        clauses.append("UPPER(sku) = UPPER(?)")
        params.append(sku.strip())
    if search:
        # every word must match name or SKU (simple plural handling: "relays" -> "relay")
        for word in search.split():
            w = word.strip(".,;:()\"'")
            lw = w.lower()
            if len(w) > 4 and lw.endswith(("ches", "shes", "xes", "sses")):
                w = w[:-2]
            elif len(w) > 3 and lw.endswith("s") and not lw.endswith("ss"):
                w = w[:-1]
            if not w:
                continue
            clauses.append("(name LIKE ? OR sku LIKE ?)")
            params += [f"%{w}%", f"%{w}%"]
    if category:
        clauses.append("LOWER(category) = LOWER(?)")
        params.append(category.strip())
    if low_stock_only:
        clauses.append("stock_qty <= reorder_level")
    if min_stock is not None:
        clauses.append("stock_qty >= ?")
        params.append(int(min_stock))

    sort_col = sort_by if sort_by in ALLOWED_SORT else "sku"  # whitelist, never interpolate raw input
    limit = max(1, min(int(limit), 100))

    sql = "SELECT * FROM products"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY {sort_col} LIMIT ?"
    params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    result = []
    for r in rows:
        item = dict(r)
        item["needs_reorder"] = item["stock_qty"] <= item["reorder_level"]
        item["stock_value_eur"] = round(item["stock_qty"] * item["unit_price"], 2)
        result.append(item)
    return result


def get_product(sku: str) -> dict[str, Any] | None:
    rows = query_products(sku=sku, limit=1)
    return rows[0] if rows else None


def list_categories() -> list[str]:
    with get_connection() as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT category FROM products ORDER BY category")]


def schema_description() -> str:
    cats = ", ".join(list_categories())
    return (
        "Table `products` (SQLite)\n"
        "  sku TEXT PK | name TEXT | category TEXT | unit_price REAL (EUR, net)\n"
        "  stock_qty INTEGER | reorder_level INTEGER | warehouse TEXT\n"
        f"Categories: {cats}\n"
        "Derived fields returned by query_inventory: needs_reorder, stock_value_eur"
    )


if __name__ == "__main__":
    for p in query_products():
        print(p)
