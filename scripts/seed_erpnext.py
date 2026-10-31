"""Set up the local ERPNext for po-guard: company, custom fields, audit doctype, demo data.

Runs as the local Administrator (password `admin`, set by docker/compose.yml; local
development only) and deliberately bypasses po-guard: this is the ERP's starting state,
not something the MCP does. Idempotent: safe to re-run.

What it loads:
- Company "Lakeside Demo Utilities" (made up), fiscal year 2026, USD.
- Supplier master: the `master` rows of data/seed/suppliers.csv, real organisation names
  from the City of Chicago vendor-payments ledger. Tax IDs are synthetic (00- prefix).
- Synthetic, and labelled so: cost centres and budgets, an item catalogue with
  reference prices, and approved historical orders that set last purchase prices and
  part-consume budgets.
- Users (local test accounts, @example.com) with API keys, written to .env.
"""

from __future__ import annotations

import csv
import json
import os
import random
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")
COMPANY, ABBR = "Lakeside Demo Utilities", "LDU"
SUPPLIER_GROUP = "Demo Suppliers"
ITEM_GROUP = "Demo Purchases"
AUDIT = "PO Guard Audit"
TODAY = date.today()

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
    "PIPE-DI-8": ("8-inch ductile iron pipe, per ft", 54), "VALVE-GATE-8": ("8-inch gate valve", 1650),
    "CHLORINE-LB": ("Chlorine, per lb", 1.9), "PPE-KIT": ("PPE kit", 65),
    "SECURITY-HR": ("Security guard, per hour", 34), "CLEAN-HR": ("Janitorial, per hour", 29),
    "CONSULT-HR": ("Consulting, per hour", 185), "HVAC-SVC": ("HVAC service visit", 640),
    "GENERATOR-RENT": ("Generator rental, per day", 420), "LIFT-RENT": ("Boom lift rental, per day", 380),
}
USERS = {
    "requester@example.com": ("Riley", "Requester", ["Purchase User"]),
    "requester2@example.com": ("Sam", "Second-Requester", ["Purchase User"]),
    "manager@example.com": ("Morgan", "Manager", ["Purchase User", "Purchase Manager"]),
    "director@example.com": ("Dana", "Director", ["Purchase User", "Purchase Manager"]),
}
USER_ROLES_EXTRA = ["Accounts User", "Stock User"]  # read access to cost centres and items

PO_FIELDS = [
    {"fieldname": "po_guard_section", "fieldtype": "Section Break", "label": "po-guard",
     "insert_after": "items"},
    {"fieldname": "po_guard_status", "fieldtype": "Select", "label": "po-guard status",
     "options": "Draft\nPending Approval\nApproved\nRejected\nCancelled", "default": "Draft",
     "allow_on_submit": 1, "in_list_view": 1, "insert_after": "po_guard_section"},
    {"fieldname": "po_guard_cost_centre", "fieldtype": "Link", "options": "Cost Center",
     "label": "Cost centre (po-guard)", "insert_after": "po_guard_status"},
    {"fieldname": "po_guard_approver", "fieldtype": "Data", "label": "Approved by",
     "allow_on_submit": 1, "read_only": 1, "insert_after": "po_guard_cost_centre"},
    {"fieldname": "po_guard_reason", "fieldtype": "Small Text", "label": "Reject/cancel reason",
     "allow_on_submit": 1, "insert_after": "po_guard_approver"},
    {"fieldname": "po_guard_notes", "fieldtype": "Small Text", "label": "Notes",
     "insert_after": "po_guard_reason"},
]
SUPPLIER_FIELDS = [
    {"fieldname": "po_guard_onboarded_by", "fieldtype": "Data", "label": "Onboarded by",
     "read_only": 1, "insert_after": "supplier_group"},
    {"fieldname": "po_guard_notes", "fieldtype": "Small Text", "label": "Notes",
     "insert_after": "po_guard_onboarded_by"},
]
CC_FIELDS = [
    {"fieldname": "po_guard_annual_budget", "fieldtype": "Currency", "label": "Annual budget",
     "insert_after": "cost_center_name"},
]


