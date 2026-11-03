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
from erp_guard.connectors.base import ErpError
from erp_guard.connectors.erpnext import ErpNext, HardLimitError
from erp_guard.env import load_env
from erp_guard.generic.actions import meta_of
from erp_guard.models import Line, Status
from erp_guard.packs.p2p import APPROVE, SUBMIT_FOR_APPROVAL
from erp_guard.packs.p2p.models import (
    Allocation,
    DocState,
    InvoiceLine,
    ReceiptLine,
    RequestLine,
)
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


def test_live_amend_pending_withdraws_it(created):
    """Amending a pending order withdraws it to Draft first. An approval code the manager
    previewed before the amendment is refused at write time: the order isn't pending."""
    req, mgr = svc("REQUESTER"), svc("MANAGER")
    _, out = run(req, "create_purchase_order", _po())
    po = out.target
    created.append(po)
    assert run(req, "submit_for_approval", {"po": po})[1].outcome == "written"
    early = mgr.preview("approve_purchase_order", {"po": po})
    assert early.code, [r.message for r in early.checks]
    assert req.erp.transitions("Purchase Order", po) == ["Withdraw"]  # the submitter's way back
    _, am = run(req, "amend_purchase_order", {"po": po, "lines": [  # under $5,000: no request
        {"item_code": "HVAC-SVC", "qty": 7, "rate": 640}]})
    assert am.outcome == "written", am.message
    doc = req.erp.get_doc("Purchase Order", po)
    assert (doc["workflow_state"], doc["docstatus"]) == ("Draft", 0)
    o = req.erp.get_order(po)
    assert o.status == Status.DRAFT and o.total == 7 * 640
    late = mgr.execute("approve_purchase_order", {"po": po}, early.code)
    assert late.outcome == "blocked" and "Draft" in late.message
    assert req.erp.get_doc("Purchase Order", po)["docstatus"] == 0


def test_live_requester_cancels_own_pending_order(created):
    """The requester cancels their own order while it waits for approval: it stays a draft
    with the cancelled flag, and no approver can act on it any more."""
    req, mgr = svc("REQUESTER"), svc("MANAGER")
    _, out = run(req, "create_purchase_order", _po())
    po = out.target
    created.append(po)
    assert run(req, "submit_for_approval", {"po": po})[1].outcome == "written"
    _, c = run(req, "cancel_purchase_order", {"po": po, "reason": "found a cheaper quote"})
    assert c.outcome == "written", c.message
    doc = req.erp.get_doc("Purchase Order", po)
    assert (doc["docstatus"], doc["erp_guard_cancelled"], doc["erp_guard_reason"]) == (
        0, 1, "found a cheaper quote")
    assert req.erp.get_order(po).status == Status.CANCELLED
    assert mgr.erp.transitions("Purchase Order", po) == []
    assert mgr.preview("approve_purchase_order", {"po": po}).blocked


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
    assert {t.get("condition") for t in pi_flow["transitions"]} == {
        "not doc.erp_guard_cancelled"}
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


# --- purchase-to-pay documents through the connector -----------------------------------

D = Decimal
PI, PE, PR = "Purchase Invoice", "Payment Entry", "Purchase Receipt"


@pytest.fixture
def undo():
    """(connector, doctype, name) to cancel after the test, newest first (a payment before
    its invoice, an invoice before its receipt). Nothing is deleted."""
    docs: list[tuple] = []
    yield docs
    for erp, doctype, name in reversed(docs):
        if erp.get_doc(doctype, name)["docstatus"] == 1:
            erp.cancel(doctype, name)


def _approved_order(created, lines) -> str:
    req, mgr = svc("REQUESTER").erp, svc("MANAGER").erp
    o = req.create_order("TRANE U.S. INC", CC, lines, "live p2p",
                         date.today() + timedelta(days=14))
    created.append(o.id)
    req.set_status(o.id, Status.PENDING, "")
    mgr.approve_order(o.id)
    return o.id


