"""The demo company's synthetic master data, shared by the ERPNext seed and the evals.

Everything here is made up and labelled so, except supplier names, which come from
data/seed/suppliers.csv (real organisation names from the City of Chicago ledger).
"""

from __future__ import annotations

import csv
from pathlib import Path

COMPANY, ABBR = "Lakeside Demo Utilities", "LDU"
SEED_FILE = Path(__file__).resolve().parents[2] / "data/seed/suppliers.csv"

# Synthetic: annual budgets per cost centre.
COST_CENTRES = {
    "Fleet Operations": 900_000, "Facilities": 600_000, "Water Operations": 750_000,
    "IT": 400_000, "Public Health Programs": 300_000,
}
# Synthetic: item catalogue with a reference unit price.
ITEMS = {
    "LAPTOP-14": ("14-inch business laptop", 1150), "MONITOR-27": ("27-inch monitor", 290),
    "TONER-K": ("Black toner cartridge", 88), "PAPER-A4": ("Copy paper, case", 42),
    "SALT-TON": ("Road salt, per ton", 78), "ASPHALT-TON": ("Hot-mix asphalt, per ton", 96),
    "DIESEL-GAL": ("Diesel, per gallon", 3.85), "TIRE-TRK": ("Truck tyre", 410),
    "PIPE-DI-8": ("8-inch ductile iron pipe, per ft", 54),
    "VALVE-GATE-8": ("8-inch gate valve", 1650),
    "CHLORINE-LB": ("Chlorine, per lb", 1.9), "PPE-KIT": ("PPE kit", 65),
    "SECURITY-HR": ("Security guard, per hour", 34), "CLEAN-HR": ("Janitorial, per hour", 29),
    "CONSULT-HR": ("Consulting, per hour", 185), "HVAC-SVC": ("HVAC service visit", 640),
    "GENERATOR-RENT": ("Generator rental, per day", 420),
    "LIFT-RENT": ("Boom lift rental, per day", 380),
    # No purchase history on purpose, so orders of any size pass the price check.
    "PROJECT-SVC": ("Fixed-price project services, per milestone", 10000),
}


def cc(name: str) -> str:
    """ERPNext's full cost-centre name, e.g. 'IT' -> 'IT - LDU'."""
    return f"{name} - {ABBR}"


def seed_suppliers(role: str | None = None) -> list[dict]:
    rows = list(csv.DictReader(SEED_FILE.open()))
    return [r for r in rows if role is None or r["role"] == role]
