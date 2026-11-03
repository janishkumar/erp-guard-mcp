"""Set up the local ERPNext for erp-guard: company, custom fields, audit doctype, demo data.

Runs as the local Administrator (password `admin`, set by docker/compose.yml; local
development only) and deliberately bypasses erp-guard: this is the ERP's starting state,
not something the MCP does. Idempotent: safe to re-run.

What it loads:
- Company "Lakeside Demo Utilities" (made up), fiscal year 2026, USD.
- Supplier master: the `master` rows of data/seed/suppliers.csv, real organisation names
  from the City of Chicago vendor-payments ledger. Tax IDs are synthetic (00- prefix).
- Synthetic, and labelled so: cost centres and budgets, an item catalogue with
  reference prices (goods are stock items into one warehouse, services non-stock), a
  company bank ledger account with its mode of payment, and history: approved orders that
  set last purchase prices and part-consume budgets, some of them received, invoiced and
  paid.
- Users (local test accounts, @example.com) with API keys, written to .env.
- Approval workflows (all active) for material requests, purchase orders, purchase
  invoices, payments and erp-guard change requests. A re-run brings existing workflows up
  to the definitions here.

ERPNext's Buying, Stock and Accounts Settings are left at their defaults.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from erp_guard.demo import (  # noqa: E402
    ABBR,
    BANK_ACCOUNT,
    COMPANY,
    COST_CENTRES,
    ITEMS,
    MODE_OF_PAYMENT,
    STOCK_ITEMS,
    USERS,
    WAREHOUSE,
    seed_suppliers,
)

ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")
SUPPLIER_GROUP = "Demo Suppliers"
ITEM_GROUP = "Demo Purchases"
AUDIT = "ERP Guard Audit"
CHANGE_REQUEST = "ERP Guard Change Request"
HISTORY_NOTE = "Seed history (synthetic)."
TODAY = date.today()

# Demo users' names; their roles and .env tags are in erp_guard.demo.USERS.
NAMES = {
    "requester@example.com": ("Riley", "Requester"),
    "requester2@example.com": ("Sam", "Second-Requester"),
    "manager@example.com": ("Morgan", "Manager"),
    "director@example.com": ("Dana", "Director"),
    "receiver@example.com": ("Robin", "Receiver"),
    "ap@example.com": ("Alex", "Payables"),
    "controller@example.com": ("Casey", "Controller"),
    "treasury@example.com": ("Taylor", "Treasury"),
}
# Roles that do business through erp-guard, so must be able to write its audit trail.
BUSINESS_ROLES = ["Purchase User", "Purchase Manager", "Stock User", "Accounts User",
                  "Accounts Manager", "System Manager"]

PO_FIELDS = [
    {"fieldname": "erp_guard_section", "fieldtype": "Section Break", "label": "erp-guard",
     "insert_after": "items"},
    {"fieldname": "erp_guard_cost_centre", "fieldtype": "Link", "options": "Cost Center",
     "label": "Cost centre (erp-guard)", "insert_after": "erp_guard_section"},
    {"fieldname": "erp_guard_approver", "fieldtype": "Data", "label": "Approved by",
     "allow_on_submit": 1, "read_only": 1, "insert_after": "erp_guard_cost_centre"},
    {"fieldname": "erp_guard_reason", "fieldtype": "Small Text", "label": "Reject/cancel reason",
     "allow_on_submit": 1, "insert_after": "erp_guard_approver"},
    {"fieldname": "erp_guard_notes", "fieldtype": "Small Text", "label": "Notes",
     "insert_after": "erp_guard_reason"},
    # A draft order cancelled through erp-guard: drafts can't be cancelled in ERPNext, and
    # erp-guard never deletes, so the draft stays with this flag (no workflow action is
    # offered on it any more).
    {"fieldname": "erp_guard_cancelled", "fieldtype": "Check", "label": "Cancelled (draft)",
     "default": "0", "read_only": 1, "in_list_view": 1, "insert_after": "erp_guard_notes"},
]
SUPPLIER_FIELDS = [
    {"fieldname": "erp_guard_onboarded_by", "fieldtype": "Data", "label": "Onboarded by",
     "read_only": 1, "insert_after": "supplier_group"},
    {"fieldname": "erp_guard_notes", "fieldtype": "Small Text", "label": "Notes",
     "insert_after": "erp_guard_onboarded_by"},
]
# Who approved an invoice or payment: set by the workflow when it enters Approved (ON_ENTER
# below), as on the purchase order, so the approver is ERPNext's own record.
APPROVER_FIELD = {"fieldname": "erp_guard_approver", "fieldtype": "Data", "label": "Approved by",
                  "allow_on_submit": 1, "read_only": 1}
PI_FIELDS = [{**APPROVER_FIELD, "insert_after": "bill_date"}]
PE_FIELDS = [{**APPROVER_FIELD, "insert_after": "reference_date"}]
CC_FIELDS = [
    {"fieldname": "erp_guard_annual_budget", "fieldtype": "Currency", "label": "Annual budget",
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


def setup_wizard(a: Admin) -> bool:
    """Complete the wizard if needed; True if this run did it (a brand-new site)."""
    if a.get_list("Company", ["name"]):
        return False
    print("completing setup wizard (a minute or two)...")
    args = {"language": "English", "country": "United States", "timezone": "America/Chicago",
            "currency": "USD", "full_name": "Administrator", "company_name": COMPANY,
            "company_abbr": ABBR, "chart_of_accounts": "Standard",
            "fy_start_date": f"{TODAY.year}-01-01", "fy_end_date": f"{TODAY.year}-12-31",
            "setup_demo": 0}
    a.call("POST", "/api/method/frappe.desk.page.setup_wizard.setup_wizard.setup_complete",
           json={"args": args})
    return True


def custom_fields(a: Admin) -> None:
    for dt, fields in (("Purchase Order", PO_FIELDS), ("Supplier", SUPPLIER_FIELDS),
                       ("Cost Center", CC_FIELDS), ("Purchase Invoice", PI_FIELDS),
                       ("Payment Entry", PE_FIELDS)):
        for f in fields:
            a.ensure({"doctype": "Custom Field", "dt": dt, **f}, f"{dt}-{f['fieldname']}")


def _perms(grants: dict[str, tuple[str, ...]]) -> list[dict]:
    flags = ("read", "create", "write", "submit", "cancel", "amend", "delete")
    return [{"role": role, **{f: int(f in given) for f in flags}, "report": 1, "export": 1}
            for role, given in grants.items()]


def _custom_doctype(a: Admin, doc: dict) -> None:
    """Create an erp-guard DocType, or bring an existing one's permissions up to date."""
    if not a.exists("DocType", doc["name"]):
        a.insert({"doctype": "DocType", "module": "Buying", "custom": 1, **doc})
        return
    have = a.call("GET", f"/api/resource/DocType/{doc['name']}")["data"]["permissions"]
    flags = ("role", "read", "create", "write", "submit", "cancel", "amend", "delete")
    if ({tuple(p.get(f) for f in flags) for p in have}
            != {tuple(p.get(f) for f in flags) for p in doc["permissions"]}):
        a.call("PUT", f"/api/resource/DocType/{doc['name']}",
               json={"permissions": doc["permissions"]})