def test_live_p2p_roundtrip(created, undo):
    """Receipt and return, invoice from the receipt and the order, hold and release,
    approval, debit note, payment and its approval: outstanding goes to 0. Every document
    is cancelled afterwards, and none is a history document, so the seed's history counts
    don't change."""
    po = _approved_order(created, [Line("PAPER-A4", D(4), D(42)),
                                   Line("HVAC-SVC", D(1), D(640))])
    rcv, ap, ctl = svc("RECEIVER").erp, svc("AP").erp, svc("CONTROLLER").erp
    trs, mgr = svc("TREASURY").erp, svc("MANAGER").erp

    r = rcv.create_receipt(po, [ReceiptLine(po, "PAPER-A4", D(3))])
    undo.append((rcv, PR, r.id))
    assert (r.state, r.receiver, r.supplier_id, r.lines) == (
        DocState.SUBMITTED, "receiver@example.com", "TRANE U.S. INC",
        (ReceiptLine(po, "PAPER-A4", D(3)),))
    ret = rcv.create_receipt(po, [ReceiptLine(po, "PAPER-A4", D(1))], is_return=True,
                             return_against=r.id)
    undo.append((rcv, PR, ret.id))
    assert (ret.is_return, ret.return_against, ret.lines) == (
        True, r.id, (ReceiptLine(po, "PAPER-A4", D(1)),))
    assert rcv.list_receipts(po=po) == [r, ret]

    bill = f"LIVE-{uuid.uuid4().hex[:6]}"
    inv = ap.create_invoice("TRANE U.S. INC", bill, date.today(), [
        InvoiceLine(po, "PAPER-A4", D(2), D(42), r.id),
        InvoiceLine(po, "HVAC-SVC", D(1), D(640))], "live roundtrip")
    undo.append((ctl, PI, inv.id))
    assert (inv.state, inv.entered_by, inv.bill_no, inv.total, inv.remarks) == (
        DocState.DRAFT, "ap@example.com", bill, D(724), "live roundtrip")
    assert [(ln.po, ln.item_code, ln.receipt) for ln in inv.lines] == [
        (po, "PAPER-A4", r.id), (po, "HVAC-SVC", None)]
    assert ap.outstanding(inv.id) == 0
    ap.update_draft(PI, inv.id, {"remarks": "live roundtrip (checked)"})
    ap.workflow(PI, inv.id, SUBMIT_FOR_APPROVAL)
    assert ap.get_invoice(inv.id).state == DocState.PENDING
    ctl.workflow(PI, inv.id, APPROVE)
    inv = ap.get_invoice(inv.id)
    assert (inv.state, inv.approver, inv.remarks) == (
        DocState.APPROVED, "controller@example.com", "live roundtrip (checked)")
    assert ap.list_invoices(po=po) == [inv]
    assert trs.outstanding(inv.id) == D(724)

    assert ctl.set_invoice_hold(inv.id, True, "price query").on_hold
    assert ctl.get_doc(PI, inv.id)["hold_comment"] == "price query"
    assert not ctl.set_invoice_hold(inv.id, False, "").on_hold

    note = ap.create_debit_note(inv.id, [InvoiceLine(po, "PAPER-A4", D(1), D(42))],
                                "one case damaged")
    undo.append((ctl, PI, note.id))
    assert (note.is_return, note.return_against, note.total, note.state) == (
        True, inv.id, D(42), DocState.DRAFT)
    ap.workflow(PI, note.id, SUBMIT_FOR_APPROVAL)
    ctl.workflow(PI, note.id, APPROVE)
    assert trs.outstanding(inv.id) == D(682)

    pay = trs.create_payment("TRANE U.S. INC", [Allocation(inv.id, D(682))],
                             demo.BANK_ACCOUNT)
    undo.append((mgr, PE, pay.id))
    assert (pay.state, pay.payer, pay.paid_from, pay.amount, pay.unallocated) == (
        DocState.DRAFT, "treasury@example.com", demo.BANK_ACCOUNT, D(682), 0)
    doc = trs.get_doc(PE, pay.id)
    assert (doc["payment_type"], doc["party_type"], doc.get("bank_account")) == (
        "Pay", "Supplier", None)
    assert trs.outstanding(inv.id) == D(682)  # a draft pays nothing
    trs.workflow(PE, pay.id, SUBMIT_FOR_APPROVAL)
    mgr.workflow(PE, pay.id, APPROVE)
    pay = trs.get_payment(pay.id)
    assert (pay.state, pay.approver) == (DocState.APPROVED, "manager@example.com")
    assert trs.outstanding(inv.id) == 0
    assert trs.list_payments(invoice=inv.id) == [pay]
    assert pay in trs.list_payments(supplier_id="TRANE U.S. INC", since=date.today())

    with pytest.raises(ErpError, match="workflow"):
        trs.submit_doc(PE, pay.id)
    with pytest.raises(ErpError, match="Not on"):
        rcv.create_receipt(po, [ReceiptLine(po, "LAPTOP-14", D(1))])


