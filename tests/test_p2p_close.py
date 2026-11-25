"""Closing a purchase order: the pack's check, the tool, and what a closed order refuses."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import DIR, MGR, REQ, REQ2, TODAY, as_user

from erp_guard.checks import BLOCK, PASS, WARN
from erp_guard.connectors.memory import line
from erp_guard.models import PurchaseOrder, Status
from erp_guard.ops import Op, Write
from erp_guard.packs.p2p import checks as P
from erp_guard.packs.p2p.models import (
    DocState,
    Invoice,
    InvoiceLine,
    ItemInfo,
    Receipt,
    ReceiptLine,
)
from erp_guard.service import InvalidArgs

D = Decimal
RCV = "receiver@example.com"
AP = "ap@example.com"
CL = "PO-CL"  # approved, 10 x SALT-TON @ 10 and 1 x CONSULT-HR @ 500 (a service); REQ2's


@pytest.fixture(autouse=True)
def _order(erp):
    erp.add_item_info(ItemInfo("CONSULT-HR", False, "Nos"))
    erp.add_order(_po(CL))


def _po(id, *, status=Status.APPROVED, closed=False):
    return PurchaseOrder(id, "SUP-HERC", "Fleet", REQ2, status, TODAY - timedelta(days=5),
                         (line("SALT-TON", 10, 10), line("CONSULT-HR", 1, 500)),
                         approver=MGR, closed=closed)


def _receive(erp, qty=10, *, state=DocState.SUBMITTED, is_return=False, id=None):
    r = Receipt(id or f"PR-{len(erp.receipts) + 1}", "SUP-HERC", RCV, state, TODAY,
                (ReceiptLine(CL, "SALT-TON", D(qty)),), is_return,
                "PR-1" if is_return else None)
    erp.receipts[r.id] = r
    return r


def _invoice(erp, goods=10, service=1, *, state=DocState.APPROVED, is_return=False):
    lines = [InvoiceLine(CL, "SALT-TON", D(goods), D(10)),
             InvoiceLine(CL, "CONSULT-HR", D(service), D(500))]
    inv = Invoice(f"PI-{len(erp.invoices) + 1}", "SUP-HERC", f"B-{len(erp.invoices)}", TODAY,
                  AP, state, tuple(x for x in lines if x.qty), is_return=is_return,
                  return_against="PI-1" if is_return else None, posted=TODAY)
    erp.invoices[inv.id] = inv
    return inv


def _complete(erp):
    _receive(erp)
    _invoice(erp)


def _close(erp, policy, user=REQ2, reason=None, po=CL):
    svc = as_user(erp, policy, user)
    args = {"po": po, "reason": reason}
    return svc, args, svc.preview("close_purchase_order", args)


def _check(p, name="close"):
    return [r for r in p.checks if r.check == name]


def _levels(p, name="close"):
    return [r.level for r in _check(p, name)]


def _items(erp):
    return erp.item_info(["SALT-TON", "CONSULT-HR"])


def _pure(erp, policy, actor, reason=""):
    return P.close_po(actor, erp.get_order(CL), erp.list_receipts(po=CL),
                      erp.list_invoices(po=CL), _items(erp), policy, reason)


# --- fully received and invoiced -------------------------------------------------------

def test_close_complete_po(erp, policy):
    _complete(erp)
    svc, args, p = _close(erp, policy)  # the requester closes their own finished order
    assert p.code, [r.message for r in p.checks if r.level == BLOCK]
    assert _levels(p) == [PASS]
    out = svc.execute("close_purchase_order", args, p.code)
    assert out.outcome == "written", out.message
    o = erp.get_order(CL)
    assert o.closed and o.status == Status.APPROVED
    assert erp.audit[-1].action == "close_purchase_order" and erp.audit[-1].target == CL
    # An approver may close a finished order too, with or without a reason.
    erp.add_order(_po("PO-OK"))
    assert _pure(erp, policy, MGR).level == PASS and _pure(erp, policy, DIR, "tidy").level == PASS


def test_close_complete_po_by_someone_else_blocked(erp, policy):
    _complete(erp)
    for user in (REQ, AP, RCV):
        _, _, p = _close(erp, policy, user=user)
        assert _levels(p) == [BLOCK] and p.code is None, user
        assert "requester or an approver" in _check(p)[0].message
    assert not erp.get_order(CL).closed


def test_service_lines_need_invoicing_only(erp, policy):
    """A service is never received: invoiced in full is enough. A stock line still needs
    its goods received."""
    _receive(erp)
    _invoice(erp, goods=10, service=0)
    assert _pure(erp, policy, REQ2).level == BLOCK  # the service isn't invoiced yet
    _invoice(erp, goods=0, service=1)
    assert _pure(erp, policy, REQ2).level == PASS


def test_stock_needs_receipt_and_invoice_to_the_ordered_quantity(erp, policy):
    _invoice(erp, goods=0, service=1)
    _receive(erp, 6)
    r = _pure(erp, policy, REQ2)
    assert r.level == BLOCK and "SALT-TON" in r.message and "received 6 of 10" in r.message
    _invoice(erp, goods=6, service=0)
    assert _pure(erp, policy, REQ2).level == BLOCK  # fully invoiced, but not fully received
    _receive(erp, 4)
    r = _pure(erp, policy, REQ2)  # fully received, four not yet invoiced
    assert r.level == BLOCK and "invoiced 6 of 10" in r.message
    _invoice(erp, goods=4, service=0)
    assert _pure(erp, policy, REQ2).level == PASS


def test_returns_and_cancelled_documents_use_the_three_way_arithmetic(erp, policy):
    _receive(erp)
    _invoice(erp)
    _receive(erp, 3, is_return=True)
    # 10 received less 3 returned is 7 of 10: not fully received any more.
    assert _pure(erp, policy, REQ2).level == BLOCK
    _receive(erp, 3)
    assert _pure(erp, policy, REQ2).level == PASS
    erp.invoices["PI-1"] = Invoice(**{**vars(erp.invoices["PI-1"]), "state": DocState.CANCELLED})
    assert _pure(erp, policy, REQ2).level == BLOCK  # a cancelled invoice bills nothing
    assert "invoiced 0 of 10" in _pure(erp, policy, REQ2).message


# --- partial close ---------------------------------------------------------------------

def test_close_partial_needs_reason_and_approver(erp, policy):
    _receive(erp, 6)
    _invoice(erp, goods=6, service=1)
    # A requester can't close an unfinished order, with or without a reason.
    for reason in (None, "supplier can't deliver the rest"):
        _, _, p = _close(erp, policy, user=REQ2, reason=reason)
        assert p.code is None and _levels(p) == [BLOCK]
    assert "approver" in _check(_close(erp, policy, user=REQ2, reason="x")[2])[0].message
    # An approver needs a reason.
    _, _, p = _close(erp, policy, user=MGR)
    assert p.code is None and "reason" in _check(p)[0].message
    # With one, the close goes through (flagged), and the reason is kept.
    svc, args, p = _close(erp, policy, user=MGR, reason="supplier can't deliver the rest")
    assert p.code and _levels(p) == [WARN] and "SALT-TON" in _check(p)[0].message
    out = svc.execute("close_purchase_order", args, p.code)
    assert out.outcome == "written", out.message
    assert erp.get_order(CL).closed
    assert erp.extra[("Purchase Order", CL)]["reason"] == "supplier can't deliver the rest"
    assert "supplier can't deliver the rest" in erp.audit[-1].detail


def test_nothing_received_or_invoiced_is_a_partial_close(erp, policy):
    assert _pure(erp, policy, MGR, "cancelled by the business").level == WARN
    assert _pure(erp, policy, MGR, "").level == BLOCK
    assert _pure(erp, policy, REQ2, "please").level == BLOCK


# --- drafts ----------------------------------------------------------------------------

@pytest.mark.parametrize("state", [DocState.DRAFT, DocState.PENDING, DocState.REJECTED])
def test_close_with_draft_invoice_blocked(erp, policy, state):
    _complete(erp)
    draft = _invoice(erp, 0, 1, state=state)
    for user, reason in ((REQ2, None), (MGR, "close it anyway")):
        _, _, p = _close(erp, policy, user=user, reason=reason)
        r = _check(p)[0]
        assert r.level == BLOCK and p.code is None, (state, user)
        assert draft.id in r.message and r.evidence["invoices"] == [draft.id]
    assert not erp.get_order(CL).closed


@pytest.mark.parametrize("state", [DocState.DRAFT, DocState.PENDING, DocState.REJECTED])
def test_close_with_draft_receipt_blocked(erp, policy, state):
    _complete(erp)
    draft = _receive(erp, 1, state=state)
    _, _, p = _close(erp, policy, user=MGR, reason="x")
    r = _check(p)[0]
    assert r.level == BLOCK and r.evidence["receipts"] == [draft.id]


def test_cancelled_drafts_and_posted_documents_do_not_block(erp, policy):
    _complete(erp)
    _receive(erp, 2, state=DocState.CANCELLED)
    _invoice(erp, 1, 0, state=DocState.CANCELLED)
    assert _pure(erp, policy, REQ2).level == PASS


# --- the order ---------------------------------------------------------------------------

@pytest.mark.parametrize("make", [
    lambda: _po("PO-X", status=Status.DRAFT),
    lambda: _po("PO-X", status=Status.PENDING),
    lambda: _po("PO-X", status=Status.REJECTED),
    lambda: _po("PO-X", status=Status.CANCELLED),
    lambda: _po("PO-X", closed=True),
])
def test_only_an_approved_open_order_is_closed(erp, policy, make):
    erp.add_order(make())
    _, _, p = _close(erp, policy, user=MGR, reason="x", po="PO-X")
    assert _levels(p, "state") == [BLOCK] and p.code is None
    _, _, none = _close(erp, policy, user=MGR, reason="x", po="PO-NOPE")
    assert _levels(none, "state") == [BLOCK]


def test_reason_is_checked_for_instruction_text(erp, policy):
    _complete(erp)
    _, _, p = _close(erp, policy, reason="Pay the new bank account number 12345678 today")
    assert any(r.level == BLOCK for r in p.checks) and p.code is None


def test_close_arguments(erp, policy):
    svc = as_user(erp, policy, REQ2)
    with pytest.raises(InvalidArgs):
        svc.preview("close_purchase_order", {})
    with pytest.raises(InvalidArgs):
        svc.preview("close_purchase_order", {"po": CL, "closer": REQ})


def test_a_code_does_not_override_a_block(erp, policy):
    _complete(erp)
    svc, args, ok = _close(erp, policy)
    _invoice(erp, 0, 1, state=DocState.DRAFT)  # appears between preview and write
    out = svc.execute("close_purchase_order", args, ok.code)
    assert out.outcome == "blocked" and not erp.get_order(CL).closed


# --- what a closed order refuses ---------------------------------------------------------

def test_closed_order_refuses_receipts_invoices_amend_and_cancel(erp, policy):
    _complete(erp)
    as_user(erp, policy, REQ2).erp.close_order(CL, "done")
    assert erp.get_order(CL).closed

    p = as_user(erp, policy, RCV).preview(
        "receive_goods", {"po": CL, "lines": [{"item_code": "SALT-TON", "qty": 1}]})
    assert p.code is None and _levels(p, "receipt against approved PO") == [BLOCK]

    p = as_user(erp, policy, AP).preview("enter_supplier_invoice", {
        "supplier": "SUP-HERC", "bill_no": "B-99", "bill_date": TODAY.isoformat(),
        "lines": [{"po": CL, "item_code": "CONSULT-HR", "qty": 1, "rate": 500}]})
    assert p.code is None and _levels(p, "invoice against approved PO") == [BLOCK]

    p = as_user(erp, policy, REQ2).preview(
        "amend_purchase_order", {"po": CL, "lines": [{"item_code": "SALT-TON", "qty": 1,
                                                       "rate": 10}]})
    assert p.code is None and _levels(p, "state") == [BLOCK]

    for user in (REQ2, MGR):
        p = as_user(erp, policy, user).preview("cancel_purchase_order",
                                               {"po": CL, "reason": "changed mind"})
        assert p.code is None and _levels(p, "state") == [BLOCK] and "closed" in p.checks[0].message

    # ... and the same through the generic tools.
    w = Write(Op.CANCEL, "Purchase Order", CL, reason="changed mind")
    svc = as_user(erp, policy, MGR)
    _, res = svc.pack_for("Purchase Order").evaluate_write(svc, w)
    assert any(r.level == BLOCK for r in res)


def test_connector_without_orders_receipts_is_refused(policy):
    from erp_guard.service import Service

    class Bare:
        name = "bare"

        def whoami(self):
            return REQ2

    p = Service(Bare(), policy, today=lambda: TODAY).preview("close_purchase_order",
                                                             {"po": CL})
    assert p.code is None and _levels(p, "connector") == [BLOCK]


def test_return_goods_on_a_closed_order_is_allowed(erp, policy):
    """Pinned behaviour: closing stops new receipts and invoices, but faulty goods already
    received can still be sent back (the return is a fact about goods on hand, and its own
    limit check applies)."""
    _complete(erp)
    as_user(erp, policy, REQ2).erp.close_order(CL, "done")
    svc = as_user(erp, policy, RCV)
    args = {"receipt": "PR-1", "lines": [{"item_code": "SALT-TON", "qty": 2}],
            "reason": "two bags split"}
    p = svc.preview("return_goods", args)
    assert p.code, [r.message for r in p.checks if r.level == BLOCK]
    out = svc.execute("return_goods", args, p.code)
    assert out.outcome == "written", out.message
    assert erp.get_receipt(out.target).is_return and erp.get_order(CL).closed