def audit_doctype(a: Admin) -> None:
    _custom_doctype(a, {
        "name": AUDIT, "autoname": "format:AUD-{#####}", "track_changes": 0,
        "sort_field": "creation", "sort_order": "DESC", "title_field": "action",
        "description": "Append-only trail of every erp-guard write attempt.",
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
        # Create and read only: the trail is append-only.
        "permissions": _perms({r: ("read", "create") for r in BUSINESS_ROLES}),
    })


def change_request_doctype(a: Admin) -> None:
    """A requested change to master data that a second person approves (workflow below).

    Business roles create and read change requests and can't delete them. The workflow
    needs a little more, because ERPNext saves a document as the user who applies a
    transition and submits it on approval: the submitting role (Purchase User) also writes,
    and the approving role (Purchase Manager) writes and submits.
    """
    grants = {r: ("read", "create") for r in BUSINESS_ROLES}
    grants["Purchase User"] = ("read", "create", "write")
    grants["Purchase Manager"] = ("read", "create", "write", "submit")
    _custom_doctype(a, {
        "name": CHANGE_REQUEST, "autoname": "format:ECR-{#####}", "is_submittable": 1,
        "track_changes": 1, "sort_field": "creation", "sort_order": "DESC",
        "title_field": "target_name",
        "description": "A change to master data that needs a second person (erp-guard).",
        "fields": [
            {"fieldname": "target_doctype", "fieldtype": "Link", "options": "DocType",
             "label": "Document type", "reqd": 1, "in_list_view": 1},
            {"fieldname": "target_name", "fieldtype": "Dynamic Link",
             "options": "target_doctype", "label": "Document", "reqd": 1, "in_list_view": 1},
            {"fieldname": "fields_json", "fieldtype": "Long Text", "reqd": 1,
             "label": "Requested field values (JSON)"},
            {"fieldname": "requested_by", "fieldtype": "Data", "label": "Requested by",
             "in_list_view": 1},
            {"fieldname": "status", "fieldtype": "Select", "label": "Status",
             "options": "Pending\nApplied\nRejected", "default": "Pending",
             "allow_on_submit": 1, "in_list_view": 1},
            {"fieldname": "decided_by", "fieldtype": "Data", "label": "Decided by",
             "allow_on_submit": 1},
            {"fieldname": "reason", "fieldtype": "Small Text", "label": "Reason",
             "allow_on_submit": 1},
        ],
        "permissions": _perms(grants),
    })