def test_live_receive_and_return(created, undo):
    """Receipts and returns through the guarded tools, as the receiver: 106 of 100 is
    refused, 100 lands, a return gives room back, and a receipt is cancelled through the
    generic cancel_document by its receiver but not by a requester. Everything is cancelled
    afterwards; none of it is a history document. (The guard allows 5% over; ERPNext's own
    allowance is 0 at its defaults, so receipts here stay within the ordered quantity.)"""
    po = _approved_order(created, [Line("PAPER-A4", D(100), D(42))])
    rcv = svc("RECEIVER")
    item = lambda q: [{"item_code": "PAPER-A4", "qty": q}]  # noqa: E731

    # The order's approver and a plain requester can't record goods.
    for tag, check in (("MANAGER", "receiver independence"), ("REQUESTER", "role")):
        p = svc(tag).preview("receive_goods", {"po": po, "lines": item(1)})
        assert p.code is None and any(r.check == check and r.level == "block"
                                      for r in p.checks), tag

    p, out = run(rcv, "receive_goods", {"po": po, "lines": item(106)})
    assert p.code is None and out.outcome == "refused"
    assert rcv.erp.list_receipts(po=po) == []

    args = {"po": po, "lines": item(100), "notes": "pallet 3 of 3"}
    p, out = run(rcv, "receive_goods", args)
    assert out.outcome == "written", out.message
    r = rcv.erp.get_receipt(out.target)
    undo.append((rcv.erp, PR, r.id))
    assert (r.state, r.receiver, r.lines) == (
        DocState.SUBMITTED, "receiver@example.com", (ReceiptLine(po, "PAPER-A4", D(100)),))
    assert rcv.erp.get_doc(PR, r.id)["docstatus"] == 1

    def drafts():
        return rcv.erp.find(PR, [["docstatus", "=", 0],
                                 ["Purchase Receipt Item", "purchase_order", "=", po]],
                            ["name"], limit=0)

    # 101 of 100 is within the guard's 5%, but ERPNext's own allowance (0% at its defaults)
    # refuses the submit: the stricter wins, and no draft is left behind.
    p, out = run(rcv, "receive_goods", {"po": po, "lines": item(1)})
    assert p.code and out.outcome == "error", out.message
    assert drafts() == []

    ret_args = {"receipt": r.id, "lines": item(5), "reason": "damaged on delivery"}
    assert run(rcv, "return_goods", {**ret_args, "lines": item(106)})[0].code is None
    p, out = run(rcv, "return_goods", ret_args)
    assert out.outcome == "written", out.message
    ret = rcv.erp.get_receipt(out.target)
    undo.append((rcv.erp, PR, ret.id))
    assert (ret.is_return, ret.return_against, ret.lines) == (
        True, r.id, (ReceiptLine(po, "PAPER-A4", D(5)),))
    # 100 received less 5 returned is 95 (returns are netted): the guard allows 10 more (105,
    # within 5%) and blocks 11 (106). ERPNext's 0% allowance then refuses the 10.
    assert run(rcv, "receive_goods", {"po": po, "lines": item(11)})[0].code is None
    p, out = run(rcv, "receive_goods", {"po": po, "lines": item(10)})
    assert p.code and out.outcome == "error", out.message
    assert drafts() == []

    # Cancelling goes through cancel_document: a receipt with a live return waits for it.
    cancel = lambda s, name: run(s, "cancel_document", {  # noqa: E731
        "doctype": PR, "name": name, "reason": "entered in error"})
    assert cancel(svc("REQUESTER"), ret.id)[0].code is None  # not the receiver
    blocked, _ = cancel(rcv, r.id)
    assert blocked.code is None and any("return" in c.message for c in blocked.checks)
    out = cancel(rcv, ret.id)[1]
    assert out.outcome == "written", out.message
    assert rcv.erp.get_receipt(ret.id).state == DocState.CANCELLED
    assert cancel(rcv, r.id)[1].outcome == "written"
    assert rcv.erp.get_receipt(r.id).state == DocState.CANCELLED
    assert [x.outcome for x in rcv.erp.read_audit(r.id)][:1] == ["written"]


