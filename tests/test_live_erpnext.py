"""Integration tests against the local ERPNext from `make up && make seed`.

Skipped unless it's reachable. Each test cancels what it creates, so budgets and the
split window stay usable across runs (cancelled orders stay on record; nothing is deleted).
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from erp_guard import demo
from erp_guard.connectors.erpnext import ErpNext, HardLimitError
from erp_guard.env import load_env
from erp_guard.generic.actions import meta_of
from erp_guard.models import Line, Status
from erp_guard.policy import load_policy
from erp_guard.service import Service

load_env()
URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")


def _up() -> bool:
    try:
        return httpx.get(f"{URL}/api/method/ping", timeout=3).status_code == 200 \
            and bool(os.environ.get("ERPNEXT_KEY_REQUESTER"))
    except httpx.HTTPError:
        return False


pytestmark = [pytest.mark.live, pytest.mark.skipif(not _up(), reason="local ERPNext not up")]
CC = "Facilities - LDU"


def svc(tag: str) -> Service:
    return Service(ErpNext.from_env(tag), load_policy())


def run(s: Service, action: str, args: dict):
    p = s.preview(action, args)
    return p, s.execute(action, args, p.code)


@pytest.fixture
def created():
    ids: list[str] = []
    yield ids
    req = ErpNext.from_env("REQUESTER")
    for po in ids:
        o = req.get_order(po)
        if o and o.status not in (Status.CANCELLED,):
            (ErpNext.from_env("DIRECTOR") if o.status == Status.APPROVED else req) \
                .cancel_order(po, "test cleanup")


def _po(qty=2, rate=640.0, item="HVAC-SVC", supplier="TRANE U.S. INC", **kw):
    return {"supplier": supplier, "cost_centre": CC,
            "lines": [{"item_code": item, "qty": qty, "rate": rate}], **kw}


def test_identity_comes_from_the_key():
    assert svc("REQUESTER").actor == "requester@example.com"
    assert svc("MANAGER").actor == "manager@example.com"


def test_full_lifecycle_with_audit(created):
    req, mgr = svc("REQUESTER"), svc("MANAGER")
    _, out = run(req, "create_purchase_order", _po(notes=f"live {uuid.uuid4().hex[:6]}"))
    assert out.outcome == "written", out.message
    po = out.target
    created.append(po)
    assert run(req, "submit_for_approval", {"po": po})[1].outcome == "written"
    p = req.preview("approve_purchase_order", {"po": po})
    assert p.blocked  # requester can't approve own order
    _, ok = run(mgr, "approve_purchase_order", {"po": po})
    assert ok.outcome == "written", ok.message
    o = req.erp.get_order(po)
    assert o.status == Status.APPROVED and o.approver == "manager@example.com"
    trail = req.erp.read_audit(po, limit=10)
    assert [a.outcome for a in trail][:3] == ["written"] * 3
    _, c = run(req, "cancel_purchase_order", {"po": po, "reason": "test done"})
    assert c.outcome == "written" and req.erp.get_order(po).status == Status.CANCELLED


def test_reject_amend_resubmit(created):
    req, mgr = svc("REQUESTER"), svc("MANAGER")
    _, out = run(req, "create_purchase_order", _po())
    created.append(out.target)
    run(req, "submit_for_approval", {"po": out.target})
    _, rej = run(mgr, "reject_purchase_order", {"po": out.target, "reason": "qty too high"})
    assert rej.outcome == "written"
    assert req.erp.get_order(out.target).status == Status.REJECTED
    _, am = run(req, "amend_purchase_order", {"po": out.target, "lines": [
        {"item_code": "HVAC-SVC", "qty": 1, "rate": 640}]})
    assert am.outcome == "written", am.message
    o = req.erp.get_order(out.target)
    assert o.status == Status.DRAFT and o.total == 640


def _wf(po: str, action: str, comment: str = "") -> dict:
    return {"doctype": "Purchase Order", "name": po, "action": action,
            **({"comment": comment} if comment else {})}


def test_live_po_workflow_states(created):
    """Order approval runs on ERPNext's own workflow: the generic workflow_action reaches
    the purchase-to-pay checks, and ERPNext records the states."""
    req, mgr = svc("REQUESTER"), svc("MANAGER")
    _, out = run(req, "create_purchase_order", _po())
    assert out.outcome == "written", out.message
    po = out.target
    created.append(po)
    doc = req.erp.get_doc("Purchase Order", po)
    assert (doc["workflow_state"], doc["docstatus"]) == ("Draft", 0)
    p, sub = run(req, "workflow_action", _wf(po, "Submit for Approval"))
    assert sub.outcome == "written", (sub.message, [r.message for r in p.checks])
    doc = req.erp.get_doc("Purchase Order", po)
    assert (doc["workflow_state"], doc["docstatus"]) == ("Pending Approval", 0)
    assert req.erp.get_order(po).status == Status.PENDING
    # The requester's own approval is refused by the pack's checks, before ERPNext.
    p = req.preview("workflow_action", _wf(po, "Approve"))
    assert p.blocked and any(r.check == "segregation_of_duties" for r in p.checks
                             if r.level == "block")
    p, ok = run(mgr, "workflow_action", _wf(po, "Approve", "fine"))
    assert ok.outcome == "written", (ok.message, [r.message for r in p.checks])
    doc = req.erp.get_doc("Purchase Order", po)
    assert (doc["workflow_state"], doc["docstatus"]) == ("Approved", 1)
    o = req.erp.get_order(po)
    assert o.status == Status.APPROVED and o.approver == "manager@example.com"
    trail = req.erp.read_audit(po, limit=10)
    assert [(a.action, a.outcome) for a in trail][:2] == [("workflow_action", "written")] * 2


def test_live_po_reject_revise_and_draft_cancel(created):
    """Reject stores the reason on the order; Revise goes through amend_purchase_order; a
    cancelled draft stays a draft with the cancelled flag, and ERPNext offers no more
    transitions on it."""
    req, mgr = svc("REQUESTER"), svc("MANAGER")
    _, out = run(req, "create_purchase_order", _po())
    po = out.target
    created.append(po)
    assert run(req, "submit_for_approval", {"po": po})[1].outcome == "written"
    assert mgr.preview("workflow_action", _wf(po, "Reject")).blocked  # needs a reason
    _, rej = run(mgr, "workflow_action", _wf(po, "Reject", "qty too high"))
    assert rej.outcome == "written", rej.message
    doc = req.erp.get_doc("Purchase Order", po)
    assert (doc["workflow_state"], doc["erp_guard_reason"]) == ("Rejected", "qty too high")
    assert req.erp.get_order(po).status == Status.REJECTED
    p = req.preview("workflow_action", _wf(po, "Revise"))
    assert p.blocked and "amend_purchase_order" in " ".join(r.message for r in p.checks)
    assert req.preview("submit_document", {"doctype": "Purchase Order", "name": po}).blocked
    _, c = run(req, "cancel_purchase_order", {"po": po, "reason": "not needed"})
    assert c.outcome == "written", c.message
    doc = req.erp.get_doc("Purchase Order", po)
    assert (doc["docstatus"], doc["erp_guard_cancelled"]) == (0, 1)
    assert req.erp.get_order(po).status == Status.CANCELLED
    assert req.erp.transitions("Purchase Order", po) == []  # nothing more to do with it


def test_write_without_code_is_refused_and_audited_in_erp(created):
    req = svc("REQUESTER")
    before = len(req.erp.list_orders(cost_centre=CC))
    out = req.execute("create_purchase_order", _po(), None)
    assert out.outcome == "refused"
    assert len(req.erp.list_orders(cost_centre=CC)) == before
    assert req.erp.read_audit(limit=1)[0].outcome == "refused"


def test_duplicate_supplier_against_real_master():
    p = svc("REQUESTER").preview("onboard_supplier", {"name": "Herc Rentals, Inc"})
    assert p.blocked


def test_hard_limits_hold_against_the_real_client():
    erp = ErpNext.from_env("REQUESTER")
    # Payment Entry is pack-owned, so the client lets it through to the pack's checks; a
    # ledger-posting type with no pack, and the system's own types, it never writes.
    for doctype in ("Journal Entry", "Bank Account", "User"):
        with pytest.raises(HardLimitError):
            erp.c.request("POST", f"/api/resource/{doctype}", body={})
        with pytest.raises(HardLimitError):
            erp.c.request("GET", f"/api/resource/{doctype}")
    with pytest.raises(HardLimitError):
        erp.c.request("PUT", "/api/resource/ERP Guard Audit/x", body={"outcome": "written"})
    with pytest.raises(HardLimitError):
        erp.c.request("DELETE", "/api/resource/Supplier/TRANE U.S. INC")
    with pytest.raises(HardLimitError):
        erp.c.request("PUT", "/api/resource/Supplier/TRANE U.S. INC",
                      body={"default_bank_account": "x"})


def test_live_generic_task_create_and_update():
    """A later-phase type through the generic tools: create, update, audit in ERPNext. Tasks
    can't be cancelled, so the test closes its Task by setting it to Cancelled."""
    req = svc("REQUESTER")
    subject = f"live generic {uuid.uuid4().hex[:6]}"
    p, out = run(req, "create_document", {"doctype": "Task", "fields": {"subject": subject}})
    assert out.outcome == "written", (out.message, [r.message for r in p.checks])
    task = out.target
    assert req.erp.get_doc("Task", task)["subject"] == subject
    p, upd = run(req, "update_document", {"doctype": "Task", "name": task,
                                          "fields": {"subject": subject + " (b)"}})
    assert f"subject: '{subject}' → '{subject} (b)'" in p.summary
    assert upd.outcome == "written", upd.message
    _, done = run(req, "update_document", {"doctype": "Task", "name": task,
                                           "fields": {"status": "Cancelled"}})
    assert done.outcome == "written", done.message
    doc = req.erp.get_doc("Task", task)
    assert doc["subject"] == subject + " (b)" and doc["status"] == "Cancelled"
    trail = req.erp.read_audit(task, limit=10)
    assert [a.action for a in trail] == ["update_document", "update_document",
                                         "create_document"]
    assert {a.outcome for a in trail} == {"written"}