def masters(a: Admin) -> None:
    root = f"{COMPANY} - {ABBR}"
    for cc, budget in COST_CENTRES.items():
        name = f"{cc} - {ABBR}"
        if not a.exists("Cost Center", name):
            a.insert({"doctype": "Cost Center", "cost_center_name": cc, "company": COMPANY,
                      "parent_cost_center": root, "is_group": 0,
                      "erp_guard_annual_budget": budget})
    a.ensure({"doctype": "Supplier Group", "supplier_group_name": SUPPLIER_GROUP,
              "parent_supplier_group": "All Supplier Groups"}, SUPPLIER_GROUP)
    a.ensure({"doctype": "Item Group", "item_group_name": ITEM_GROUP,
              "parent_item_group": "All Item Groups"}, ITEM_GROUP)
    items(a)
    bank(a)

    have = {s["supplier_name"] for s in a.get_list("Supplier", ["supplier_name"])}
    rows = seed_suppliers("master")
    for r in rows:
        if r["name"] in have:
            continue
        a.insert({"doctype": "Supplier", "supplier_name": r["name"],
                  "supplier_group": SUPPLIER_GROUP, "supplier_type": "Company",
                  "tax_id": r["synthetic_tax_id"],
                  "erp_guard_notes": "Seeded from the City of Chicago vendor-payments ledger."})
    print(f"suppliers: {len(rows)} in seed file")


def items(a: Admin) -> None:
    """Goods are stock items with one default warehouse; services are non-stock."""
    if not a.exists("Warehouse", WAREHOUSE):
        raise RuntimeError(f"{WAREHOUSE} missing (the setup wizard creates it)")
    for code, (desc, _) in ITEMS.items():
        stock = code in STOCK_ITEMS
        flags = {"is_stock_item": int(stock), "item_defaults": (
            [{"company": COMPANY, "default_warehouse": WAREHOUSE}] if stock else [])}
        if not a.exists("Item", code):
            a.insert({"doctype": "Item", "item_code": code, "item_name": desc,
                      "description": desc, "item_group": ITEM_GROUP, "stock_uom": "Nos",
                      "is_purchase_item": 1, "include_item_in_manufacturing": 0, **flags})
            continue
        doc = a.call("GET", f"/api/resource/Item/{code}")["data"]
        if doc["is_stock_item"] != flags["is_stock_item"]:
            # ERPNext won't change it once the item has submitted transactions.
            raise RuntimeError(f"Item {code} has the wrong stock flag (an older seed?); "
                               "start from a fresh ERPNext: make reset up seed")
        have = [(d["company"], d.get("default_warehouse")) for d in doc["item_defaults"]]
        want = [(d["company"], d["default_warehouse"]) for d in flags["item_defaults"]]
        if have != want:
            a.call("PUT", f"/api/resource/Item/{code}", json=flags)