def test_live_invoice_match_and_hold(created, undo):
    """Invoices through the guarded tools: a quantity above what was received and an
    invoice number already used (in another spelling) are refused and write nothing; an
    invoice matched to the receipt and the order is entered by AP, edited within the
    match, submitted, refused to AP and approved by the controller, held and released; a
    debit note larger than the invoice is refused, a smaller one approved; cancelling goes
    through cancel_document and only an invoice approver may. Everything is cancelled
    afterwards; nothing is a history document."""
    po = _approved_order(created, [Line("PAPER-A4", D(4), D(42)),
                                   Line("HVAC-SVC", D(1), D(640))])
    rcv, ap, ctl = svc("RECEIVER"), svc("AP"), svc("CONTROLLER")
    p, out = run(rcv, "receive_goods", {"po": po, "lines": [
        {"item_code": "PAPER-A4", "qty": 3}]})
    assert out.outcome == "written", out.message
    receipt = out.target
    undo.append((rcv.erp, PR, receipt))

    n = 100000 + uuid.uuid4().int % 900000
    lines = lambda paper: [  # noqa: E731
        {"po": po, "item_code": "PAPER-A4", "qty": paper, "rate": 42},
        {"po": po, "item_code": "HVAC-SVC", "qty": 1, "rate": 640}]
    args = {"supplier": "TRANE U.S. INC", "bill_no": f"INV-0{n}",
            "bill_date": date.today().isoformat(), "lines": lines(4)}
    blocked = lambda p: {c.check for c in p.checks if c.level == "block"}  # noqa: E731

    def drafts():
        return ap.erp.find(PI, [["docstatus", "=", 0],
                                ["Purchase Invoice Item", "purchase_order", "=", po]],
                           ["name"], limit=0)

    p, out = run(ap, "enter_supplier_invoice", args)  # 4 billed, 3 received
    assert p.code is None and "three-way match: quantity" in blocked(p)
    assert out.outcome == "refused" and drafts() == []

    p, out = run(ap, "enter_supplier_invoice", {**args, "lines": lines(2)})
    assert out.outcome == "written", out.message
    inv_id = out.target
    undo.append((ctl.erp, PI, inv_id))
    inv = ap.erp.get_invoice(inv_id)
    assert (inv.state, inv.entered_by, inv.bill_no, inv.total) == (
        DocState.DRAFT, "ap@example.com", f"INV-0{n}", D(724))
    assert [(ln.item_code, ln.receipt) for ln in inv.lines] == [
        ("PAPER-A4", receipt), ("HVAC-SVC", None)]

    p = ap.preview("enter_supplier_invoice", {**args, "bill_no": f"inv {n}",
                                              "lines": lines(1)})
    assert p.code is None and "duplicate invoice: number" in blocked(p)

    # Edits go through update_document to the pack: 4 is over what was received, 3 isn't.
    upd = lambda paper: {"doctype": PI, "name": inv_id,  # noqa: E731
                         "fields": {"items": [{"qty": paper}, {"qty": 1}]}}
    assert "three-way match: quantity" in blocked(ap.preview("update_document", upd(4)))
    assert run(ap, "update_document", upd(3))[1].outcome == "written"
    assert ap.erp.get_invoice(inv_id).total == D(766)

    wf = lambda s, action, comment=None: run(s, "workflow_action", {  # noqa: E731
        "doctype": PI, "name": inv_id, "action": action, "comment": comment})
    assert wf(ap, SUBMIT_FOR_APPROVAL)[1].outcome == "written"
    p, out = wf(ap, APPROVE)
    assert {"invoice separation", "invoice authority"} <= blocked(p)
    assert out.outcome == "refused"
    p, out = wf(ctl, APPROVE, "matched to receipt")
    assert out.outcome == "written", out.message
    inv = ctl.erp.get_invoice(inv_id)
    assert (inv.state, inv.approver) == (DocState.APPROVED, "controller@example.com")

    assert "hold" in blocked(ap.preview("hold_invoice", {"invoice": inv_id,
                                                         "reason": "price query"}))
    assert run(ctl, "hold_invoice", {"invoice": inv_id, "reason": "price query"})[1] \
        .outcome == "written"
    assert ctl.erp.get_invoice(inv_id).on_hold
    assert ctl.erp.get_doc(PI, inv_id)["hold_comment"] == "price query"
    assert run(ctl, "release_invoice", {"invoice": inv_id, "comment": "credit agreed"})[1] \
        .outcome == "written"
    assert not ctl.erp.get_invoice(inv_id).on_hold

    note = {"invoice": inv_id, "reason": "one box damaged",
            "lines": [{"item_code": "PAPER-A4", "qty": 1}]}
    big = {**note, "lines": [{"item_code": "HVAC-SVC", "qty": 1, "rate": 800}]}
    assert "debit note limit" in blocked(ap.preview("enter_debit_note", big))
    p, out = run(ap, "enter_debit_note", note)
    assert out.outcome == "written", out.message
    note_id = out.target
    undo.append((ctl.erp, PI, note_id))
    # The note's rows keep their order, so the order's invoice list (and its checks) see it.
    assert [r.get("purchase_order") for r in ap.erp.get_doc(PI, note_id)["items"]] == [po]
    assert note_id in [i.id for i in ap.erp.list_invoices(po=po)]
    for s, action in ((ap, SUBMIT_FOR_APPROVAL), (ctl, APPROVE)):
        out = run(s, "workflow_action", {"doctype": PI, "name": note_id, "action": action})[1]
        assert out.outcome == "written", out.message
    assert svc("TREASURY").erp.outstanding(inv_id) == D(724)

    cancel = lambda s, name: run(s, "cancel_document", {  # noqa: E731
        "doctype": PI, "name": name, "reason": "live test cleanup"})
    assert cancel(ap, inv_id)[0].code is None  # not an invoice approver
    p, _ = cancel(ctl, inv_id)  # the approved debit note goes first
    assert p.code is None and any(note_id in c.message for c in p.checks)
    assert cancel(ctl, note_id)[1].outcome == "written"
    assert cancel(ctl, inv_id)[1].outcome == "written"
    assert ctl.erp.get_invoice(inv_id).state == DocState.CANCELLED