def test_live_generic_scope_and_workflow_detection():
    req = svc("REQUESTER")
    meta, _ = meta_of(req, "Task")
    assert meta.has_workflow is False  # getdoctype's __workflow_docs, empty with no workflow
    w = req.erp.c.writable
    assert w("Task") and w("Purchase Order") and not w("Journal Entry")
    assert not w("Cost Center") and not w("Server Script")
    p = req.preview("create_document", {"doctype": "Journal Entry", "fields": {}})
    assert p.blocked and "ledger" in p.checks[0].message
    out = req.execute("create_document", {"doctype": "Server Script", "fields": {}}, None)
    assert out.outcome == "refused"
    assert "Projects User" in req.erp.roles()


def test_live_child_row_not_written_on_its_own():
    """A real PO's item row can't be edited around the PO's checks."""
    req = svc("REQUESTER")
    po = req.erp.find("Purchase Order", [["docstatus", "=", 1]], ["name"], "creation asc", 1)[0]
    row = req.erp.get_doc("Purchase Order", po["name"])["items"][0]
    args = {"doctype": "Purchase Order Item", "name": row["name"], "fields": {"qty": 999}}
    p = req.preview("update_document", args)
    assert p.blocked and p.code is None
    assert "written through their parent document" in p.checks[0].message
    assert req.execute("update_document", args, None).outcome == "refused"
    with pytest.raises(HardLimitError):
        req.erp.save({**row, "qty": 999})
    with pytest.raises(HardLimitError):
        req.erp.c.request("PUT", f"/api/resource/Purchase Order Item/{row['name']}",
                          body={"qty": 999})
    after = req.erp.get_doc("Purchase Order", po["name"])["items"][0]
    assert after["qty"] == row["qty"] and after["modified"] == row["modified"]