def bank(a: Admin) -> None:
    """The company bank as a ledger account (no Bank Account record), and the mode of
    payment treasury uses, defaulting to it."""
    a.ensure({"doctype": "Account", "account_name": BANK_ACCOUNT.removesuffix(f" - {ABBR}"),
              "parent_account": f"Bank Accounts - {ABBR}", "company": COMPANY,
              "account_type": "Bank", "is_group": 0, "root_type": "Asset"}, BANK_ACCOUNT)
    row = {"company": COMPANY, "default_account": BANK_ACCOUNT}
    if not a.exists("Mode of Payment", MODE_OF_PAYMENT):
        a.insert({"doctype": "Mode of Payment", "mode_of_payment": MODE_OF_PAYMENT,
                  "type": "Bank", "enabled": 1, "accounts": [row]})
        return
    doc = a.call("GET", f"/api/resource/Mode of Payment/{MODE_OF_PAYMENT}")["data"]
    if [(r["company"], r["default_account"]) for r in doc["accounts"]] != [tuple(row.values())]:
        a.call("PUT", f"/api/resource/Mode of Payment/{MODE_OF_PAYMENT}",
               json={"accounts": [row]})


# Read access the design's roles lack in ERPNext but need for their own work:
# (doctype, role). A receipt reads the company's accounts; submitting an invoice reads its
# items; invoicing and approving invoices read the order and receipt they match; cancelling
# a receipt first looks for invoices against it.
READ_GRANTS = [("Account", "Stock User"), ("Purchase Invoice", "Stock User"),
               ("Item", "Accounts Manager"),
               ("Purchase Order", "Accounts User"), ("Purchase Order", "Accounts Manager"),
               ("Purchase Receipt", "Accounts User"), ("Purchase Receipt", "Accounts Manager")]
_PERMS = "frappe.core.page.permission_manager.permission_manager"


def read_grants(a: Admin) -> None:
    """Add each read permission with ERPNext's permission manager (which first copies the
    doctype's standard permissions into Custom DocPerm), unless the role already reads."""
    for doctype, role in READ_GRANTS:
        rules = a.call("POST", f"/api/method/{_PERMS}.get_permissions",
                       json={"doctype": doctype})["message"]
        if any(r["role"] == role and r["permlevel"] == 0 and r["read"] for r in rules):
            continue
        if any(r["role"] == role and r["permlevel"] == 0 for r in rules):  # a rule, no read
            a.call("POST", f"/api/method/{_PERMS}.update",
                   json={"doctype": doctype, "role": role, "permlevel": 0, "ptype": "read",
                         "value": 1})
        else:  # a new rule, with read
            a.call("POST", f"/api/method/{_PERMS}.add",
                   json={"parent": doctype, "role": role, "permlevel": 0})


def _works(kv: tuple[str, str], tries: int = 8) -> bool:
    # Several calls, so they land on different gunicorn workers.
    c = _client(kv)
    return all(c.get("/api/method/frappe.auth.get_logged_user").status_code == 200
               for _ in range(tries))


def _restart_backend() -> None:
    # On a brand-new site the gunicorn workers can each cache a different encryption key
    # before the first one is written to site_config.json, so a freshly generated API
    # secret fails to decrypt. A restart makes them all read the saved key.
    print("restarting the ERPNext backend once (fresh-site encryption key)...")
    # The frontend (nginx) resolves the backend's address once, so restart it too.
    subprocess.run(["docker", "compose", "-f", str(ROOT / "docker/compose.yml"), "restart",
                    "backend", "frontend"], check=True, capture_output=True)
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
    for email, (_, want) in USERS.items():
        first, last = NAMES[email]
        if not a.exists("User", email):
            a.insert({"doctype": "User", "email": email, "first_name": first, "last_name": last,
                      "send_welcome_email": 0, "user_type": "System User",
                      "roles": [{"role": r} for r in want]})
        else:
            doc = a.call("GET", f"/api/resource/User/{email}")["data"]
            if {r["role"] for r in doc["roles"]} != set(want):
                a.call("PUT", f"/api/resource/User/{email}",
                       json={"roles": [{"role": r} for r in want]})
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
            "erp_guard_cost_centre": f"{cc} - {ABBR}",
            "erp_guard_notes": HISTORY_NOTE, "items": items})
        r.raise_for_status()
        name = r.json()["data"]["name"]
        s = appr.put(f"/api/resource/Purchase Order/{name}", json={
            "docstatus": 1, "erp_guard_approver": "director@example.com"})
        if s.status_code >= 400:
            raise RuntimeError(f"submitting {name}: {s.text[:800]}")
    print(f"history: {len(plan)} approved orders")