def test_live_void_draft_invoice(created):
    """A draft invoice keyed by mistake is voided through cancel_document: it stays a
    draft with the erp_guard_cancelled flag and the reason, the workflow offers nothing on
    it, and the same bill number is then entered again. Both drafts end voided: the first
    by AP, who entered it, the second by the controller, an invoice approver."""
    po = _approved_order(created, [Line("HVAC-SVC", D(1), D(640))])
    ap = svc("AP")
    args = {"supplier": "TRANE U.S. INC", "bill_no": f"VOID-{uuid.uuid4().hex[:6]}",
            "bill_date": date.today().isoformat(),
            "lines": [{"po": po, "item_code": "HVAC-SVC", "qty": 1, "rate": 640}]}
    first = run(ap, "enter_supplier_invoice", args)[1]
    assert first.outcome == "written", first.message
    p = ap.preview("enter_supplier_invoice", args)
    assert p.code is None  # the same number again, while the first is live
    void = lambda name, s=ap: run(s, "cancel_document", {  # noqa: E731
        "doctype": PI, "name": name, "reason": "keyed twice"})[1]
    out = void(first.target)
    assert out.outcome == "written", out.message
    doc = ap.erp.get_doc(PI, first.target)
    assert (doc["docstatus"], doc["erp_guard_cancelled"], doc["erp_guard_reason"]) == (
        0, 1, "keyed twice")
    assert ap.erp.get_invoice(first.target).state == DocState.CANCELLED
    assert ap.erp.transitions(PI, first.target) == []
    again = run(ap, "enter_supplier_invoice", args)[1]
    assert again.outcome == "written", again.message
    out = void(again.target, svc("CONTROLLER"))
    assert out.outcome == "written", out.message
    assert svc("CONTROLLER").erp.get_invoice(again.target).state == DocState.CANCELLED


def test_live_pay_and_approve(created, undo):
    """Payments through the guarded tools, as treasury: more than is owed is refused and
    writes nothing; a payment from the policy's bank account is entered (a Pay to the
    supplier, paid_to as ERPNext maps it), can't be entered twice, is edited within what is
    owed, submitted, refused to the payer and approved by the manager, so nothing is owed;
    the payer can't cancel it, the manager can. A second draft is voided by the payer: it
    stays a draft with the erp_guard_cancelled flag, gets no workflow action, and no longer
    blocks paying the invoice. Everything is cancelled or voided afterwards; nothing is a
    history document."""
    po = _approved_order(created, [Line("HVAC-SVC", D(1), D(640))])
    ap, ctl, trs, mgr = svc("AP"), svc("CONTROLLER"), svc("TREASURY"), svc("MANAGER")
    out = run(ap, "enter_supplier_invoice", {
        "supplier": "TRANE U.S. INC", "bill_no": f"PAY-{uuid.uuid4().hex[:6]}",
        "bill_date": date.today().isoformat(),
        "lines": [{"po": po, "item_code": "HVAC-SVC", "qty": 1, "rate": 640}]})[1]
    assert out.outcome == "written", out.message
    inv = out.target
    undo.append((ctl.erp, PI, inv))
    for s, action in ((ap, SUBMIT_FOR_APPROVAL), (ctl, APPROVE)):
        out = run(s, "workflow_action", {"doctype": PI, "name": inv, "action": action})[1]
        assert out.outcome == "written", out.message
    assert trs.erp.outstanding(inv) == D(640)
    blocked = lambda p: {c.check for c in p.checks if c.level == "block"}  # noqa: E731
    pay = lambda amount: {"supplier": "TRANE U.S. INC", "notes": "live payment",  # noqa: E731
                          "invoices": [{"invoice": inv, "amount": amount}]}

    p, out = run(trs, "pay_supplier", pay(641))
    assert p.code is None and "amount owed" in blocked(p) and out.outcome == "refused"
    assert trs.erp.list_payments(invoice=inv) == []
    p, out = run(trs, "pay_supplier", pay(600))
    assert out.outcome == "written", out.message
    pe = out.target
    undo.append((mgr.erp, PE, pe))
    doc = trs.erp.get_doc(PE, pe)
    credit_to = trs.erp.get_doc(PI, inv)["credit_to"]
    assert (doc["payment_type"], doc["party_type"], doc["party"], doc["paid_from"],
            doc["paid_to"], doc.get("bank_account"), doc["remarks"]) == (
        "Pay", "Supplier", "TRANE U.S. INC", demo.BANK_ACCOUNT, credit_to, None,
        "live payment")
    assert "duplicate payment" in blocked(trs.preview("pay_supplier", pay(40)))

    upd = lambda amount: {"doctype": PE, "name": pe,  # noqa: E731
                          "fields": {"references": [{"allocated_amount": amount}]}}
    assert "amount owed" in blocked(trs.preview("update_document", upd(641)))
    assert run(trs, "update_document", upd(640))[1].outcome == "written"
    assert trs.erp.get_payment(pe).amount == D(640)
    assert float(trs.erp.get_doc(PE, pe)["paid_amount"]) == 640.0

    wf = lambda s, action: run(s, "workflow_action", {  # noqa: E731
        "doctype": PE, "name": pe, "action": action})
    assert wf(trs, SUBMIT_FOR_APPROVAL)[1].outcome == "written"
    p, out = wf(trs, APPROVE)
    assert "payment authority" in blocked(p) and out.outcome == "refused"
    out = wf(mgr, APPROVE)[1]
    assert out.outcome == "written", out.message
    got = trs.erp.get_payment(pe)
    assert (got.state, got.approver, got.payer) == (
        DocState.APPROVED, "manager@example.com", "treasury@example.com")
    assert trs.erp.outstanding(inv) == 0

    cancel = lambda s, name: run(s, "cancel_document", {  # noqa: E731
        "doctype": PE, "name": name, "reason": "live test cleanup"})
    p, out = cancel(trs, pe)
    assert "cancel payment" in blocked(p) and out.outcome == "refused"
    out = cancel(mgr, pe)[1]
    assert out.outcome == "written", out.message
    assert trs.erp.get_payment(pe).state == DocState.CANCELLED
    assert trs.erp.outstanding(inv) == D(640)

    out = run(trs, "pay_supplier", pay(640))[1]  # the cancelled one no longer counts
    assert out.outcome == "written", out.message
    draft = out.target
    out = cancel(trs, draft)[1]
    assert out.outcome == "written", out.message
    doc = trs.erp.get_doc(PE, draft)
    assert (doc["docstatus"], doc["erp_guard_cancelled"], doc["erp_guard_reason"]) == (
        0, 1, "live test cleanup")
    assert trs.erp.get_payment(draft).state == DocState.CANCELLED
    assert trs.erp.transitions(PE, draft) == []
    flow = trs.erp.get_meta(PE)["__workflow_docs"][0]
    assert {t.get("condition") for t in flow["transitions"]} == {
        "not doc.erp_guard_cancelled"}
    assert run(trs, "pay_supplier", pay(640))[0].code  # payable again; not written


