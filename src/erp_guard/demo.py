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
# Goods are stock items, received into one warehouse (three-way match); the rest are
# services, non-stock (two-way match).
STOCK_ITEMS = {
    "LAPTOP-14", "MONITOR-27", "TONER-K", "PAPER-A4", "SALT-TON", "ASPHALT-TON",
    "DIESEL-GAL", "TIRE-TRK", "PIPE-DI-8", "VALVE-GATE-8", "CHLORINE-LB", "PPE-KIT",
}
WAREHOUSE = f"Stores - {ABBR}"
# The company bank: a ledger account (no Bank Account record), the one payments may use.
BANK_ACCOUNT = f"Demo Operating Bank - {ABBR}"
MODE_OF_PAYMENT = "Bank Transfer"

_REQUESTER = ("Purchase User", "Purchase Master Manager",
              # Task is a later-phase type the generic tools write (live generic test).
              "Projects User")
_APPROVER = ("Purchase User", "Purchase Master Manager", "Purchase Manager",
             # ERPNext submits a document as the user who applies the workflow transition,
             # and submitting a Payment Entry needs an Accounts role.
             "Accounts User")
# Local test accounts: email -> (tag for the ERPNEXT_KEY_<tag> keys in .env, ERPNext
# roles). Who may approve what is decided by config/policy.toml, not by these roles. The
# seed adds a few read permissions these roles lack in ERPNext (see READ_GRANTS there).
USERS: dict[str, tuple[str, list[str]]] = {
    "requester@example.com": ("REQUESTER", list(_REQUESTER)),
    "requester2@example.com": ("REQUESTER2", list(_REQUESTER)),
    "manager@example.com": ("MANAGER", list(_APPROVER)),
    "director@example.com": ("DIRECTOR", list(_APPROVER)),
    "receiver@example.com": ("RECEIVER", ["Stock User"]),
    "ap@example.com": ("AP", ["Accounts User"]),
    "controller@example.com": ("CONTROLLER", ["Accounts Manager"]),
    "treasury@example.com": ("TREASURY", ["Accounts User"]),
}


def cc(name: str) -> str:
    """ERPNext's full cost-centre name, e.g. 'IT' -> 'IT - LDU'."""
    return f"{name} - {ABBR}"


def seed_suppliers(role: str | None = None) -> list[dict]:
    rows = list(csv.DictReader(SEED_FILE.open()))
    return [r for r in rows if role is None or r["role"] == role]