_MAKE_RECEIPT = "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt"
_MAKE_INVOICE = "erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice"
_MAKE_PAYMENT = "erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry"


def _submit_new(a: Admin, doc: dict, **fields) -> dict:
    """Insert ERPNext's mapped draft with `fields` set, then submit it."""
    doc = a.call("POST", "/api/method/frappe.client.insert", json={"doc": {**doc, **fields}})
    return a.call("POST", "/api/method/frappe.client.submit", json={"doc": doc["message"]})[
        "message"]


def history_documents(a: Admin) -> None:
    """The first six goods orders of the history were received; four of those were
    invoiced (bill numbers HIST-INV-1..4) and two of those paid from the company bank.
    Entered by Administrator straight into ERPNext, like the orders' own history, so none
    of it is in erp-guard's audit trail. Runs before the workflows exist, so each document
    is submitted directly; turning a workflow on marks submitted documents Approved."""
    if a.get_list("Purchase Receipt", ["name"], [["docstatus", "=", 1]]):
        return
    orders = sorted(a.get_list("Purchase Order", ["name", "transaction_date"],
                               [["erp_guard_notes", "=", HISTORY_NOTE], ["docstatus", "=", 1]]),
                    key=lambda o: (o["transaction_date"], o["name"]))
    goods = [o for o in orders if {i["item_code"] for i in a.call(
        "GET", f"/api/resource/Purchase Order/{o['name']}")["data"]["items"]} <= STOCK_ITEMS]
    for n, order in enumerate(goods[:6], 1):
        on = date.fromisoformat(order["transaction_date"]) + timedelta(days=10)
        dated = {"posting_date": on.isoformat(), "set_posting_time": 1}
        draft = a.call("POST", f"/api/method/{_MAKE_RECEIPT}",
                       json={"source_name": order["name"]})["message"]
        receipt = _submit_new(a, draft, **dated)
        if n > 4:
            continue
        billed = on + timedelta(days=2)
        draft = a.call("POST", f"/api/method/{_MAKE_INVOICE}",
                       json={"source_name": receipt["name"]})["message"]
        invoice = _submit_new(a, draft, bill_no=f"HIST-INV-{n}", bill_date=billed.isoformat(),
                              posting_date=billed.isoformat(), set_posting_time=1,
                              due_date=(billed + timedelta(days=30)).isoformat())
        if n > 2:
            continue
        paid = billed + timedelta(days=28)
        draft = a.call("POST", f"/api/method/{_MAKE_PAYMENT}",
                       json={"dt": "Purchase Invoice", "dn": invoice["name"]})["message"]
        _submit_new(a, draft, posting_date=paid.isoformat(), mode_of_payment=MODE_OF_PAYMENT,
                    paid_from=BANK_ACCOUNT, bank_account=None, reference_no=f"HIST-PAY-{n}",
                    reference_date=paid.isoformat())
    print("history: 6 receipts, 4 invoices, 2 payments")


# Approval workflows: doctype -> (role that submits for approval and revises, roles that
# approve and reject). Who may approve what, and up to how much, is erp-guard's policy
# (config/policy.toml); these roles only let the right people apply transitions.
# Invoices: the controller (Accounts Manager), and the director as backup (Purchase
# Manager; policy refuses the manager).
WORKFLOWS = {
    "Material Request": ("Purchase User", ("Purchase Manager",)),
    "Purchase Order": ("Purchase User", ("Purchase Manager",)),
    "Purchase Invoice": ("Accounts User", ("Accounts Manager", "Purchase Manager")),
    "Payment Entry": ("Accounts User", ("Purchase Manager",)),
    CHANGE_REQUEST: ("Purchase User", ("Purchase Manager",)),
}
STATES = [("Draft", 0), ("Pending Approval", 0), ("Approved", 1), ("Rejected", 0),
          ("Cancelled", 2)]