def test_live_p2p_requests_and_quotes(undo):
    """Purchase request on its workflow, a request for quotation from it, a supplier quote
    answering it and one without; all cancelled afterwards. The requester sends the
    request for quotation (the seed grants Purchase User submit on it)."""
    req, mgr = svc("REQUESTER").erp, svc("MANAGER").erp
    mr = req.create_request(CC, [RequestLine("LAPTOP-14", D(5))], "new starters")
    assert (mr.requester, mr.cost_centre, mr.state, mr.lines) == (
        "requester@example.com", CC, DocState.DRAFT, (RequestLine("LAPTOP-14", D(5)),))
    req.update_draft("Material Request", mr.id, {"items": [{"qty": 6}]})
    req.workflow("Material Request", mr.id, SUBMIT_FOR_APPROVAL)
    mgr.workflow("Material Request", mr.id, APPROVE)
    undo.append((mgr, "Material Request", mr.id))
    mr = req.get_request(mr.id)
    assert (mr.state, mr.lines[0].qty) == (DocState.APPROVED, D(6))
    assert mr in req.list_requests(since=date.today())

    rfq = req.create_rfq(mr.id, ["TRANE U.S. INC", "HERC RENTALS INC"])
    req.submit_doc("Request for Quotation", rfq)
    undo.append((mgr, "Request for Quotation", rfq))
    q1 = req.create_quote("TRANE U.S. INC", [Line("LAPTOP-14", D(6), D(1100))], rfq,
                          date.today())
    req.submit_doc("Supplier Quotation", q1.id)
    undo.append((mgr, "Supplier Quotation", q1.id))
    q2 = req.create_quote("HERC RENTALS INC", [Line("LAPTOP-14", D(6), D(1180))], None,
                          date.today())
    req.submit_doc("Supplier Quotation", q2.id)
    undo.append((mgr, "Supplier Quotation", q2.id))
    assert (q1.rfq, q1.supplier_id, q1.lines) == (
        rfq, "TRANE U.S. INC", (Line("LAPTOP-14", D(6), D(1100)),))
    found = {q.id: q for q in req.list_quotes(item_codes=["LAPTOP-14"],
                                              since=date.today())}
    assert (found[q1.id].state, found[q2.id].rfq) == (DocState.SUBMITTED, None)


