"""The ERPNext connector's purchase-to-pay documents over the fake ERPNext: drafts come
from ERPNext's mappers, keep only the requested lines, and read back as the pack's models.
The live tests (test_live_p2p_*) run the same against v16.52.0."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from conftest import MGR, REQ, TODAY
from fake_erpnext import FakeErpNext, connector

from erp_guard.connectors.base import ErpError
from erp_guard.connectors.erpnext import _doc_state, _keep_rows, _plain
from erp_guard.models import Line
from erp_guard.packs.p2p import APPROVE, P2P_OWNED, REJECT
from erp_guard.packs.p2p.models import (
    Allocation,
    DocState,
    InvoiceLine,
    ItemInfo,
    ReceiptLine,
    RequestLine,
)
from erp_guard.service import Service

D = Decimal
PO, PR, PI = "PUR-ORD-1", "MAT-PRE-1", "ACC-PINV-1"
SUP = "TRANE U.S. INC"
BANK = "Demo Operating Bank - LDU"
RCV, AP = "receiver@example.com", "ap@example.com"


CO = {"company": "Lakeside Demo Utilities"}


def _erp(fake, **kw):
    """The connector writing the pack's document types (a Service gives it the full scope
    rules; these are the ones this file writes)."""
    return connector(fake, writable=lambda d: d in P2P_OWNED, **kw)


def _row(doctype, item, qty, **kw):
    return {"doctype": doctype, "item_code": item, "qty": qty, "parenttype": "x", **kw}


@pytest.fixture
def fake():
    f = FakeErpNext(RCV)
    f.add({"doctype": "Purchase Order", "name": PO, "docstatus": 1, "owner": REQ,
           "supplier": SUP, "transaction_date": "2026-10-20", "status": "To Receive and Bill",
           "items": [{"item_code": "PAPER-A4", "qty": 4, "rate": 42},
                     {"item_code": "HVAC-SVC", "qty": 1, "rate": 640}]})
    f.add({**CO, "doctype": "Purchase Receipt", "name": PR, "docstatus": 1, "owner": RCV,
           "supplier": SUP, "posting_date": "2026-10-25", "is_return": 0,
           "items": [_row("Purchase Receipt Item", "PAPER-A4", 3, purchase_order=PO)]})
    f.add({**CO, "doctype": "Purchase Invoice", "name": PI, "docstatus": 1, "owner": AP,
           "workflow_state": "Approved", "supplier": SUP, "bill_no": "B-1",
           "bill_date": "2026-10-26", "posting_date": "2026-10-26", "is_return": 0,
           "outstanding_amount": 766.0, "erp_guard_approver": "controller@example.com",
           "items": [_row("Purchase Invoice Item", "PAPER-A4", 3, rate=42, purchase_order=PO,
                          purchase_receipt=PR),
                     _row("Purchase Invoice Item", "HVAC-SVC", 1, rate=640,
                          purchase_order=PO)]})
    f.mapped["make_purchase_receipt"] = {
        "doctype": "Purchase Receipt", "supplier": SUP, "posting_date": "2026-11-01",
        "items": [_row("Purchase Receipt Item", "PAPER-A4", 4, received_qty=0,
                       conversion_factor=12, purchase_order=PO),
                  _row("Purchase Receipt Item", "HVAC-SVC", 1, received_qty=0,
                       purchase_order=PO)]}
    f.mapped["make_purchase_return"] = {
        "doctype": "Purchase Receipt", "supplier": SUP, "posting_date": "2026-11-01",
        "is_return": 1, "return_against": PR,
        "items": [_row("Purchase Receipt Item", "PAPER-A4", -3, purchase_order=PO)]}
    return f


def _inserted(fake, doctype: str) -> dict:
    return next(b["doc"] for b in reversed(fake.method_calls("frappe.client.insert"))
                if b["doc"]["doctype"] == doctype)


@pytest.mark.parametrize("doc,workflow,state", [
    ({"docstatus": 2, "workflow_state": "Approved"}, True, DocState.CANCELLED),
    ({"docstatus": 1, "workflow_state": "Approved"}, True, DocState.APPROVED),
    ({"docstatus": 1}, True, DocState.APPROVED),  # submitted before the workflow existed
    ({"docstatus": 1}, False, DocState.SUBMITTED),
    ({"docstatus": 0, "workflow_state": "Pending Approval"}, True, DocState.PENDING),
    ({"docstatus": 0, "workflow_state": "Rejected"}, True, DocState.REJECTED),
    ({"docstatus": 0}, True, DocState.DRAFT),
    ({"docstatus": 0}, False, DocState.DRAFT),
])
def test_doc_state(doc, workflow, state):
    assert _doc_state(doc, workflow) == state


def test_keep_rows_keeps_named_first_rows_and_refuses_missing():
    rows = [{"k": "a", "q": 1}, {"k": "a", "q": 2}, {"k": "b", "q": 3}]
    assert _keep_rows(rows, {("a",): {"q": 9}}, lambda r: (r["k"],), "SRC") == [
        {"k": "a", "q": 9}]
    with pytest.raises(ErpError, match="Not on SRC: c"):
        _keep_rows(rows, {("c",): {}}, lambda r: (r["k"],), "SRC")


def test_plain_values():
    assert _plain({"d": date(2026, 1, 2), "n": [D("1.5")], "s": "x"}) == {
        "d": "2026-01-02", "n": [1.5], "s": "x"}


def test_receipt_from_order_keeps_requested_lines_and_submits(fake):
    r = _erp(fake).create_receipt(PO, [ReceiptLine(PO, "PAPER-A4", D(2))])
    sent = _inserted(fake, "Purchase Receipt")
    assert [(i["item_code"], i["qty"], i["received_qty"], i["stock_qty"]) for i in
            sent["items"]] == [("PAPER-A4", 2.0, 2.0, 24.0)]
    assert sent["ignore_pricing_rule"] == 1
    # One request inserts and submits: a refused submit can't leave a draft behind.
    assert sent["docstatus"] == 1 and len(fake.method_calls("frappe.client.insert")) == 1
    assert not fake.method_calls("frappe.client.submit")
    assert (r.state, r.lines, r.receiver) == (
        DocState.SUBMITTED, (ReceiptLine(PO, "PAPER-A4", D(2)),), RCV)


def test_return_receipt_is_negative_in_erpnext_positive_in_the_model(fake):
    erp = _erp(fake)
    r = erp.create_receipt(PO, [ReceiptLine(PO, "PAPER-A4", D(1))], is_return=True,
                           return_against=PR)
    sent = _inserted(fake, "Purchase Receipt")
    assert sent["items"][0]["qty"] == -1.0 and sent["docstatus"] == 1
    assert not fake.method_calls("frappe.client.submit")
    assert (r.is_return, r.return_against, r.lines[0].qty) == (True, PR, D(1))
    with pytest.raises(ErpError, match="names the receipt"):
        erp.create_receipt(PO, [], is_return=True)


def test_receipt_line_not_on_the_order_writes_nothing(fake):
    erp = _erp(fake)
    with pytest.raises(ErpError, match="Not on PUR-ORD-1: PUR-ORD-1 LAPTOP-14"):
        erp.create_receipt(PO, [ReceiptLine(PO, "LAPTOP-14", D(1))])
    with pytest.raises(ErpError, match="One line per order and item"):
        erp.create_receipt(PO, [ReceiptLine(PO, "PAPER-A4", D(1))] * 2)
    assert not fake.method_calls("frappe.client.insert")


def test_lists_read_rows_and_filter_by_order(fake):
    erp = _erp(fake)
    fake.add({**CO, "doctype": "Purchase Receipt", "name": "MAT-PRE-9", "docstatus": 1,
              "owner": RCV, "supplier": "OTHER", "posting_date": "2026-10-25",
              "items": [_row("Purchase Receipt Item", "TONER-K", 1, purchase_order="PO-9")]})
    assert [r.id for r in erp.list_receipts(po=PO)] == [PR]
    assert [r.id for r in erp.list_receipts(supplier_id="OTHER")] == ["MAT-PRE-9"]
    inv = erp.list_invoices(po=PO, supplier_id=SUP, since=date(2026, 10, 1))[0]
    assert (inv.id, inv.approver, inv.state, inv.total) == (
        PI, "controller@example.com", DocState.APPROVED, D(766))
    assert [ln.receipt for ln in inv.lines] == [PR, None]
    assert erp.list_invoices(since=date(2026, 11, 1)) == []
    assert erp.get_invoice(PI) == inv and erp.get_invoice("nope") is None
    assert erp.get_receipt(PR).lines == (ReceiptLine(PO, "PAPER-A4", D(3)),)


def test_invoice_merges_receipt_and_order_rows(fake):
    fake.user = AP
    fake.mapped["make_purchase_invoice"] = {
        "doctype": "Purchase Invoice", "supplier": SUP, "posting_date": "2026-11-01",
        "items": [_row("Purchase Invoice Item", "PAPER-A4", 3, rate=42, purchase_order=PO,
                       purchase_receipt=PR),
                  _row("Purchase Invoice Item", "HVAC-SVC", 1, rate=640, purchase_order=PO)]}
    inv = _erp(fake).create_invoice(SUP, "INV-7", date(2026, 10, 30), [
        InvoiceLine(PO, "PAPER-A4", D(2), D(41), PR), InvoiceLine(PO, "HVAC-SVC", D(1), D(640))],
        "two lines")
    sent = _inserted(fake, "Purchase Invoice")
    assert [(i["item_code"], i["qty"], i["rate"], i.get("purchase_receipt")) for i in
            sent["items"]] == [("PAPER-A4", 2.0, 41.0, PR), ("HVAC-SVC", 1.0, 640.0, None)]
    assert (sent["bill_no"], sent["bill_date"], sent["ignore_pricing_rule"]) == (
        "INV-7", "2026-10-30", 1)
    assert (inv.state, inv.entered_by, inv.total) == (DocState.DRAFT, AP, D(722))
    with pytest.raises(ErpError, match="at least one line"):
        _erp(fake).create_invoice(SUP, "X", date(2026, 10, 30), [], "")


def test_debit_note_is_negative_and_reduces_the_original(fake):
    fake.mapped["make_debit_note"] = {
        "doctype": "Purchase Invoice", "supplier": SUP, "posting_date": "2026-11-01",
        "bill_no": "B-1", "bill_date": "2026-10-26", "is_return": 1, "return_against": PI,
        "items": [_row("Purchase Invoice Item", "PAPER-A4", -3, rate=42, purchase_order=PO),
                  _row("Purchase Invoice Item", "HVAC-SVC", -1, rate=640, purchase_order=PO)]}
    note = _erp(fake).create_debit_note(PI, [InvoiceLine(PO, "PAPER-A4", D(1), D(42))],
                                             "damaged")
    sent = _inserted(fake, "Purchase Invoice")
    assert [(i["item_code"], i["qty"]) for i in sent["items"]] == [("PAPER-A4", -1.0)]
    assert (sent["update_outstanding_for_self"], sent["remarks"]) == (0, "damaged")
    assert (note.is_return, note.return_against, note.total) == (True, PI, D(42))


def test_payment_merges_references_and_sends_no_bank_fields(fake):
    fake.add({**fake.docs[("Purchase Invoice", PI)], "name": "ACC-PINV-2"})
    erp = _erp(fake, allowed_bank_values={BANK})

    def pe(inv, amount):
        return {"doctype": "Payment Entry", "payment_type": "Pay", "party_type": "Supplier",
                "party": SUP, "posting_date": "2026-11-01", "paid_from": BANK,
                "bank": "", "bank_account_no": "", "party_bank_account": None,
                "bank_account": None, "paid_amount": amount,
                "references": [{"doctype": "Payment Entry Reference", "parenttype": "x",
                                "reference_doctype": "Purchase Invoice",
                                "reference_name": inv, "allocated_amount": amount}]}
    fake.mapped["get_payment_entry"] = pe(PI, 766)
    # each invoice's own payment draft: the fake returns one draft, so swap it per call
    orig = fake._method

    def per_invoice(method, req, body):
        if method.endswith("get_payment_entry"):
            fake.mapped["get_payment_entry"] = pe(body["dn"], 766)
        return orig(method, req, body)
    fake._method = per_invoice
    pay = erp.create_payment(SUP, [Allocation(PI, D(500)), Allocation("ACC-PINV-2", D(66))],
                             BANK)
    sent = _inserted(fake, "Payment Entry")
    assert [(r["reference_name"], r["allocated_amount"]) for r in sent["references"]] == [
        (PI, 500.0), ("ACC-PINV-2", 66.0)]
    assert (sent["paid_amount"], sent["received_amount"], sent["paid_from"]) == (
        566.0, 566.0, BANK)
    assert (sent["payment_type"], sent["party_type"], sent["party"]) == (
        "Pay", "Supplier", SUP)
    assert not {"bank", "bank_account_no", "party_bank_account", "bank_account"} & set(sent)
    assert sent["reference_no"] == f"{PI}, ACC-PINV-2"
    assert (pay.amount, pay.state, pay.allocations[0]) == (
        D(566), DocState.DRAFT, Allocation(PI, D(500)))
    with pytest.raises(ErpError, match="at least one invoice"):
        erp.create_payment(SUP, [], BANK)


def test_outstanding_only_on_submitted_invoices(fake):
    erp = _erp(fake)
    assert erp.outstanding(PI) == D(766)
    fake.docs[("Purchase Invoice", PI)]["docstatus"] = 0
    assert erp.outstanding(PI) == 0
    with pytest.raises(ErpError, match="No Purchase Invoice"):
        erp.outstanding("nope")


def test_hold_and_release_through_the_invoice_methods(fake):
    erp = _erp(fake)
    assert erp.set_invoice_hold(PI, True, "price query").on_hold
    assert fake.method_calls("frappe.handler.run_doc_method")[-1] == {
        "dt": "Purchase Invoice", "dn": PI, "method": "block_invoice",
        "args": {"hold_comment": "price query"}}
    assert not erp.set_invoice_hold(PI, False, "").on_hold
    with pytest.raises(KeyError):  # the fake has no such invoice; ERPNext would refuse
        erp.set_invoice_hold("nope", True, "x")


def test_workflow_submit_cancel_and_update(fake):
    erp = _erp(fake)
    fake.workflows.add("Purchase Invoice")
    fake.add({"doctype": "Purchase Invoice", "name": "ACC-PINV-3", "docstatus": 0,
              "owner": AP, "workflow_state": "Draft", "supplier": SUP,
              "posting_date": "2026-11-01", "remarks": "",
              "items": [_row("Purchase Invoice Item", "PAPER-A4", 1, rate=42,
                             purchase_order=PO)]})
    with pytest.raises(ErpError, match="approval workflow"):
        erp.submit_doc("Purchase Invoice", "ACC-PINV-3")
    erp.submit_doc("Purchase Receipt", PR)  # no workflow: submitted directly
    erp.update_draft("Purchase Invoice", "ACC-PINV-3",
                     {"bill_no": "N-1", "bill_date": date(2026, 10, 31), "items": [{"qty": 2}]})
    saved = fake.method_calls("frappe.client.save")[-1]["doc"]
    assert (saved["bill_no"], saved["bill_date"], saved["items"][0]["qty"],
            saved["items"][0]["rate"]) == ("N-1", "2026-10-31", 2, 42)
    with pytest.raises(ErpError, match="rows"):
        erp.update_draft("Purchase Invoice", "ACC-PINV-3", {"items": []})
    with pytest.raises(ErpError, match="isn't submitted"):
        erp.cancel_doc("Purchase Invoice", "ACC-PINV-3", "draft")
    erp.workflow("Purchase Invoice", "ACC-PINV-3", "Submit for Approval")
    assert fake.docs[("Purchase Invoice", "ACC-PINV-3")]["workflow_state"] == "Pending Approval"
    with pytest.raises(ErpError, match="isn't a draft"):
        erp.update_draft("Purchase Invoice", PI, {"remarks": "x"})
    erp.cancel_doc("Purchase Invoice", PI, "wrong supplier")
    assert fake.docs[("Purchase Invoice", PI)]["docstatus"] == 2


def test_order_rejection_comment_and_cancel_go_through_the_order(fake):
    erp = _erp(fake)
    fake.add({"doctype": "Purchase Order", "name": "PUR-ORD-2", "docstatus": 0, "owner": REQ,
              "workflow_state": "Pending Approval", "supplier": SUP,
              "transaction_date": "2026-10-20", "items": []})
    fake.user = MGR
    erp.workflow("Purchase Order", "PUR-ORD-2", REJECT, "too much")
    d = fake.docs[("Purchase Order", "PUR-ORD-2")]
    assert (d["erp_guard_reason"], d["workflow_state"]) == ("too much", "Rejected")
    erp.cancel_doc("Purchase Order", "PUR-ORD-2", "gone")
    assert d["erp_guard_cancelled"] == 1  # a draft order: the v1 flag, nothing deleted


def test_close_order_and_order_links(fake):
    fake.docs[("Purchase Order", PO)]["items"][0].update(material_request="MAT-MR-1",
                                                         supplier_quotation="SQ-1")
    o = _erp(fake).close_order(PO, "complete")
    assert (o.closed, o.request, o.quotations) == (True, "MAT-MR-1", ("SQ-1",))
    assert fake.docs[("Purchase Order", PO)]["erp_guard_reason"] == "complete"


def test_requests_rfqs_and_quotes(fake):
    erp = _erp(fake)
    fake.user = REQ
    mr = erp.create_request("IT - LDU", [RequestLine("LAPTOP-14", D(5))], "")
    sent = _inserted(fake, "Material Request")
    assert (sent["material_request_type"], sent["items"][0]["cost_center"]) == (
        "Purchase", "IT - LDU")
    assert (mr.cost_centre, mr.state, mr.lines) == (
        "IT - LDU", DocState.DRAFT, (RequestLine("LAPTOP-14", D(5)),))
    fake.docs[("Material Request", mr.id)]["transaction_date"] = "2026-10-29"
    assert erp.get_request(mr.id).id == mr.id
    assert [r.id for r in erp.list_requests(since=date(2026, 10, 1))] == [mr.id]
    fake.mapped["make_request_for_quotation"] = {
        "doctype": "Request for Quotation", "items": [
            _row("Request for Quotation Item", "LAPTOP-14", 5)]}
    rfq = erp.create_rfq(mr.id, [SUP, "HERC RENTALS INC"])
    sent = _inserted(fake, "Request for Quotation")
    assert [s["supplier"] for s in sent["suppliers"]] == [SUP, "HERC RENTALS INC"]
    assert sent["subject"] and sent["message_for_supplier"]
    fake.mapped["make_supplier_quotation_from_rfq"] = {
        **CO, "doctype": "Supplier Quotation", "items": [
            _row("Supplier Quotation Item", "LAPTOP-14", 5, request_for_quotation=rfq)]}
    q = erp.create_quote(SUP, [Line("LAPTOP-14", D(5), D(1100))], rfq, date(2026, 10, 30))
    assert (q.rfq, q.supplier_id, q.quoted_on, q.lines) == (
        rfq, SUP, date(2026, 10, 30), (Line("LAPTOP-14", D(5), D(1100)),))
    q2 = erp.create_quote(SUP, [Line("TONER-K", D(2), D(80))], None, date(2026, 10, 30))
    assert q2.rfq is None and _inserted(fake, "Supplier Quotation")["ignore_pricing_rule"] == 1
    found = erp.list_quotes(item_codes=["LAPTOP-14"], since=date(2026, 10, 1))
    assert [x.id for x in found] == [q.id]
    assert erp.list_quotes(item_codes=[], since=date(2026, 10, 1)) == []


def test_item_info(fake):
    fake.add({"doctype": "Item", "name": "PAPER-A4", "is_stock_item": 1, "stock_uom": "Nos"})
    fake.add({"doctype": "Item", "name": "HVAC-SVC", "is_stock_item": 0, "stock_uom": "Nos"})
    assert _erp(fake).item_info(["PAPER-A4", "HVAC-SVC", "NOPE"]) == {
        "PAPER-A4": ItemInfo("PAPER-A4", True, "Nos"),
        "HVAC-SVC": ItemInfo("HVAC-SVC", False, "Nos")}
    assert _erp(fake).item_info([]) == {}


def test_change_request_submitted_then_applied_or_rejected(fake):
    erp = _erp(fake)
    fake.user = "requester2@example.com"
    fake.add({"doctype": "Supplier", "name": SUP, "supplier_name": SUP, "on_hold": 0})
    cr = erp.create_change_request("Supplier", SUP, {"on_hold": 1})
    doc = fake.docs[("ERP Guard Change Request", cr)]
    assert (json.loads(doc["fields_json"]), doc["requested_by"], doc["workflow_state"]) == (
        {"on_hold": 1}, "requester2@example.com", "Pending Approval")
    fake.user = MGR
    erp.apply_change_request(cr, approve=True)
    assert fake.docs[("Supplier", SUP)]["on_hold"] == 1
    assert (doc["status"], doc["decided_by"], doc["docstatus"]) == ("Applied", MGR, 1)
    with pytest.raises(ErpError, match="isn't pending"):
        erp.apply_change_request(cr, approve=False)
    no = erp.create_change_request("Supplier", SUP, {"on_hold": 0})
    erp.apply_change_request(no, approve=False)
    assert fake.docs[("ERP Guard Change Request", no)]["status"] == "Rejected"
    assert fake.docs[("Supplier", SUP)]["on_hold"] == 1
    assert [c["action"] for c in fake.method_calls(
        "frappe.model.workflow.apply_workflow")][-2:] == ["Submit for Approval", REJECT]
    with pytest.raises(ErpError, match="bank"):
        erp.create_change_request("Supplier", SUP, {"default_bank_account": "x"})
    assert APPROVE in [c["action"] for c in fake.method_calls(
        "frappe.model.workflow.apply_workflow")]


# --- review fixes: editable fields, payment totals, onboarding date, change requests ----

def _drafts(fake):
    """A draft invoice and a draft payment in the fake ERPNext."""
    fake.add({**CO, "doctype": "Purchase Invoice", "name": "ACC-PINV-D", "docstatus": 0,
              "owner": AP, "workflow_state": "Draft", "supplier": SUP,
              "posting_date": "2026-11-01", "update_stock": 0, "is_paid": 0,
              "items": [_row("Purchase Invoice Item", "PAPER-A4", 1, rate=42,
                             purchase_order=PO)]})
    fake.add({**CO, "doctype": "Payment Entry", "name": "ACC-PAY-D", "docstatus": 0,
              "owner": "treasury@example.com", "workflow_state": "Draft", "party": SUP,
              "posting_date": "2026-11-01", "paid_from": BANK, "paid_amount": 766.0,
              "received_amount": 766.0, "payment_type": "Pay",
              "references": [
                  {"doctype": "Payment Entry Reference", "reference_doctype": PI_DT,
                   "reference_name": PI, "allocated_amount": 700.0},
                  {"doctype": "Payment Entry Reference", "reference_doctype": PI_DT,
                   "reference_name": "ACC-PINV-2", "allocated_amount": 66.0}]})
    return "ACC-PINV-D", "ACC-PAY-D"


def _memory_drafts(erp):
    from conftest import TODAY
    inv = erp.as_user(AP).create_invoice(
        "SUP-CANON", "M-1", TODAY, [InvoiceLine("PO-HIST1", "TONER-K", D(1), D(90))], "")
    erp.workflow(PI_DT, inv.id, "Submit for Approval")
    erp.as_user("controller@example.com").workflow(PI_DT, inv.id, APPROVE)
    draft = erp.as_user(AP).create_invoice(
        "SUP-CANON", "M-2", TODAY, [InvoiceLine("PO-HIST1", "TONER-K", D(1), D(90))], "")
    pay = erp.create_payment("SUP-CANON", [Allocation(inv.id, D(50))], BANK)
    return draft.id, pay.id


PI_DT, PE_DT = "Purchase Invoice", "Payment Entry"
REFUSED = [
    (PI_DT, {"update_stock": 1}, "update_stock"),
    (PI_DT, {"is_paid": 1}, "is_paid"),
    (PI_DT, {"ignore_pricing_rule": 0}, "ignore_pricing_rule"),
    (PI_DT, {"items": [{"qty": 2, "purchase_order": "PO-X"}]}, "items[].purchase_order"),
    (PE_DT, {"payment_type": "Receive"}, "payment_type"),
    (PE_DT, {"party": "OTHER"}, "party"),
    (PE_DT, {"paid_amount": 1}, "paid_amount"),
    (PE_DT, {"references": [{"reference_doctype": "Purchase Order"}, {}]},
     "references[].reference_doctype"),
    (PE_DT, {"paid_from": "Petty Cash - LDU"}, "paid_from"),
    ("Purchase Order", {"items": [{"qty": 9}]}, "items"),
]


@pytest.mark.parametrize("doctype,fields,bad", REFUSED)
def test_draft_updates_outside_editable_refused_on_both_connectors(fake, erp, doctype,
                                                                     fields, bad):
    msg = f"{doctype} drafts can't have these changed: {bad}"
    ext = _erp(fake, allowed_bank_values={BANK})
    names = dict(zip((PI_DT, PE_DT), _drafts(fake), strict=True))
    with pytest.raises(ErpError) as exc:
        ext.update_draft(doctype, names.get(doctype, PO), fields)
    assert str(exc.value) == msg
    assert not fake.method_calls("frappe.client.save")
    mem = dict(zip((PI_DT, PE_DT), _memory_drafts(erp), strict=True))
    with pytest.raises(ErpError) as exc2:
        erp.update_draft(doctype, mem.get(doctype, "PO-HIST1"), fields)
    assert str(exc2.value) == str(exc.value)


def test_payment_allocation_edit_sets_the_paid_amount(fake):
    _, pay = _drafts(fake)
    _erp(fake, allowed_bank_values={BANK}).update_draft(
        PE_DT, pay, {"references": [{"allocated_amount": 500}, {"allocated_amount": 66}],
                     "remarks": "partial"})
    saved = fake.method_calls("frappe.client.save")[-1]["doc"]
    assert [r["allocated_amount"] for r in saved["references"]] == [500, 66]
    assert (saved["paid_amount"], saved["received_amount"], saved["remarks"]) == (
        566.0, 566.0, "partial")


def test_payment_referencing_anything_but_invoices_is_refused(fake):
    _, pay = _drafts(fake)
    fake.docs[(PE_DT, pay)]["references"][1]["reference_doctype"] = "Purchase Order"
    with pytest.raises(ErpError, match="references Purchase Order ACC-PINV-2, not only"):
        _erp(fake).get_payment(pay)


def test_invoice_and_debit_note_never_update_stock_or_pay_at_entry(fake):
    fake.mapped["make_purchase_invoice"] = {
        "doctype": "Purchase Invoice", "supplier": SUP, "update_stock": 1, "is_paid": 1,
        "posting_date": "2026-10-30",
        "items": [_row("Purchase Invoice Item", "HVAC-SVC", 1, rate=640, purchase_order=PO)]}
    _erp(fake).create_invoice(SUP, "X-1", date(2026, 10, 30),
                              [InvoiceLine(PO, "HVAC-SVC", D(1), D(640))], "")
    sent = _inserted(fake, PI_DT)
    assert (sent["update_stock"], sent["is_paid"]) == (0, 0)
    fake.mapped["make_debit_note"] = {**fake.mapped["make_purchase_invoice"], "is_return": 1,
                                      "return_against": PI}
    _erp(fake).create_debit_note(PI, [InvoiceLine(PO, "HVAC-SVC", D(1), D(640))], "")
    sent = _inserted(fake, PI_DT)
    assert (sent["update_stock"], sent["is_paid"], sent["is_return"]) == (0, 0, 1)


def test_onboarded_on_only_for_suppliers_onboarded_through_erp_guard(fake, erp):
    fake.add({"doctype": "Supplier", "name": "OLD CO", "supplier_name": "OLD CO",
              "creation": "2026-10-31 09:00:00"})
    fake.add({"doctype": "Supplier", "name": "NEW CO", "supplier_name": "NEW CO",
              "creation": "2026-10-31 09:00:00", "erp_guard_onboarded_by": REQ})
    ext = _erp(fake)
    assert ext.get_supplier("OLD CO").onboarded_on is None
    assert ext.get_supplier("NEW CO").onboarded_on == date(2026, 10, 31)
    from conftest import TODAY
    assert erp.get_supplier("SUP-HERC").onboarded_on is None
    assert erp.create_supplier("NEW CO", None, "", REQ).onboarded_on == TODAY


def test_change_request_target_save_fails_request_stays_pending(fake, erp):
    ext = _erp(fake)
    fake.add({"doctype": "Supplier", "name": SUP, "supplier_name": SUP, "on_hold": 0})
    cr = ext.create_change_request("Supplier", SUP, {"on_hold": 1})
    orig = fake._method

    def failing_save(method, req, body):
        if method == "frappe.client.save":
            import httpx
            return httpx.Response(417, json={"exception": "ValidationError: no"})
        return orig(method, req, body)
    fake._method = failing_save
    with pytest.raises(ErpError, match="417"):
        ext.apply_change_request(cr, approve=True)
    doc = fake.docs[("ERP Guard Change Request", cr)]
    assert (doc["workflow_state"], doc.get("status"), doc.get("decided_by")) == (
        "Pending Approval", None, None)
    assert APPROVE not in [c["action"] for c in fake.method_calls(
        "frappe.model.workflow.apply_workflow")]
    # memory: the same order, the same outcome
    mcr = erp.create_change_request("Supplier", "SUP-HERC", {"on_hold": 1})
    del erp.suppliers["SUP-HERC"]
    with pytest.raises(ErpError, match="No Supplier SUP-HERC"):
        erp.apply_change_request(mcr, approve=True)
    assert erp.change_requests[mcr]["state"] == DocState.PENDING


def test_memory_refuses_what_erpnext_refuses(erp):
    from conftest import TODAY
    with pytest.raises(ErpError, match="at least one line"):
        erp.create_invoice("SUP-CANON", "E-1", TODAY, [], "")
    with pytest.raises(ErpError, match="at least one invoice"):
        erp.create_payment("SUP-CANON", [], BANK)
    inv = erp.create_invoice("SUP-CANON", "E-2", TODAY,
                             [InvoiceLine("PO-HIST1", "TONER-K", D(1), D(90))], "")
    with pytest.raises(ErpError, match=r"bank details are never written: \['paid_from'\]"):
        erp.create_payment("SUP-CANON", [Allocation(inv.id, D(1))], "Petty Cash - LDU")
    req = erp.create_request("IT", [RequestLine("LAPTOP-14", D(1))], "")
    rfq = erp.create_rfq(req.id, ["SUP-CANON"])
    with pytest.raises(ErpError, match=f"Not on {rfq}: TONER-K"):
        erp.create_quote("SUP-CANON", [Line("TONER-K", D(1), D(80))], rfq, TODAY)
    with pytest.raises(ErpError, match="bank details are never written"):
        erp.create_change_request("Supplier", "SUP-HERC", {"default_bank_account": "x"})


def test_cancel_document_on_a_receipt_runs_the_packs_receipt_checks(fake, policy):
    """The generic tool reaches the pack: the receipt an approved invoice refers to is
    refused; one nothing refers to is cancelled by its receiver."""
    svc = Service(connector(fake), policy, today=lambda: TODAY)
    args = {"doctype": "Purchase Receipt", "name": PR, "reason": "entered in error"}
    p = svc.preview("cancel_document", args)
    msgs = [r for r in p.checks if r.check == "cancel receipt" and r.level == "block"]
    assert p.code is None and msgs and PI in msgs[0].message
    fake.add({**CO, "doctype": "Purchase Receipt", "name": "MAT-PRE-2", "docstatus": 1,
              "owner": RCV, "supplier": SUP, "posting_date": "2026-10-26", "is_return": 0,
              "items": [_row("Purchase Receipt Item", "PAPER-A4", 1, purchase_order=PO)]})
    args["name"] = "MAT-PRE-2"
    p = svc.preview("cancel_document", args)
    assert p.code, [r.message for r in p.checks if r.level == "block"]
    out = svc.execute("cancel_document", args, p.code)
    assert out.outcome == "written", out.message
    assert fake.docs[("Purchase Receipt", "MAT-PRE-2")]["docstatus"] == 2
