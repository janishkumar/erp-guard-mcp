"""Purchase-to-pay documents on the in-memory ERP: the connector protocol the pack's checks
and eval run on."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import DIR, MGR, REQ, REQ2, TODAY, po_args

from erp_guard.connectors.base import ErpError
from erp_guard.connectors.memory import MemoryErp, line
from erp_guard.models import Status
from erp_guard.packs.p2p import APPROVE, REJECT, REVISE, SUBMIT_FOR_APPROVAL, WITHDRAW
from erp_guard.packs.p2p.models import (
    Allocation,
    DocState,
    InvoiceLine,
    ItemInfo,
    ReceiptLine,
    Request,
    RequestLine,
)

RCV = "receiver@example.com"
AP = "ap@example.com"
CTL = "controller@example.com"
TRS = "treasury@example.com"
BANK = "Demo Operating Bank - LDU"
PO = "PO-HIST1"  # approved: 10 x LAPTOP-14 @ 1200, 20 x TONER-K @ 90, SUP-CANON
D = Decimal
PI, PE = "Purchase Invoice", "Payment Entry"


def _approved_invoice(erp: MemoryErp, qty=4, rate=1200, bill_no="INV-1"):
    inv = erp.as_user(AP).create_invoice(
        "SUP-CANON", bill_no, TODAY, [InvoiceLine(PO, "LAPTOP-14", D(qty), D(rate))], "")
    erp.as_user(AP).workflow(PI, inv.id, SUBMIT_FOR_APPROVAL)
    erp.as_user(CTL).workflow(PI, inv.id, APPROVE)
    return erp.get_invoice(inv.id)


def test_receipt_against_po_reads_back(erp):
    r = erp.as_user(RCV).create_receipt(PO, [ReceiptLine(PO, "LAPTOP-14", D(6))])
    assert (r.state, r.receiver, r.supplier_id, r.posted) == (
        DocState.SUBMITTED, RCV, "SUP-CANON", TODAY)
    assert erp.list_receipts(po=PO) == [r] == erp.list_receipts(supplier_id="SUP-CANON")
    assert erp.get_receipt(r.id) == r
    assert erp.list_receipts(po="PO-OTHER") == []
    ret = erp.as_user(RCV).create_receipt(PO, [ReceiptLine(PO, "LAPTOP-14", D(1))],
                                          is_return=True, return_against=r.id)
    assert (ret.is_return, ret.return_against, ret.lines[0].qty) == (True, r.id, D(1))
    assert erp.list_receipts(po=PO) == [r, ret]


def test_receipt_against_unknown_po_fails(erp):
    with pytest.raises(ErpError):
        erp.create_receipt("PO-NOPE", [ReceiptLine("PO-NOPE", "LAPTOP-14", D(1))])
    with pytest.raises(ErpError, match="return"):
        erp.create_receipt(PO, [ReceiptLine(PO, "LAPTOP-14", D(1))], is_return=True)


def test_invoice_draft_then_approved_is_owed(erp):
    ap = erp.as_user(AP)
    inv = ap.create_invoice("SUP-CANON", "INV-1", TODAY,
                            [InvoiceLine(PO, "LAPTOP-14", D(4), D(1200)),
                             InvoiceLine(PO, "TONER-K", D(2), D(90))], "first batch")
    assert (inv.state, inv.entered_by, inv.total, inv.remarks) == (
        DocState.DRAFT, AP, D(4980), "first batch")
    assert erp.outstanding(inv.id) == 0  # nothing is owed on a draft
    ap.workflow(PI, inv.id, SUBMIT_FOR_APPROVAL)
    assert erp.get_invoice(inv.id).state == DocState.PENDING
    erp.as_user(CTL).workflow(PI, inv.id, APPROVE)
    done = erp.get_invoice(inv.id)
    assert (done.state, done.approver) == (DocState.APPROVED, CTL)
    assert erp.outstanding(inv.id) == done.total == D(4980)
    assert erp.list_invoices(po=PO) == [done] == erp.list_invoices(supplier_id="SUP-CANON")
    assert erp.list_invoices(since=TODAY + timedelta(days=1)) == []


def test_payment_allocation_reduces_outstanding(erp):
    inv = _approved_invoice(erp)
    pay = erp.as_user(TRS).create_payment("SUP-CANON", [Allocation(inv.id, D(1000))],
                                          BANK)
    assert (pay.state, pay.payer, pay.amount, pay.paid_from) == (
        DocState.DRAFT, TRS, D(1000), BANK)
    assert erp.outstanding(inv.id) == D(4800)  # a draft payment pays nothing yet
    erp.as_user(TRS).workflow(PE, pay.id, SUBMIT_FOR_APPROVAL)
    erp.as_user(MGR).workflow(PE, pay.id, APPROVE)
    assert erp.get_payment(pay.id).approver == MGR
    assert erp.outstanding(inv.id) == D(3800)
    assert erp.list_payments(invoice=inv.id) == [erp.get_payment(pay.id)]
    assert erp.list_payments(supplier_id="SUP-HERC") == []
    assert erp.list_payments(since=TODAY) == erp.list_payments()
    erp.as_user(MGR).cancel_doc(PE, pay.id, "wrong amount")
    assert erp.get_payment(pay.id).state == DocState.CANCELLED
    assert erp.outstanding(inv.id) == D(4800)


def test_debit_note_reduces_outstanding(erp):
    inv = _approved_invoice(erp)
    note = erp.as_user(AP).create_debit_note(
        inv.id, [InvoiceLine(PO, "LAPTOP-14", D(1), D(1200))], "one damaged")
    assert (note.is_return, note.return_against, note.total, note.state) == (
        True, inv.id, D(1200), DocState.DRAFT)
    assert (note.supplier_id, note.bill_no) == (inv.supplier_id, inv.bill_no)
    erp.as_user(AP).workflow(PI, note.id, SUBMIT_FOR_APPROVAL)
    erp.as_user(CTL).workflow(PI, note.id, APPROVE)
    assert erp.outstanding(inv.id) == D(3600)
    assert erp.outstanding(note.id) == 0
    with pytest.raises(ErpError):
        erp.create_debit_note("PI-NOPE", [], "")


def test_hold_and_release(erp):
    inv = _approved_invoice(erp)
    held = erp.as_user(CTL).set_invoice_hold(inv.id, True, "price query")
    assert held.on_hold and erp.get_invoice(inv.id).on_hold
    assert not erp.as_user(CTL).set_invoice_hold(inv.id, False, "resolved").on_hold


def test_as_user_shares_state(erp):
    rcv, ap = erp.as_user(RCV), erp.as_user(AP)
    r = rcv.create_receipt(PO, [ReceiptLine(PO, "TONER-K", D(5))])
    inv = ap.create_invoice("SUP-CANON", "B-7", TODAY,
                            [InvoiceLine(PO, "TONER-K", D(5), D(90), r.id)], "")
    for view in (erp, rcv, ap, erp.as_user(TRS)):
        assert view.get_receipt(r.id) == r and view.get_invoice(inv.id) == inv
    assert (rcv.whoami(), ap.whoami()) == (RCV, AP)
    assert inv.lines[0].receipt == r.id
    # ids keep counting across views
    assert ap.create_invoice("SUP-CANON", "B-8", TODAY,
                             [InvoiceLine(PO, "TONER-K", D(1), D(90))], "").id != inv.id


def test_workflow_transitions(erp):
    inv = erp.as_user(AP).create_invoice("SUP-CANON", "W-1", TODAY,
                                         [InvoiceLine(PO, "TONER-K", D(1), D(90))], "")
    with pytest.raises(ErpError, match="Approve"):
        erp.workflow(PI, inv.id, APPROVE)  # not pending
    erp.workflow(PI, inv.id, SUBMIT_FOR_APPROVAL)
    erp.as_user(CTL).workflow(PI, inv.id, REJECT, "wrong rate")
    assert erp.get_invoice(inv.id).state == DocState.REJECTED
    erp.workflow(PI, inv.id, REVISE)
    assert erp.get_invoice(inv.id).state == DocState.DRAFT
    with pytest.raises(ErpError, match="Withdraw"):  # orders only
        erp.workflow(PI, inv.id, WITHDRAW)
    with pytest.raises(ErpError, match="No Purchase Invoice"):
        erp.workflow(PI, "PI-NOPE", SUBMIT_FOR_APPROVAL)
    with pytest.raises(ErpError, match="workflow"):
        erp.workflow("Purchase Receipt", "x", APPROVE)


def test_workflow_on_orders_matches_v1(erp, svc):
    from conftest import run
    _, out = run(svc, "create_purchase_order", po_args())
    po = out.target
    erp.workflow("Purchase Order", po, SUBMIT_FOR_APPROVAL)
    assert erp.get_order(po).status == Status.PENDING
    erp.workflow("Purchase Order", po, WITHDRAW)
    assert erp.get_order(po).status == Status.DRAFT
    erp.workflow("Purchase Order", po, SUBMIT_FOR_APPROVAL)
    erp.as_user(MGR).workflow("Purchase Order", po, APPROVE)
    assert (erp.get_order(po).status, erp.get_order(po).approver) == (Status.APPROVED, MGR)
    closed = erp.close_order(po, "all done")
    assert closed.closed and erp.get_order(po).closed


def test_request_rfq_and_quotes(erp):
    req = erp.create_request("IT", [RequestLine("LAPTOP-14", D(5))], "new starters")
    assert req == Request(req.id, REQ, "IT", DocState.DRAFT,
                          (RequestLine("LAPTOP-14", D(5)),), TODAY)
    assert req.estimated_value({"LAPTOP-14": D(1200)}) == D(6000)
    assert req.estimated_value({}) == 0
    erp.workflow("Material Request", req.id, SUBMIT_FOR_APPROVAL)
    erp.as_user(MGR).workflow("Material Request", req.id, APPROVE)
    assert erp.get_request(req.id).state == DocState.APPROVED
    assert erp.list_requests() == [erp.get_request(req.id)]
    assert erp.list_requests(since=TODAY + timedelta(days=1)) == []
    rfq = erp.create_rfq(req.id, ["SUP-HERC", "SUP-CANON"])
    erp.submit_doc("Request for Quotation", rfq)
    q1 = erp.create_quote("SUP-CANON", [line("LAPTOP-14", 5, 1150)], rfq, TODAY)
    q2 = erp.create_quote("SUP-HERC", [line("TONER-K", 5, 80)], None,
                          TODAY - timedelta(days=100))
    assert (q1.rfq, q1.state, q1.supplier_id) == (rfq, DocState.DRAFT, "SUP-CANON")
    erp.submit_doc("Supplier Quotation", q1.id)
    found = erp.list_quotes(item_codes=["LAPTOP-14"], since=TODAY - timedelta(days=90))
    assert [(q.id, q.state) for q in found] == [(q1.id, DocState.SUBMITTED)]
    assert erp.list_quotes(item_codes=["TONER-K"], since=TODAY - timedelta(days=90)) == []
    assert erp.list_quotes(item_codes=["TONER-K"], since=TODAY - timedelta(days=200)) == [q2]
    with pytest.raises(ErpError):
        erp.create_rfq("MR-NOPE", ["SUP-HERC"])


def test_submit_doc_refuses_workflow_types(erp):
    inv = erp.create_invoice("SUP-CANON", "S-1", TODAY,
                             [InvoiceLine(PO, "TONER-K", D(1), D(90))], "")
    with pytest.raises(ErpError, match="workflow"):
        erp.submit_doc(PI, inv.id)
    with pytest.raises(ErpError, match="No Supplier Quotation"):
        erp.submit_doc("Supplier Quotation", "SQ-NOPE")


def test_cancel_doc(erp):
    r = erp.as_user(RCV).create_receipt(PO, [ReceiptLine(PO, "LAPTOP-14", D(1))])
    erp.cancel_doc("Purchase Receipt", r.id, "wrong order")
    assert erp.get_receipt(r.id).state == DocState.CANCELLED
    with pytest.raises(ErpError, match="already cancelled"):
        erp.cancel_doc("Purchase Receipt", r.id, "again")
    draft = erp.create_invoice("SUP-CANON", "C-1", TODAY,
                               [InvoiceLine(PO, "TONER-K", D(1), D(90))], "")
    with pytest.raises(ErpError, match="submitted"):
        erp.cancel_doc(PI, draft.id, "a draft isn't cancelled")  # as in ERPNext
    erp.cancel_doc("Purchase Order", PO, "orders: as cancel_order")
    assert erp.get_order(PO).status == Status.CANCELLED


def test_update_draft(erp):
    inv = erp.as_user(AP).create_invoice(
        "SUP-CANON", "U-1", TODAY, [InvoiceLine(PO, "LAPTOP-14", D(4), D(1200)),
                                    InvoiceLine(PO, "TONER-K", D(2), D(90))], "")
    erp.update_draft(PI, inv.id, {"bill_no": "U-2", "bill_date": "2026-10-20",
                                  "remarks": "fixed", "items": [{"qty": 3}, {"rate": "85"}]})
    got = erp.get_invoice(inv.id)
    assert (got.bill_no, got.bill_date, got.remarks) == ("U-2", date(2026, 10, 20), "fixed")
    assert [(ln.qty, ln.rate) for ln in got.lines] == [(D(3), D(1200)), (D(2), D(85))]
    with pytest.raises(ErpError, match="rows"):
        erp.update_draft(PI, inv.id, {"items": [{"qty": 1}]})
    with pytest.raises(ErpError, match=r"can't have these changed: items\[\].is_stock_item"):
        erp.update_draft(PI, inv.id, {"items": [{"is_stock_item": 0}, {}]})
    erp.workflow(PI, inv.id, SUBMIT_FOR_APPROVAL)
    erp.as_user(CTL).workflow(PI, inv.id, APPROVE)
    with pytest.raises(ErpError, match="draft"):
        erp.update_draft(PI, inv.id, {"remarks": "too late"})
    pay = erp.as_user(TRS).create_payment("SUP-CANON", [Allocation(inv.id, D(100))], BANK)
    erp.update_draft(PE, pay.id, {"references": [{"allocated_amount": 250}],
                                  "remarks": "kept aside"})
    assert erp.get_payment(pay.id).amount == D(250)
    req = erp.create_request("IT", [RequestLine("TONER-K", D(5))], "")
    erp.update_draft("Material Request", req.id, {"items": [{"qty": 7}]})
    assert erp.get_request(req.id).lines[0].qty == D(7)


def test_item_info(erp):
    erp.add_item_info(ItemInfo("CONSULT-HR", False, "Hour"))
    info = erp.item_info(["LAPTOP-14", "CONSULT-HR", "NOPE"])
    assert info == {"LAPTOP-14": ItemInfo("LAPTOP-14", True, "Nos"),
                    "CONSULT-HR": ItemInfo("CONSULT-HR", False, "Hour")}
    assert "CONSULT-HR" in erp.item_codes()


def test_change_request_applies_on_approval(erp):
    cr = erp.as_user(REQ2).create_change_request("Item", "LAPTOP-14", {"is_stock_item": 0})
    assert erp.change_requests[cr]["state"] == DocState.PENDING
    assert erp.item_info(["LAPTOP-14"])["LAPTOP-14"].is_stock  # not until approved
    erp.as_user(DIR).apply_change_request(cr, approve=True)
    assert not erp.item_info(["LAPTOP-14"])["LAPTOP-14"].is_stock
    rec = erp.change_requests[cr]
    assert (rec["state"], rec["status"], rec["decided_by"], rec["requested_by"]) == (
        DocState.APPROVED, "Applied", DIR, REQ2)
    with pytest.raises(ErpError, match="pending"):
        erp.apply_change_request(cr, approve=False)
    sup = erp.create_change_request("Supplier", "SUP-HERC", {"on_hold": 1, "tax_id": "9"})
    no = erp.create_change_request("Supplier", "SUP-HERC", {"disabled": 1})
    erp.as_user(DIR).apply_change_request(no, approve=False)
    erp.as_user(DIR).apply_change_request(sup, approve=True)
    s = erp.get_supplier("SUP-HERC")
    assert (s.on_hold, s.tax_id, s.disabled) == (True, "9", False)
    assert erp.change_requests[no]["status"] == "Rejected"
    with pytest.raises(ErpError):
        erp.create_change_request("Item", "NOPE", {"disabled": 1})


def test_supplier_onboarded_on(erp):
    s = erp.create_supplier("NEW CO LLC", None, "", REQ)
    assert s.onboarded_on == TODAY
    assert erp.get_supplier("SUP-HERC").onboarded_on is None  # seeded: predates the ERP


def test_po_duplicate_item_lines_blocked(svc):
    args = po_args()
    args["lines"].append({"item_code": "SALT-TON", "qty": 2, "rate": 1000})
    p = svc.preview("create_purchase_order", args)
    assert p.blocked and p.code is None
    dup = [r for r in p.checks if r.check == "duplicate_line"]
    assert dup and dup[0].level == "block" and "SALT-TON" in dup[0].message
    assert not svc.preview("create_purchase_order", po_args()).blocked


def test_po_amend_duplicate_item_lines_blocked(svc):
    from conftest import run
    _, out = run(svc, "create_purchase_order", po_args())
    p = svc.preview("amend_purchase_order", {"po": out.target, "lines": [
        {"item_code": "SALT-TON", "qty": 1, "rate": 1000},
        {"item_code": "SALT-TON", "qty": 1, "rate": 990}]})
    assert p.blocked and any(r.check == "duplicate_line" for r in p.checks)


def test_connectors_implement_the_p2p_protocol():
    from erp_guard.connectors.base import P2P_WRITE_METHODS, P2PConnector
    from erp_guard.connectors.erpnext import ErpNext
    names = {n for n in dir(P2PConnector) if not n.startswith("_")}
    assert names >= P2P_WRITE_METHODS
    for impl in (MemoryErp, ErpNext):
        assert not [n for n in names if not callable(getattr(impl, n, None))
                    and n != "name"], impl