def test_live_request_to_order(undo, created):
    """The request-to-order steps through the guarded tools: a purchase request on its
    workflow (the requester can't approve it; the manager's limit covers its estimate), a
    request for quotation sent and three quotes answering it, all by the requester; then a
    $6,600 order made from the request and the lowest quote, built from ERPNext's mappers
    so each line links back to both. A rate above the quote and a quantity above the
    request are refused. Approved, the order shows on the request as ordered. A $27,500
    sole-source order keeps its reason, and a manager can't approve it. Everything is
    cancelled afterwards (the orders first: `created` is torn down before `undo`)."""
    req, mgr = svc("REQUESTER"), svc("MANAGER")
    others = [m["name"] for m in demo.seed_suppliers("master")
              if m["name"] not in ("TRANE U.S. INC", "HERC RENTALS INC")]
    suppliers = ["TRANE U.S. INC", "HERC RENTALS INC", others[0]]
    laptops = lambda qty, rate: [{"item_code": "LAPTOP-14", "qty": qty, "rate": rate}]  # noqa: E731
    blocked = lambda p: {c.check for c in p.checks if c.level == "block"}  # noqa: E731

    p, out = run(req, "raise_purchase_request", {
        "cost_centre": CC, "lines": [{"item_code": "LAPTOP-14", "qty": 31}],
        "notes": "live request to order"})
    assert out.outcome == "written", (out.message, blocked(p))
    mr = out.target
    undo.append((mgr.erp, "Material Request", mr))
    wf = {"doctype": "Material Request", "name": mr}
    assert run(req, "workflow_action", {**wf, "action": SUBMIT_FOR_APPROVAL})[1].outcome \
        == "written"
    p = req.preview("workflow_action", {**wf, "action": APPROVE})
    assert "request approval tier" in blocked(p)
    p, out = run(mgr, "workflow_action", {**wf, "action": APPROVE})
    assert out.outcome == "written", (out.message, blocked(p))
    assert req.erp.get_request(mr).state == DocState.APPROVED

    p, out = run(req, "request_quotes", {"request": mr, "suppliers": suppliers})
    assert out.outcome == "written", (out.message, blocked(p))
    rfq = out.target
    undo.append((mgr.erp, "Request for Quotation", rfq))
    p, out = run(req, "submit_document", {"doctype": "Request for Quotation", "name": rfq})
    assert out.outcome == "written", (out.message, blocked(p))
    quotes = {}
    for sup, rate in zip(suppliers, (1100, 1180, 1200), strict=True):
        p, out = run(req, "enter_supplier_quote", {
            "supplier": sup, "rfq": rfq, "quoted_on": date.today().isoformat(),
            "lines": [{"item_code": "LAPTOP-14", "qty": 31, "rate": rate}]})
        assert out.outcome == "written", (out.message, blocked(p))
        undo.append((mgr.erp, "Supplier Quotation", out.target))
        p, sub = run(req, "submit_document", {"doctype": "Supplier Quotation",
                                              "name": out.target})
        assert sub.outcome == "written", (sub.message, blocked(p))
        quotes[sup] = out.target
    q = req.erp.get_quote(quotes["TRANE U.S. INC"])
    assert (q.rfq, q.state, q.entered_by) == (rfq, DocState.SUBMITTED, "requester@example.com")

    order = {"supplier": "TRANE U.S. INC", "cost_centre": CC, "request": mr,
             "quotations": [quotes["TRANE U.S. INC"]]}
    assert "quote price" in blocked(req.preview("create_purchase_order",
                                                {**order, "lines": laptops(6, 1150)}))
    assert "request approved" in blocked(req.preview("create_purchase_order",
                                                     {**order, "lines": laptops(32, 1100)}))
    p, out = run(req, "create_purchase_order", {**order, "lines": laptops(6, 1100)})
    assert out.outcome == "written", (out.message, blocked(p))
    po = out.target
    created.append(po)
    [row] = req.erp.get_doc("Purchase Order", po)["items"]
    assert (row["material_request"], row["supplier_quotation"], row["qty"], row["rate"]) == (
        mr, quotes["TRANE U.S. INC"], 6, 1100)
    assert row["material_request_item"] and row["supplier_quotation_item"]
    o = req.erp.get_order(po)
    assert (o.request, o.quotations, o.total) == (mr, (quotes["TRANE U.S. INC"],), D(6600))
    assert run(req, "submit_for_approval", {"po": po})[1].outcome == "written"
    p, out = run(mgr, "approve_purchase_order", {"po": po})
    assert out.outcome == "written", (out.message, blocked(p))
    assert mgr.erp.get_doc("Material Request", mr)["items"][0]["ordered_qty"] == 6

    sole = {"supplier": "TRANE U.S. INC", "cost_centre": CC, "request": mr,
            "lines": laptops(25, 1100), "sole_source_reason": "live: only approved model"}
    assert not req.preview("create_purchase_order",
                           {**sole, "sole_source_reason": None}).blocked  # three quotes
    p, out = run(req, "create_purchase_order", sole)
    assert out.outcome == "written", (out.message, blocked(p))
    created.append(out.target)
    assert req.erp.get_order(out.target).sole_source_reason == "live: only approved model"
    assert run(req, "submit_for_approval", {"po": out.target})[1].outcome == "written"
    assert "sole source" in blocked(mgr.preview("approve_purchase_order", {"po": out.target}))
    # The request can't be cancelled while live orders are made from it (Ruling 27).
    p = mgr.preview("cancel_document", {"doctype": "Material Request", "name": mr,
                                        "reason": "live test"})
    assert "cancel" in blocked(p) and mgr.erp.get_doc("Material Request", mr)["docstatus"] == 1