# --- seed v2: purchase-to-pay users, workflows, stock items, history -------------------

def test_live_users_and_roles():
    """Every demo user's key resolves to that user, holding exactly its roles from
    demo.USERS, and each new user can write to the audit trail (a refused write is audited
    as that user)."""
    for email, (tag, roles) in demo.USERS.items():
        s = svc(tag)
        assert s.actor == email
        assert set(s.erp.roles()) == set(roles), email
    for tag in ("RECEIVER", "AP", "CONTROLLER", "TREASURY"):
        s = svc(tag)
        assert s.execute("create_purchase_order", _po(), None).outcome == "refused"
        last = s.erp.read_audit(limit=1)[0]
        assert (last.actor, last.outcome) == (s.actor, "refused")


def test_live_workflows_exist():
    """Material requests, purchase orders, invoices, payments and change requests run on
    active workflows. A fresh draft request goes Draft -> Pending Approval -> Approved, then
    is cancelled."""
    for tag, doctype in (("REQUESTER", "Material Request"), ("REQUESTER", "Purchase Order"),
                         ("AP", "Purchase Invoice"), ("TREASURY", "Payment Entry"),
                         ("REQUESTER", "ERP Guard Change Request")):
        assert meta_of(svc(tag), doctype)[0].has_workflow, doctype
    # Only System Manager reads Workflow records: the seed's local Administrator login.
    admin = httpx.Client(base_url=URL, timeout=60)
    admin.post("/api/method/login", json={"usr": "Administrator", "pwd": "admin"})
    po_flow = admin.get("/api/resource/Workflow/Purchase Order Approval").json()["data"]
    assert (po_flow["document_type"], po_flow["is_active"]) == ("Purchase Order", 1)
    pi_flow = svc("AP").erp.get_meta("Purchase Invoice")["__workflow_docs"][0]
    assert {t["allowed"] for t in pi_flow["transitions"] if t["action"] == "Approve"} == {
        "Accounts Manager", "Purchase Manager"}
    req, mgr = svc("REQUESTER").erp, svc("MANAGER").erp
    when = (date.today() + timedelta(days=14)).isoformat()
    mr = req.insert({"doctype": "Material Request", "material_request_type": "Purchase",
                     "company": req.company, "schedule_date": when,
                     "items": [{"item_code": "PAPER-A4", "qty": 1, "schedule_date": when}]})
    assert mr["workflow_state"] == "Draft"
    assert req.transitions("Material Request", mr["name"]) == ["Submit for Approval"]
    req.apply_workflow("Material Request", mr["name"], "Submit for Approval")
    assert req.transitions("Material Request", mr["name"]) == []
    assert sorted(mgr.transitions("Material Request", mr["name"])) == ["Approve", "Reject"]
    done = mgr.apply_workflow("Material Request", mr["name"], "Approve")
    assert (done["workflow_state"], done["docstatus"]) == ("Approved", 1)
    mgr.cancel("Material Request", mr["name"])
    # A plain cancel skips validation in Frappe v16, so the workflow state isn't moved to
    # Cancelled: docstatus is what says a document is cancelled.
    assert mgr.get_doc("Material Request", mr["name"])["docstatus"] == 2