class Admin:
    def __init__(self):
        self.h = httpx.Client(base_url=URL, timeout=120)
        r = self.h.post("/api/method/login", json={"usr": "Administrator", "pwd": "admin"})
        r.raise_for_status()

    def call(self, verb, path, **kw):
        r = self.h.request(verb, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{verb} {path} -> {r.status_code}: {r.text[:800]}")
        return r.json()

    def exists(self, doctype, name) -> bool:
        return self.h.get(f"/api/resource/{doctype}/{httpx.URL(name)}").status_code == 200

    def get_list(self, doctype, fields, filters=None):
        params = {"fields": json.dumps(fields), "limit_page_length": "0"}
        if filters:
            params["filters"] = json.dumps(filters)
        return self.call("GET", f"/api/resource/{doctype}", params=params)["data"]

    def insert(self, doc: dict) -> dict:
        return self.call("POST", f"/api/resource/{doc['doctype']}", json=doc)["data"]

    def ensure(self, doc: dict, name: str) -> None:
        if not self.exists(doc["doctype"], name):
            self.insert(doc)


def setup_wizard(a: Admin) -> None:
    if a.get_list("Company", ["name"]):
        return
    print("completing setup wizard (a minute or two)...")
    args = {"language": "English", "country": "United States", "timezone": "America/Chicago",
            "currency": "USD", "full_name": "Administrator", "company_name": COMPANY,
            "company_abbr": ABBR, "chart_of_accounts": "Standard",
            "fy_start_date": f"{TODAY.year}-01-01", "fy_end_date": f"{TODAY.year}-12-31",
            "setup_demo": 0}
    a.call("POST", "/api/method/frappe.desk.page.setup_wizard.setup_wizard.setup_complete",
           json={"args": args})


def custom_fields(a: Admin) -> None:
    for dt, fields in (("Purchase Order", PO_FIELDS), ("Supplier", SUPPLIER_FIELDS),
                       ("Cost Center", CC_FIELDS)):
        for f in fields:
            a.ensure({"doctype": "Custom Field", "dt": dt, **f}, f"{dt}-{f['fieldname']}")


def audit_doctype(a: Admin) -> None:
    if a.exists("DocType", AUDIT):
        return
    perm = {"read": 1, "create": 1, "write": 0, "delete": 0, "report": 1, "export": 1}
    a.insert({
        "doctype": "DocType", "name": AUDIT, "module": "Buying", "custom": 1,
        "autoname": "format:AUD-{#####}", "track_changes": 0, "sort_field": "creation",
        "sort_order": "DESC", "title_field": "action",
        "description": "Append-only trail of every po-guard write attempt.",
        "fields": [
            {"fieldname": "action", "fieldtype": "Data", "label": "Action", "in_list_view": 1,
             "reqd": 1},
            {"fieldname": "actor", "fieldtype": "Data", "label": "Actor", "in_list_view": 1},
            {"fieldname": "outcome", "fieldtype": "Select", "label": "Outcome",
             "options": "written\nblocked\nrefused\nerror", "in_list_view": 1},
            {"fieldname": "target", "fieldtype": "Data", "label": "Target", "in_list_view": 1},
            {"fieldname": "payload_hash", "fieldtype": "Data", "label": "Payload SHA-256"},
            {"fieldname": "detail", "fieldtype": "Small Text", "label": "Detail"},
            {"fieldname": "checks", "fieldtype": "Long Text", "label": "Check results (JSON)"},
        ],
        "permissions": [
            {"role": "Purchase User", **perm}, {"role": "Purchase Manager", **perm},
            {"role": "System Manager", **perm},
        ],
    })


def masters(a: Admin) -> None:
    root = f"{COMPANY} - {ABBR}"
    for cc, budget in COST_CENTRES.items():
        name = f"{cc} - {ABBR}"
        if not a.exists("Cost Center", name):
            a.insert({"doctype": "Cost Center", "cost_center_name": cc, "company": COMPANY,
                      "parent_cost_center": root, "is_group": 0,
                      "po_guard_annual_budget": budget})
    a.ensure({"doctype": "Supplier Group", "supplier_group_name": SUPPLIER_GROUP,
              "parent_supplier_group": "All Supplier Groups"}, SUPPLIER_GROUP)
    a.ensure({"doctype": "Item Group", "item_group_name": ITEM_GROUP,
              "parent_item_group": "All Item Groups"}, ITEM_GROUP)
    for code, (desc, _) in ITEMS.items():
        a.ensure({"doctype": "Item", "item_code": code, "item_name": desc, "description": desc,
                  "item_group": ITEM_GROUP, "stock_uom": "Nos", "is_stock_item": 0,
                  "is_purchase_item": 1, "include_item_in_manufacturing": 0}, code)

    have = {s["supplier_name"] for s in a.get_list("Supplier", ["supplier_name"])}
    rows = [r for r in csv.DictReader((ROOT / "data/seed/suppliers.csv").open())
            if r["role"] == "master"]
    for r in rows:
        if r["name"] in have:
            continue
        a.insert({"doctype": "Supplier", "supplier_name": r["name"],
                  "supplier_group": SUPPLIER_GROUP, "supplier_type": "Company",
                  "tax_id": r["synthetic_tax_id"],
                  "po_guard_notes": "Seeded from the City of Chicago vendor-payments ledger."})
    print(f"suppliers: {len(rows)} in seed file")


def _works(kv: tuple[str, str]) -> bool:
    r = _client(kv).get("/api/method/frappe.auth.get_logged_user")
    return r.status_code == 200


def _restart_backend() -> None:
    # On a brand-new site the gunicorn workers can each cache a different encryption key
    # before the first one is written to site_config.json, so a freshly generated API
    # secret fails to decrypt. A restart makes them all read the saved key.
    print("restarting the ERPNext backend once (fresh-site encryption key)...")
    subprocess.run(["docker", "compose", "-f", str(ROOT / "docker/compose.yml"), "restart",
                    "backend"], check=True, capture_output=True)
    for _ in range(60):
        try:
            if httpx.get(f"{URL}/api/method/ping", timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError("ERPNext backend didn't come back")


def users(a: Admin, retry: bool = True) -> dict[str, tuple[str, str]]:
    keys = {}
    for email, (first, last, roles) in USERS.items():
        if not a.exists("User", email):
            a.insert({"doctype": "User", "email": email, "first_name": first, "last_name": last,
                      "send_welcome_email": 0, "user_type": "System User",
                      "roles": [{"role": r} for r in roles + USER_ROLES_EXTRA]})
        secret = a.call("POST", "/api/method/frappe.core.doctype.user.user.generate_keys",
                        json={"user": email})["message"]["api_secret"]
        key = a.call("GET", f"/api/resource/User/{email}")["data"]["api_key"]
        keys[email] = (key, secret)
    if not all(_works(kv) for kv in keys.values()):
        if not retry:
            raise RuntimeError("API keys don't authenticate even after a backend restart")
        _restart_backend()
        return users(Admin(), retry=False)
    return keys


def history(keys: dict[str, tuple[str, str]]) -> None:
    """Approved orders from the last few months: last prices and budget consumption."""
    req = _client(keys["requester2@example.com"])
    appr = _client(keys["director@example.com"])
    existing = req.get("/api/resource/Purchase Order",
                       params={"limit_page_length": "1"}).json()["data"]
    if existing:
        return
    rng = random.Random(7)
    suppliers = [s["name"] for s in req.get(
        "/api/resource/Supplier", params={"limit_page_length": "0"}).json()["data"]]
    plan = [  # (cost centre, items) for each historical order
        ("IT", ["LAPTOP-14", "MONITOR-27"]), ("IT", ["TONER-K", "PAPER-A4"]),
        ("Fleet Operations", ["DIESEL-GAL"]), ("Fleet Operations", ["TIRE-TRK"]),
        ("Water Operations", ["PIPE-DI-8", "VALVE-GATE-8"]), ("Water Operations", ["CHLORINE-LB"]),
        ("Facilities", ["SECURITY-HR"]), ("Facilities", ["CLEAN-HR", "HVAC-SVC"]),
        ("Facilities", ["GENERATOR-RENT", "LIFT-RENT"]), ("Fleet Operations", ["SALT-TON"]),
        ("Fleet Operations", ["ASPHALT-TON"]), ("Public Health Programs", ["PPE-KIT"]),
        ("Public Health Programs", ["CONSULT-HR"]),
    ]
    for n, (cc, codes) in enumerate(plan):
        when = TODAY - timedelta(days=150 - n * 9)
        qty = {"DIESEL-GAL": 8000, "SALT-TON": 900, "ASPHALT-TON": 600, "CHLORINE-LB": 6000,
               "SECURITY-HR": 2000, "CLEAN-HR": 1500, "PIPE-DI-8": 900, "CONSULT-HR": 120}
        items = [{"item_code": c, "qty": qty.get(c, rng.randint(5, 40)),
                  "rate": round(ITEMS[c][1] * rng.uniform(0.95, 1.05), 2),
                  "schedule_date": (when + timedelta(days=14)).isoformat(),
                  "cost_center": f"{cc} - {ABBR}"} for c in codes]
        r = req.post("/api/resource/Purchase Order", json={
            "supplier": rng.choice(suppliers), "company": COMPANY,
            "transaction_date": when.isoformat(),
            "schedule_date": (when + timedelta(days=14)).isoformat(),
            "po_guard_cost_centre": f"{cc} - {ABBR}", "po_guard_status": "Pending Approval",
            "po_guard_notes": "Seed history (synthetic).", "items": items})
        r.raise_for_status()
        name = r.json()["data"]["name"]
        s = appr.put(f"/api/resource/Purchase Order/{name}", json={
            "docstatus": 1, "po_guard_status": "Approved",
            "po_guard_approver": "director@example.com"})
        if s.status_code >= 400:
            raise RuntimeError(f"submitting {name}: {s.text[:800]}")
    print(f"history: {len(plan)} approved orders")


def _client(kv: tuple[str, str]) -> httpx.Client:
    return httpx.Client(base_url=URL, timeout=60,
                        headers={"Authorization": f"token {kv[0]}:{kv[1]}"})


def write_env(keys: dict[str, tuple[str, str]]) -> None:
    path = ROOT / ".env"
    keep = []
    if path.exists():
        keep = [ln for ln in path.read_text().splitlines()
                if not ln.startswith(("ERPNEXT_", "# ERPNext"))]
    out = ["# ERPNext (written by scripts/seed_erpnext.py; local test accounts only)",
           f"ERPNEXT_URL={URL}", f"ERPNEXT_COMPANY='{COMPANY}'",
           f"ERPNEXT_SUPPLIER_GROUP='{SUPPLIER_GROUP}'"]
    for email, (k, s) in keys.items():
        tag = email.split("@")[0].upper()
        out += [f"ERPNEXT_KEY_{tag}={k}", f"ERPNEXT_SECRET_{tag}={s}"]
    k, s = keys["requester@example.com"]
    out += [f"ERPNEXT_API_KEY={k}", f"ERPNEXT_API_SECRET={s}"]
    path.write_text("\n".join(keep + out) + "\n")
    path.chmod(0o600)


def main() -> None:
    a = Admin()
    setup_wizard(a)
    custom_fields(a)
    audit_doctype(a)
    masters(a)
    keys = users(a)
    write_env(keys)
    history(keys)
    print("ERPNext seeded; API keys in .env")


if __name__ == "__main__":
    sys.exit(main())