# (state, action, next state, whether the approver applies it)
TRANSITIONS = [("Draft", "Submit for Approval", "Pending Approval", False),
               ("Pending Approval", "Approve", "Approved", True),
               ("Pending Approval", "Reject", "Rejected", True),
               ("Rejected", "Revise", "Draft", False)]
# Purchase orders can also be withdrawn from approval (amend_purchase_order does this before
# changing a pending order's lines, so it needs resubmitting).
EXTRA_TRANSITIONS = {"Purchase Order": [("Pending Approval", "Withdraw", "Draft", False)]}
# Purchase orders: a draft cancelled through erp-guard (erp_guard_cancelled) gets no more
# transitions. Orders, invoices and payments entering Approved record who approved, in the
# same transaction.
CONDITIONS = {"Purchase Order": "not doc.erp_guard_cancelled"}
ON_ENTER = {(dt, "Approved"): ("erp_guard_approver", "frappe.session.user")
            for dt in ("Purchase Order", "Purchase Invoice", "Payment Entry")}


def _workflow(doctype: str, submitter: str, approvers: tuple[str, ...]) -> dict:
    # The submitter edits drafts and rejected documents, the (first) approver the rest.
    edits = {"Draft": submitter, "Rejected": submitter}
    states = []
    for st, ds in STATES:
        row = {"state": st, "doc_status": str(ds), "allow_edit": edits.get(st, approvers[0])}
        if (doctype, st) in ON_ENTER:
            field, expr = ON_ENTER[(doctype, st)]
            row |= {"update_field": field, "update_value": expr, "evaluate_as_expression": 1}
        states.append(row)
    return {
        "doctype": "Workflow", "workflow_name": f"{doctype} Approval",
        "document_type": doctype, "is_active": 1, "override_status": 0,
        "send_email_alert": 0, "workflow_state_field": "workflow_state", "states": states,
        "transitions": [{"state": st, "action": act, "next_state": nxt, "allowed": role,
                         "allow_self_approval": 1, "condition": CONDITIONS.get(doctype)}
                        for st, act, nxt, by_approver in
                        TRANSITIONS + EXTRA_TRANSITIONS.get(doctype, [])
                        for role in (approvers if by_approver else (submitter,))],
    }


def workflows(a: Admin) -> None:
    """Create each workflow, or bring an existing one up to this definition (a site seeded
    before the purchase order's workflow was turned on gets it activated here)."""
    for state, _ in STATES:
        a.ensure({"doctype": "Workflow State", "workflow_state_name": state}, state)
    for _, action, _, _ in TRANSITIONS + [t for ts in EXTRA_TRANSITIONS.values() for t in ts]:
        a.ensure({"doctype": "Workflow Action Master", "workflow_action_name": action}, action)
    for doctype, (submitter, approvers) in WORKFLOWS.items():
        doc = _workflow(doctype, submitter, approvers)
        name = doc["workflow_name"]
        if a.exists("Workflow", name):
            a.call("PUT", f"/api/resource/Workflow/{httpx.URL(name)}", json=doc)
        else:
            a.insert(doc)
    print(f"workflows: {len(WORKFLOWS)}, all active")


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
        tag = USERS[email][0]
        out += [f"ERPNEXT_KEY_{tag}={k}", f"ERPNEXT_SECRET_{tag}={s}"]
    k, s = keys["requester@example.com"]
    out += [f"ERPNEXT_API_KEY={k}", f"ERPNEXT_API_SECRET={s}"]
    path.write_text("\n".join(keep + out) + "\n")
    path.chmod(0o600)


def main() -> None:
    a = Admin()
    if setup_wizard(a):
        # Make sure the key the wizard saved is the one every worker uses before any API
        # secret is encrypted with it (see _restart_backend).
        _restart_backend()
        a = Admin()
    custom_fields(a)
    audit_doctype(a)
    change_request_doctype(a)
    masters(a)
    read_grants(a)
    keys = users(a)
    write_env(keys)
    history(keys)
    a = Admin()  # a fresh session, in case users() restarted the backend
    history_documents(a)
    workflows(a)  # after the history, so history documents needn't pass through them
    print("ERPNext seeded; API keys in .env")


if __name__ == "__main__":
    sys.exit(main())