def test_live_history_documents():
    """Of the 13 approved history orders, six have a submitted receipt, four of those a
    submitted invoice and two of those a submitted payment from the company bank."""
    erp = ErpNext.from_env("DIRECTOR")
    hist = [p["name"] for p in erp.find(
        "Purchase Order", [["erp_guard_notes", "=", "Seed history (synthetic)."]],
        ["name", "docstatus", "workflow_state"], limit=0)
        if (p["docstatus"], p["workflow_state"]) == (1, "Approved")]
    assert len(hist) == 13
    receipts = {r["name"] for r in erp.find(
        "Purchase Receipt", [["docstatus", "=", 1],
                             ["Purchase Receipt Item", "purchase_order", "in", hist]],
        ["name"], limit=0)}
    assert len(receipts) == 6
    invoices = erp.find("Purchase Invoice", [["docstatus", "=", 1],
                                             ["bill_no", "like", "HIST-INV-%"]],
                        ["name", "bill_no", "workflow_state", "outstanding_amount"], limit=0)
    assert sorted(i["bill_no"] for i in invoices) == [f"HIST-INV-{n}" for n in (1, 2, 3, 4)]
    assert {i["workflow_state"] for i in invoices} == {"Approved"}
    for i in invoices:
        rows = erp.get_doc("Purchase Invoice", i["name"])["items"]
        assert {r["purchase_receipt"] for r in rows} <= receipts
    names = [i["name"] for i in invoices]
    payments = erp.find("Payment Entry", [
        ["docstatus", "=", 1], ["Payment Entry Reference", "reference_name", "in", names]],
        ["name", "paid_from", "bank_account", "mode_of_payment", "workflow_state"], limit=0)
    assert len(payments) == 2
    for p in payments:
        assert (p["paid_from"], p["bank_account"], p["mode_of_payment"],
                p["workflow_state"]) == (demo.BANK_ACCOUNT, None, demo.MODE_OF_PAYMENT,
                                         "Approved")
    assert sorted(float(i["outstanding_amount"]) == 0 for i in invoices) == [
        False, False, True, True]
    treasury = ErpNext.from_env("TREASURY")
    acc = treasury.get_doc("Account", demo.BANK_ACCOUNT)
    assert (acc["account_type"], acc["parent_account"], acc["is_group"]) == (
        "Bank", f"Bank Accounts - {demo.ABBR}", 0)
    mop = treasury.get_doc("Mode of Payment", demo.MODE_OF_PAYMENT)
    assert [(r["company"], r["default_account"]) for r in mop["accounts"]] == [
        (demo.COMPANY, demo.BANK_ACCOUNT)]