def test_live_cancel_and_edit_request(undo):
    """An approved request no order uses is cancelled by its requester through
    cancel_document (ERPNext cancels it); a draft is edited through update_document by the
    person who raised it, only its quantities, and a draft can't be cancelled."""
    req, req2 = svc("REQUESTER"), svc("REQUESTER2")
    blocked = lambda p: {c.check for c in p.checks if c.level == "block"}  # noqa: E731
    p, out = run(req, "raise_purchase_request", {
        "cost_centre": CC, "lines": [{"item_code": "PAPER-A4", "qty": 2}]})
    assert out.outcome == "written", (out.message, blocked(p))
    mr = out.target
    undo.append((svc("MANAGER").erp, "Material Request", mr))
    upd = {"doctype": "Material Request", "name": mr, "fields": {"items": [{"qty": 3}]}}
    assert "edit after entry" in blocked(req2.preview("update_document", upd))
    p, out = run(req, "update_document", upd)
    assert out.outcome == "written", (out.message, blocked(p))
    assert req.erp.get_request(mr).lines == (RequestLine("PAPER-A4", D(3)),)
    cancel = {"doctype": "Material Request", "name": mr, "reason": "live test"}
    assert "cancel" in blocked(req.preview("cancel_document", cancel))  # a draft
    wf = {"doctype": "Material Request", "name": mr}
    assert run(req, "workflow_action", {**wf, "action": SUBMIT_FOR_APPROVAL})[1].outcome \
        == "written"
    assert run(svc("MANAGER"), "workflow_action", {**wf, "action": APPROVE})[1].outcome \
        == "written"
    assert "cancel" in blocked(req2.preview("cancel_document", cancel))  # not theirs
    p, out = run(req, "cancel_document", cancel)
    assert out.outcome == "written", (out.message, blocked(p))
    assert req.erp.get_doc("Material Request", mr)["docstatus"] == 2
    assert req.erp.get_request(mr).state == DocState.CANCELLED


def test_live_close_order(created):
    """ERPNext's own close, with the reason on the order; reopened afterwards so the
    cleanup can cancel it."""
    po = _approved_order(created, [Line("PAPER-A4", D(1), D(42))])
    req = svc("REQUESTER").erp
    o = req.close_order(po, "no longer needed")
    assert o.closed and o.status == Status.APPROVED
    assert req.get_doc("Purchase Order", po)["erp_guard_reason"] == "no longer needed"
    assert not [x for x in req.list_orders(cost_centre=CC) if x.id == po and not x.closed]
    req.call("erpnext.buying.doctype.purchase_order.purchase_order.update_status",
             status="Submitted", name=po)  # ERPNext's re-open
    assert not req.get_order(po).closed


def test_live_change_requests():
    """A change request waits for a second person; rejected, it changes nothing; approved,
    it's saved on the target. The approved change here sets a value the supplier already
    has. Change requests stay on record (no role cancels them). (Only ERPNext's Item
    Manager writes items, a role no demo user holds.)"""
    req2, mgr = svc("REQUESTER2").erp, svc("MANAGER").erp
    no = req2.create_change_request("Item", "PAPER-A4", {"is_stock_item": 0})
    cr = mgr.get_doc("ERP Guard Change Request", no)
    assert (cr["workflow_state"], cr["requested_by"]) == ("Pending Approval",
                                                          "requester2@example.com")
    mgr.apply_change_request(no, approve=False)
    cr = mgr.get_doc("ERP Guard Change Request", no)
    assert (cr["workflow_state"], cr["status"], cr["decided_by"]) == (
        "Rejected", "Rejected", "manager@example.com")
    assert mgr.get_doc("Item", "PAPER-A4")["is_stock_item"] == 1
    sup = mgr.get_doc("Supplier", "TRANE U.S. INC")
    yes = req2.create_change_request("Supplier", "TRANE U.S. INC",
                                     {"erp_guard_notes": sup["erp_guard_notes"]})
    mgr.apply_change_request(yes, approve=True)
    cr = mgr.get_doc("ERP Guard Change Request", yes)
    assert (cr["docstatus"], cr["workflow_state"], cr["status"], cr["decided_by"]) == (
        1, "Approved", "Applied", "manager@example.com")
    with pytest.raises(ErpError, match="pending"):
        mgr.apply_change_request(yes, approve=True)
    after = mgr.get_doc("Supplier", "TRANE U.S. INC")
    assert (after["modified_by"], after["modified"] > sup["modified"]) == (
        "manager@example.com", True)  # saved by the approver
    with pytest.raises(HardLimitError, match="bank"):
        req2.create_change_request("Supplier", "TRANE U.S. INC", {"default_bank_account": "x"})