def test_live_stock_flags():
    """Goods are stock items received into one warehouse; services are non-stock."""
    erp = ErpNext.from_env("REQUESTER")
    assert set(demo.ITEMS) > demo.STOCK_ITEMS
    for code in demo.ITEMS:
        item = erp.get_doc("Item", code)
        stock = code in demo.STOCK_ITEMS
        assert item["is_stock_item"] == int(stock), code
        warehouses = [(d["company"], d.get("default_warehouse")) for d in item["item_defaults"]]
        assert warehouses == ([(demo.COMPANY, demo.WAREHOUSE)] if stock else []), code


def test_live_read_grants(created):
    """The seed's narrow read grants let each role do its own part: the receiver records
    a receipt against an approved order, AP reads the order and drafts its invoice, the
    controller reads items. The receipt is cancelled and the order with it."""
    req, mgr = svc("REQUESTER").erp, svc("MANAGER").erp
    o = req.create_order("TRANE U.S. INC", CC, [Line("PAPER-A4", Decimal(1), Decimal(42))],
                         "live read grants", date.today() + timedelta(days=14))
    created.append(o.id)
    req.set_status(o.id, Status.PENDING, "")
    mgr.approve_order(o.id)
    rcv = svc("RECEIVER").erp
    pr = rcv.insert(rcv.map_doc("Purchase Order", o.id, "Purchase Receipt"))
    assert rcv.submit("Purchase Receipt", pr["name"])["docstatus"] == 1
    assert rcv.cancel("Purchase Receipt", pr["name"])["docstatus"] == 2
    ap = svc("AP").erp
    assert ap.get_doc("Purchase Order", o.id)["name"] == o.id
    draft = ap.map_doc("Purchase Order", o.id, "Purchase Invoice")
    assert [i["item_code"] for i in draft["items"]] == ["PAPER-A4"]
    assert svc("CONTROLLER").erp.get_doc("Item", "PAPER-A4")["is_stock_item"] == 1
