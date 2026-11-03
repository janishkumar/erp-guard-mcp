"""Supplier payments: the pack's checks, pay_supplier, and the payment's workflow, edits and
cancellation as the generic tools reach them."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import DIR, MGR, REQ, REQ2, TODAY, as_user
from fake_erpnext import FakeErpNext, connector

from erp_guard.checks import BLOCK, PASS, WARN
from erp_guard.connectors.memory import line
from erp_guard.models import PurchaseOrder, Status, Supplier
from erp_guard.ops import Op, Write
from erp_guard.packs.p2p import APPROVE, REJECT, REVISE, SUBMIT_FOR_APPROVAL
from erp_guard.packs.p2p import checks as P
from erp_guard.packs.p2p.models import Allocation, DocState, Invoice, InvoiceLine, Payment
from erp_guard.service import InvalidArgs, Service

D = Decimal
AP = "ap@example.com"
CTL = "controller@example.com"
TRS = "treasury@example.com"
PE = "Payment Entry"
BANK = "Demo Operating Bank - LDU"
GOODS = "PO-GOODS"  # approved, SUP-HERC, requested by REQ2


def _po(id, supplier, total, requester=REQ2):
    return PurchaseOrder(id, supplier, "Fleet", requester, Status.APPROVED,
                         TODAY - timedelta(days=20), (line("CONSULT-HR", 1, total),),
                         approver=MGR)


def _inv(erp, id, total, *, supplier="SUP-HERC", po=GOODS, approver=CTL,
         state=DocState.APPROVED, **kw) -> Invoice:
    """An invoice already in the ERP (entered by AP, approved by the controller)."""
    inv = Invoice(id, supplier, f"B-{id}", TODAY - timedelta(days=3), AP, state,
                  (InvoiceLine(po, "CONSULT-HR", D(1), D(total)),), approver=approver, **kw)
    erp.invoices[id] = inv
    return inv


@pytest.fixture(autouse=True)
def _docs(erp):
    erp.add_order(_po(GOODS, "SUP-HERC", 100000))
    _inv(erp, "PI-A", 100)
    _inv(erp, "PI-B", 250)


def _args(*allocs, supplier="SUP-HERC", notes=None):
    allocs = allocs or (("PI-A", 100),)
    return {"supplier": supplier, "invoices": [{"invoice": i, "amount": a} for i, a in allocs],
            "notes": notes}


def _blocks(res):
    return [r for r in res if r.level == BLOCK]


def _checks(res, name):
    return [r for r in res if r.check == name]


def _level(res, name):
    return [r.level for r in _checks(res, name)]


def _pay(erp, policy, args=None, user=TRS):
    svc = as_user(erp, policy, user)
    args = args or _args()
    return svc, args, svc.preview("pay_supplier", args)


def _paid(erp, policy, args=None) -> Payment:
    svc, args, p = _pay(erp, policy, args)
    assert p.code, [r.message for r in p.checks if r.level == BLOCK]
    out = svc.execute("pay_supplier", args, p.code)
    assert out.outcome == "written", out.message
    return erp.get_payment(out.target)


def _wf(erp, policy, user, pay_id, action, comment=""):
    svc = as_user(erp, policy, user)
    pack = svc.pack_for(PE)
    w = Write(Op.WORKFLOW, PE, pay_id, action=action, reason=comment)
    return svc, pack, w, pack.evaluate_write(svc, w)[1]


def _step(erp, policy, user, pay_id, action, comment=""):
    svc, pack, w, res = _wf(erp, policy, user, pay_id, action, comment)
    assert not _blocks(res), [r.message for r in _blocks(res)]
    pack.apply_write(svc, w)


def _submitted(erp, policy, args=None) -> Payment:
    pay = _paid(erp, policy, args)
    _step(erp, policy, TRS, pay.id, SUBMIT_FOR_APPROVAL)
    return erp.get_payment(pay.id)


def _approved(erp, policy, args=None, approver=MGR) -> Payment:
    pay = _submitted(erp, policy, args)
    _step(erp, policy, approver, pay.id, APPROVE)
    return erp.get_payment(pay.id)


def _payment(id, *allocs, state=DocState.DRAFT, supplier="SUP-HERC", payer=TRS,
             posted=TODAY, unallocated=0, paid_from=BANK):
    return Payment(id, supplier, payer, state, posted, paid_from,
                   tuple(Allocation(i, D(a)) for i, a in allocs),
                   unallocated=D(unallocated))


# --- the pure checks -------------------------------------------------------------------

def test_payable_invoices(erp):
    by_id = {"PI-A": erp.get_invoice("PI-A"), "PI-B": erp.get_invoice("PI-B")}
    owed = {"PI-A": D(100), "PI-B": D(250)}
    alloc = lambda *a: [Allocation(i, D(x)) for i, x in a]  # noqa: E731
    res = P.payable_invoices("SUP-HERC", alloc(("PI-A", 100), ("PI-B", 50)), by_id, owed)
    assert [r.check for r in res] == ["pay only approved invoices", "amount owed"]
    assert {r.level for r in res} == {PASS}
    over = P.payable_invoices("SUP-HERC", alloc(("PI-A", "100.01")), by_id, owed)
    assert _level(over, "amount owed") == [BLOCK] and "PI-A" in over[-1].message
    # Another supplier's invoice, one on hold, a draft, a debit note, a paid one, a missing one.
    other = {**by_id, "PI-A": replace(by_id["PI-A"], supplier_id="SUP-CANON")}
    held = {**by_id, "PI-A": replace(by_id["PI-A"], on_hold=True)}
    draft = {**by_id, "PI-A": replace(by_id["PI-A"], state=DocState.PENDING)}
    note = {**by_id, "PI-A": replace(by_id["PI-A"], is_return=True)}
    for invs, owed_a, why in ((other, 100, "SUP-CANON"), (held, 100, "on hold"),
                              (draft, 100, "Pending Approval"), (note, 100, "debit note"),
                              (by_id, 0, "nothing is owed"), ({}, 0, "No purchase invoice")):
        res = P.payable_invoices("SUP-HERC", alloc(("PI-A", 1)), invs,
                                 {**owed, "PI-A": D(owed_a)})
        blocked = _checks(res, "pay only approved invoices")[0]
        assert blocked.level == BLOCK and why in blocked.message, why
    # No invoice at all, or money left unallocated: an advance.
    for res in (P.payable_invoices("SUP-HERC", [], by_id, owed),
                P.payable_invoices("SUP-HERC", alloc(("PI-A", 50)), by_id, owed,
                                   unallocated=D(25))):
        assert _level(res, "pay only approved invoices") == [BLOCK]
        assert "advance payments are not allowed" in res[0].message


def test_duplicate_payment():
    new = [Allocation("PI-A", D(100))]
    assert {r.level for r in P.duplicate_payment(new, [], "SUP-HERC", TODAY, 30)} == {PASS}
    for state in (DocState.DRAFT, DocState.PENDING, DocState.REJECTED, DocState.APPROVED):
        res = P.duplicate_payment(new, [_payment("PE-1", ("PI-A", 40), state=state)],
                                  "SUP-HERC", TODAY, 30)
        assert _level(res, "duplicate payment") == [BLOCK] and "PE-1" in res[0].message
    gone = _payment("PE-1", ("PI-A", 100), state=DocState.CANCELLED)
    assert BLOCK not in {r.level for r in P.duplicate_payment(new, [gone], "SUP-HERC",
                                                              TODAY, 30)}
    # The same amount to the same supplier for another invoice, recently: a warning.
    same = _payment("PE-2", ("PI-B", 100), state=DocState.APPROVED,
                    posted=TODAY - timedelta(days=30))
    res = P.duplicate_payment(new, [same], "SUP-HERC", TODAY, 30)
    assert _level(res, "duplicate payment: amount") == [WARN] and "PE-2" in res[-1].message
    for quiet in (replace(same, posted=TODAY - timedelta(days=31)),
                  replace(same, supplier_id="SUP-CANON"),
                  replace(same, allocations=(Allocation("PI-B", D(99)),)),
                  replace(same, state=DocState.CANCELLED)):
        res = P.duplicate_payment(new, [quiet], "SUP-HERC", TODAY, 30)
        assert {r.level for r in res} == {PASS}, quiet


def test_payer_separation(erp):
    by_id = {"PI-A": erp.get_invoice("PI-A")}
    po_by_id = {GOODS: erp.get_order(GOODS)}
    assert P.payer_separation(TRS, by_id, po_by_id).level == PASS
    res = P.payer_separation(CTL, by_id, po_by_id)  # approved the invoice
    assert res.level == BLOCK and "approved PI-A" in res.message
    res = P.payer_separation(REQ2, by_id, po_by_id)  # requested the order
    assert res.level == BLOCK and f"requested {GOODS}" in res.message


def test_payment_authority(policy):
    pay = replace(_payment("PE-1", ("PI-A", 80000)), state=DocState.PENDING)
    assert P.payment_authority(DIR, pay, policy).level == PASS
    mgr = P.payment_authority(MGR, pay, policy)
    assert mgr.level == BLOCK and "50,000" in mgr.message and "director" in mgr.message
    assert P.payment_authority(TRS, replace(pay, payer=DIR), policy).level == BLOCK
    own = P.payment_authority(DIR, replace(pay, payer=DIR), policy)
    assert own.level == BLOCK and "payer" in own.message
    assert P.payment_authority(CTL, pay, policy).level == BLOCK  # no payment limit


def test_new_supplier_rule(policy):
    new = Supplier("SUP-NEW", "NEW CO", onboarded_by=REQ, onboarded_on=TODAY - timedelta(days=14))
    assert P.new_supplier(DIR, new, [], TODAY, policy).level == PASS
    res = P.new_supplier(MGR, new, [], TODAY, policy)
    assert res.level == BLOCK and "director" in res.message
    paid = _payment("PE-0", ("PI-X", 10), state=DocState.APPROVED, supplier="SUP-NEW")
    assert P.new_supplier(MGR, new, [paid], TODAY, policy).level == PASS  # paid before
    for not_yet in (replace(paid, state=DocState.PENDING), replace(paid, state=DocState.CANCELLED)):
        assert P.new_supplier(MGR, new, [not_yet], TODAY, policy).level == BLOCK
    old = replace(new, onboarded_on=TODAY - timedelta(days=91))
    assert P.new_supplier(MGR, old, [], TODAY, policy).level == PASS
    seeded = replace(new, onboarded_by=None, onboarded_on=None)  # established master data
    assert P.new_supplier(MGR, seeded, [], TODAY, policy).level == PASS


def test_paying_account(policy):
    assert P.paying_account(BANK, policy).level == PASS
    res = P.paying_account("Petty Cash - LDU", policy)
    assert res.level == BLOCK and BANK in res.message


# --- pay_supplier ----------------------------------------------------------------------

def test_pay_approved_invoice(erp, policy):
    svc, args, p = _pay(erp, policy, _args(("PI-A", 100), ("PI-B", 50), notes="October run"))
    assert p.code, [r.message for r in p.checks if r.level == BLOCK]
    assert "$150.00" in p.summary and BANK in p.summary
    out = svc.execute("pay_supplier", args, p.code)
    assert out.outcome == "written", out.message
    pay = erp.get_payment(out.target)
    assert (pay.state, pay.payer, pay.paid_from, pay.supplier_id, pay.allocations) == (
        DocState.DRAFT, TRS, BANK, "SUP-HERC",
        (Allocation("PI-A", D(100)), Allocation("PI-B", D(50))))
    assert erp.extra[(PE, pay.id)]["remarks"] == "October run"
    assert erp.outstanding("PI-A") == D(100)  # a draft pays nothing
    _step(erp, policy, TRS, pay.id, SUBMIT_FOR_APPROVAL)
    _step(erp, policy, MGR, pay.id, APPROVE)
    assert (erp.get_payment(pay.id).state, erp.get_payment(pay.id).approver) == (
        DocState.APPROVED, MGR)
    assert (erp.outstanding("PI-A"), erp.outstanding("PI-B")) == (0, D(200))


def test_pay_needs_treasury_and_valid_arguments(erp, policy):
    _, _, p = _pay(erp, policy, user=AP)
    assert _level(p.checks, "role") == [BLOCK] and p.code is None
    svc = as_user(erp, policy, TRS)
    for bad in ({**_args(), "invoices": "PI-A"},
                {**_args(), "invoices": [{"invoice": "PI-A"}]},
                {**_args(), "invoices": [{"invoice": "PI-A", "amount": 0}]},
                {**_args(), "invoices": [{"invoice": "PI-A", "amount": "1.005"}]},
                {**_args(), "invoices": [{"invoice": "PI-A", "amount": 1, "paid_to": "x"}]},
                _args(("PI-A", 10), ("PI-A", 20)),
                {**_args(), "paid_from": "Petty Cash - LDU"},
                {**_args(), "payment_type": "Internal Transfer"}):
        with pytest.raises(InvalidArgs):
            svc.preview("pay_supplier", bad)


def test_overpay_blocked(erp, policy):
    _, _, p = _pay(erp, policy, _args(("PI-A", "100.01")))
    assert _level(p.checks, "amount owed") == [BLOCK] and p.code is None
    assert erp.payments == {}


def test_pay_twice_blocked(erp, policy):
    first = _paid(erp, policy)
    _, _, p = _pay(erp, policy, _args(("PI-A", 1)))  # the first is still a draft
    dup = _checks(p.checks, "duplicate payment")
    assert [r.level for r in dup] == [BLOCK] and first.id in dup[0].message
    assert p.code is None
    _step(erp, policy, TRS, first.id, SUBMIT_FOR_APPROVAL)
    _step(erp, policy, MGR, first.id, APPROVE)
    _, _, p = _pay(erp, policy)  # fully paid now
    assert {"duplicate payment", "pay only approved invoices"} <= {
        r.check for r in _blocks(p.checks)}


def test_same_amount_for_another_invoice_warns(erp, policy):
    _inv(erp, "PI-C", 100)
    first = _paid(erp, policy)
    _, _, p = _pay(erp, policy, _args(("PI-C", 100)))
    warn = _checks(p.checks, "duplicate payment: amount")
    assert [r.level for r in warn] == [WARN] and first.id in warn[0].message
    assert p.code  # a warning only


def test_pay_on_hold_blocked(erp, policy):
    _inv(erp, "PI-A", 100, on_hold=True)
    _, _, p = _pay(erp, policy)
    res = _checks(p.checks, "pay only approved invoices")
    assert [r.level for r in res] == [BLOCK] and "on hold" in res[0].message
    erp.suppliers["SUP-HERC"] = replace(erp.suppliers["SUP-HERC"], on_hold=True)
    _inv(erp, "PI-A", 100)
    _, _, p = _pay(erp, policy)
    assert [r.check for r in _blocks(p.checks)] == ["supplier_usable"]


def test_pay_only_approved_invoices(erp, policy):
    _inv(erp, "PI-D", 100, state=DocState.DRAFT)
    _inv(erp, "PI-O", 100, supplier="SUP-CANON")
    for ref in ("PI-D", "PI-O", "PI-NOPE"):
        _, _, p = _pay(erp, policy, _args((ref, 10)))
        assert _level(p.checks, "pay only approved invoices") == [BLOCK], ref


def test_advance_payment_blocked(erp, policy):
    _, _, p = _pay(erp, policy, {"supplier": "SUP-HERC", "invoices": []})
    res = _checks(p.checks, "pay only approved invoices")
    assert [r.level for r in res] == [BLOCK]
    assert "advance payments are not allowed" in res[0].message and p.code is None
    # A payment with money left unallocated (entered outside erp-guard) isn't approved.
    erp.payments["PE-ADV"] = _payment("PE-ADV", ("PI-A", 50), state=DocState.PENDING,
                                      unallocated=25)
    res = _wf(erp, policy, MGR, "PE-ADV", APPROVE)[3]
    assert "advance payments are not allowed" in " ".join(r.message for r in _blocks(res))


def test_payer_is_invoice_approver_blocked(erp, policy):
    _inv(erp, "PI-T", 100, approver=TRS)
    _, _, p = _pay(erp, policy, _args(("PI-T", 100)))
    assert _level(p.checks, "payer separation") == [BLOCK] and p.code is None
    erp.add_order(_po("PO-TRS", "SUP-HERC", 500, requester=TRS))
    _inv(erp, "PI-U", 100, po="PO-TRS")
    _, _, p = _pay(erp, policy, _args(("PI-U", 100)))
    assert _level(p.checks, "payer separation") == [BLOCK]


def test_wrong_bank_account_blocked(erp, policy):
    pay = _submitted(erp, policy)
    erp._set(PE, pay.id, paid_from="Petty Cash - LDU")  # changed outside erp-guard
    res = _wf(erp, policy, MGR, pay.id, APPROVE)[3]
    assert _level(res, "paying account") == [BLOCK]


def test_payment_race_blocked_at_write(erp, policy):
    svc, args, p = _pay(erp, policy)  # A, previewed
    assert p.code
    other = erp.as_user(TRS).create_payment("SUP-HERC", [Allocation("PI-A", D(100))], BANK)
    out = svc.execute("pay_supplier", args, p.code)  # B landed in between
    assert out.outcome == "blocked" and other.id in out.message
    assert list(erp.payments) == [other.id]


def test_connector_without_payments_is_refused(policy):
    from erp_guard.connectors.sap import SapS4
    svc = Service(SapS4.__new__(SapS4), policy, today=lambda: TODAY)
    svc._actor = TRS
    summary, res = svc.evaluate("pay_supplier", svc.actions["pay_supplier"].canonicalise(
        _args()))
    assert [r.check for r in res] == ["connector"]


# --- approval through the workflow -----------------------------------------------------

def test_manager_cannot_approve_80k(erp, policy):
    _inv(erp, "PI-80", 80000)
    pay = _submitted(erp, policy, _args(("PI-80", 80000)))
    res = _wf(erp, policy, MGR, pay.id, APPROVE)[3]
    auth = _checks(res, "payment authority")
    assert [r.level for r in auth] == [BLOCK] and "50,000" in auth[0].message
    _step(erp, policy, DIR, pay.id, APPROVE)
    assert erp.get_payment(pay.id).approver == DIR


def test_new_supplier_needs_director(erp, policy):
    erp.add_supplier(Supplier("SUP-NEW", "NEW CO LLC", onboarded_by=REQ,
                              onboarded_on=TODAY - timedelta(days=14)))
    erp.add_order(_po("PO-NEW", "SUP-NEW", 5000))
    _inv(erp, "PI-N1", 1000, supplier="SUP-NEW", po="PO-NEW")
    _inv(erp, "PI-N2", 900, supplier="SUP-NEW", po="PO-NEW")
    pay = _submitted(erp, policy, _args(("PI-N1", 1000), supplier="SUP-NEW"))
    res = _wf(erp, policy, MGR, pay.id, APPROVE)[3]
    assert [r.check for r in _blocks(res)] == ["new supplier"]
    _step(erp, policy, DIR, pay.id, APPROVE)
    # After the first payment, the manager's own limit applies again.
    second = _submitted(erp, policy, _args(("PI-N2", 900), supplier="SUP-NEW"))
    _step(erp, policy, MGR, second.id, APPROVE)


def test_approval_reruns_the_create_checks(erp, policy):
    pay = _submitted(erp, policy)
    _inv(erp, "PI-A", 100, on_hold=True)  # held after the payment was entered
    res = _wf(erp, policy, MGR, pay.id, APPROVE)[3]
    assert _level(res, "pay only approved invoices") == [BLOCK]


def test_payment_workflow_rules(erp, policy):
    pay = _paid(erp, policy)
    # Only the payer submits it; approval needs Pending Approval.
    assert "ownership" in [r.check for r in _blocks(_wf(erp, policy, AP, pay.id,
                                                        SUBMIT_FOR_APPROVAL)[3])]
    assert "payment state" in [r.check for r in _blocks(_wf(erp, policy, MGR, pay.id,
                                                            APPROVE)[3])]
    _step(erp, policy, TRS, pay.id, SUBMIT_FOR_APPROVAL)
    # The payer never approves; rejection needs a payment approver and a comment.
    assert _blocks(_wf(erp, policy, TRS, pay.id, APPROVE)[3])
    assert "comment" in " ".join(r.message for r in _blocks(_wf(erp, policy, MGR, pay.id,
                                                                  REJECT)[3]))
    assert _blocks(_wf(erp, policy, CTL, pay.id, REJECT, "wrong amount")[3])
    assert _blocks(_wf(erp, policy, TRS, pay.id, REJECT, "wrong amount")[3])
    _step(erp, policy, MGR, pay.id, REJECT, "wrong amount")
    assert erp.get_payment(pay.id).state == DocState.REJECTED
    assert _blocks(_wf(erp, policy, MGR, pay.id, REVISE)[3])  # the payer revises
    _step(erp, policy, TRS, pay.id, REVISE)
    assert erp.get_payment(pay.id).state == DocState.DRAFT
    res = _wf(erp, policy, TRS, pay.id, "Withdraw")[3]
    assert "not a Payment Entry workflow action" in " ".join(r.message for r in res)
    assert _blocks(_wf(erp, policy, MGR, "PE-NOPE", APPROVE)[3])


# --- editing a draft (generic update_document goes to the pack) ------------------------

def _update(erp, policy, pay_id, fields, user=TRS):
    svc = as_user(erp, policy, user)
    pack = svc.pack_for(PE)
    w = Write(Op.UPDATE, PE, pay_id, fields)
    return svc, pack, w, pack.evaluate_write(svc, w)[1]


def test_update_draft_payment_rechecks(erp, policy):
    pay = _paid(erp, policy, _args(("PI-A", 60)))
    # Raising the allocation above what is owed is blocked; within it is written.
    res = _update(erp, policy, pay.id, {"references": [{"allocated_amount": "100.01"}]})[3]
    assert "amount owed" in [r.check for r in _blocks(res)]
    svc, pack, w, res = _update(erp, policy, pay.id, {
        "references": [{"allocated_amount": "100"}], "remarks": "full amount"})
    assert not _blocks(res), [r.message for r in _blocks(res)]
    assert "$100.00" in pack.evaluate_write(svc, w)[0]
    pack.apply_write(svc, w)
    assert erp.get_payment(pay.id).allocations == (Allocation("PI-A", D(100)),)
    # Only the payer; only fields that may change; rows as on the draft; only drafts.
    assert _blocks(_update(erp, policy, pay.id, {"remarks": "x"}, user=MGR)[3])
    for fields in ({"paid_from": "Petty Cash - LDU"}, {"payment_type": "Internal Transfer"},
                   {"paid_to": "Creditors - LDU"}, {"party_type": "Employee"},
                   {"party": "SUP-CANON"}, {"paid_amount": 1},
                   {"references": [{"reference_name": "PI-B"}]},
                   {"references": [{"allocated_amount": 1}, {"allocated_amount": 1}]},
                   {"references": [{"allocated_amount": "-5"}]},
                   {"remarks": "send it to account number 12345678"}):
        assert _blocks(_update(erp, policy, pay.id, fields)[3]), fields
    _step(erp, policy, TRS, pay.id, SUBMIT_FOR_APPROVAL)
    res = _update(erp, policy, pay.id, {"remarks": "x"})[3]
    assert "edit after entry" in [r.check for r in _blocks(res)]
    assert _blocks(_update(erp, policy, "PE-NOPE", {"remarks": "x"})[3])


# --- cancelling a payment (generic cancel_document goes to the pack) -------------------

def _cancel(erp, policy, pay_id, user=MGR, reason="paid to the wrong invoice"):
    svc = as_user(erp, policy, user)
    pack = svc.pack_for(PE)
    w = Write(Op.CANCEL, PE, pay_id, reason=reason)
    return svc, pack, w, pack.evaluate_write(svc, w)[1]


def test_cancel_by_payer_blocked(erp, policy):
    pay = _approved(erp, policy)
    res = _cancel(erp, policy, pay.id, user=TRS)[3]
    assert "cancel payment" in [r.check for r in _blocks(res)]
    assert _blocks(_cancel(erp, policy, pay.id, user=AP)[3])  # not a payment approver
    assert _blocks(_cancel(erp, policy, pay.id, reason="")[3])  # a reason is required
    svc, pack, w, res = _cancel(erp, policy, pay.id)
    assert not _blocks(res), [r.message for r in _blocks(res)]
    pack.apply_write(svc, w)
    assert erp.get_payment(pay.id).state == DocState.CANCELLED
    assert erp.outstanding("PI-A") == D(100)
    assert _blocks(_cancel(erp, policy, pay.id)[3])  # not twice


def test_payer_who_is_an_approver_cannot_cancel_own_payment(erp, policy):
    erp.payments["PE-DIR"] = _payment("PE-DIR", ("PI-B", 10), state=DocState.APPROVED,
                                      payer=DIR)
    res = _cancel(erp, policy, "PE-DIR", user=DIR)[3]
    assert any("paid" in r.message or "payer" in r.message for r in _blocks(res))


def test_void_draft_payment(erp, policy):
    pay = _paid(erp, policy)
    assert _blocks(_cancel(erp, policy, pay.id, user=REQ)[3])
    svc, pack, w, res = _cancel(erp, policy, pay.id, user=TRS, reason="keyed twice")
    assert not _blocks(res), [r.message for r in _blocks(res)]
    assert "Void draft payment" in pack.evaluate_write(svc, w)[0]
    assert "voided" in pack.apply_write(svc, w)[1]
    assert erp.get_payment(pay.id).state == DocState.CANCELLED
    assert erp.extra[(PE, pay.id)]["reason"] == "keyed twice"
    # It no longer counts: the invoice can be paid again; nothing more happens to it.
    assert "payment state" in [r.check for r in _blocks(_wf(erp, policy, TRS, pay.id,
                                                            SUBMIT_FOR_APPROVAL)[3])]
    assert _paid(erp, policy).id != pay.id


def test_void_rejected_payment_but_not_a_pending_one(erp, policy):
    pay = _submitted(erp, policy)
    assert any("Pending Approval" in r.message for r in _blocks(_cancel(erp, policy,
                                                                        pay.id)[3]))
    _step(erp, policy, MGR, pay.id, REJECT, "wrong supplier")
    svc, pack, w, res = _cancel(erp, policy, pay.id)  # a payment approver
    assert not _blocks(res), [r.message for r in _blocks(res)]
    pack.apply_write(svc, w)
    assert erp.get_payment(pay.id).state == DocState.CANCELLED


def test_other_payment_operations_are_refused_with_the_right_tool(erp, policy):
    svc = as_user(erp, policy, TRS)
    pack = svc.pack_for(PE)
    for op, hint in ((Op.CREATE, "pay_supplier"), (Op.SUBMIT, "workflow_action"),
                     (Op.AMEND, "pay_supplier")):
        _, res = pack.evaluate_write(svc, Write(op, PE, "PE-1"))
        assert any(r.level == BLOCK and hint in r.message for r in res), op
    assert "pay_supplier" in pack.task_tools[PE]


# --- through the generic tools on ERPNext (the fake) -----------------------------------

CO = {"company": "Lakeside Demo Utilities"}
SUP, FPI, FPO = "TRANE U.S. INC", "ACC-PINV-1", "PUR-ORD-1"


@pytest.fixture
def fake_svc(policy):
    f = FakeErpNext(TRS)
    f.workflows.add(PE)
    f.add({"doctype": "Supplier", "name": SUP, "supplier_name": SUP, "disabled": 0,
           "on_hold": 0, "creation": "2025-01-01 00:00:00"})
    f.add({"doctype": "Purchase Order", "name": FPO, "docstatus": 1, "owner": REQ,
           "supplier": SUP, "transaction_date": "2026-10-20", "workflow_state": "Approved",
           "erp_guard_approver": MGR, "items": [{"item_code": "HVAC-SVC", "qty": 1,
                                                  "rate": 640}]})
    f.add({**CO, "doctype": "Purchase Invoice", "name": FPI, "docstatus": 1, "owner": AP,
           "workflow_state": "Approved", "erp_guard_approver": CTL, "supplier": SUP,
           "bill_no": "T-1", "bill_date": "2026-10-26", "posting_date": "2026-10-26",
           "is_return": 0, "is_paid": 0, "on_hold": 0, "outstanding_amount": 640.0,
           "items": [{"doctype": "Purchase Invoice Item", "item_code": "HVAC-SVC", "qty": 1,
                      "rate": 640, "purchase_order": FPO}]})
    f.add({**CO, "doctype": PE, "name": "ACC-PAY-D", "docstatus": 0, "owner": TRS,
           "workflow_state": "Draft", "payment_type": "Pay", "party_type": "Supplier",
           "party": SUP, "posting_date": "2026-10-28", "paid_from": BANK,
           "paid_amount": 600.0, "received_amount": 600.0, "unallocated_amount": 0,
           "references": [{"doctype": "Payment Entry Reference",
                           "reference_doctype": "Purchase Invoice", "reference_name": FPI,
                           "allocated_amount": 600.0}]})
    # As from_env: the client lets the policy's bank account through, nothing else.
    return Service(connector(f, allowed_bank_values={BANK}), policy, today=lambda: TODAY), f


def test_generic_update_routes_to_pack_and_rechecks(fake_svc):
    svc, fake = fake_svc
    upd = lambda fields: {"doctype": PE, "name": "ACC-PAY-D", "fields": fields}  # noqa: E731
    p = svc.preview("update_document", upd({"references": [{"allocated_amount": 641}]}))
    assert "amount owed" in [r.check for r in p.checks if r.level == BLOCK]
    assert p.code is None
    p = svc.preview("update_document", upd({"paid_to": "Cash - LDU"}))
    assert "edit after entry" in [r.check for r in p.checks if r.level == BLOCK]
    args = upd({"references": [{"allocated_amount": 640}], "remarks": "in full"})
    p = svc.preview("update_document", args)
    assert p.code, [r.message for r in p.checks if r.level == BLOCK]
    assert svc.execute("update_document", args, p.code).outcome == "written"
    doc = fake.docs[(PE, "ACC-PAY-D")]
    assert (doc["references"][0]["allocated_amount"], doc["paid_amount"], doc["remarks"],
            doc["custom_remarks"]) == (640.0, 640.0, "in full", 1)


def test_pay_supplier_on_erpnext_keeps_the_notes(fake_svc):
    svc, fake = fake_svc
    fake.docs.pop((PE, "ACC-PAY-D"))
    fake.mapped["get_payment_entry"] = {
        "doctype": PE, "payment_type": "Pay", "party_type": "Supplier", "party": SUP,
        "posting_date": "2026-10-29", "paid_from": "Cash - LDU", "paid_to": "Creditors - LDU",
        "references": [{"doctype": "Payment Entry Reference",
                        "reference_doctype": "Purchase Invoice", "reference_name": FPI,
                        "allocated_amount": 640.0}]}
    args = {"supplier": SUP, "invoices": [{"invoice": FPI, "amount": 640}],
            "notes": "October run"}
    p = svc.preview("pay_supplier", args)
    assert p.code, [r.message for r in p.checks if r.level == BLOCK]
    out = svc.execute("pay_supplier", args, p.code)
    assert out.outcome == "written", out.message
    sent = fake.docs[(PE, out.target)]
    # ERPNext would write its own remarks over ours unless they are marked custom.
    assert (sent["remarks"], sent["custom_remarks"], sent["paid_from"]) == (
        "October run", 1, BANK)


def test_generic_workflow_and_void_on_erpnext(fake_svc):
    svc, fake = fake_svc
    wf = {"doctype": PE, "name": "ACC-PAY-D", "action": SUBMIT_FOR_APPROVAL}
    p = svc.preview("workflow_action", wf)
    assert p.code, [r.message for r in p.checks if r.level == BLOCK]
    assert svc.execute("workflow_action", wf, p.code).outcome == "written"
    assert fake.docs[(PE, "ACC-PAY-D")]["workflow_state"] == "Pending Approval"
    # The payer can't approve it, through the generic tool either.
    p = svc.preview("workflow_action", {**wf, "action": APPROVE})
    assert "payment authority" in [r.check for r in p.checks if r.level == BLOCK]
    # Pending: reject first, then void.
    void = {"doctype": PE, "name": "ACC-PAY-D", "reason": "keyed twice"}
    assert svc.preview("cancel_document", void).code is None
    fake.docs[(PE, "ACC-PAY-D")]["workflow_state"] = "Rejected"
    p = svc.preview("cancel_document", void)
    assert p.code, [r.message for r in p.checks if r.level == BLOCK]
    assert svc.execute("cancel_document", void, p.code).outcome == "written"
    doc = fake.docs[(PE, "ACC-PAY-D")]
    assert (doc["docstatus"], doc["erp_guard_cancelled"], doc["erp_guard_reason"]) == (
        0, 1, "keyed twice")
    assert not fake.method_calls("frappe.client.cancel")
    assert svc.erp.get_payment("ACC-PAY-D").state == DocState.CANCELLED
    # A pay_supplier preview no longer sees it as paying the invoice.
    p = svc.preview("pay_supplier", {"supplier": SUP, "invoices": [
        {"invoice": FPI, "amount": 640}]})
    assert "duplicate payment" not in [r.check for r in p.checks if r.level == BLOCK]


def test_generic_create_and_submit_of_payments_refused(fake_svc):
    svc, _ = fake_svc
    p = svc.preview("create_document", {"doctype": PE, "fields": {
        "payment_type": "Pay", "party_type": "Supplier", "party": SUP, "paid_amount": 1}})
    assert p.code is None and any("pay_supplier" in r.message for r in p.checks)
    p = svc.preview("submit_document", {"doctype": PE, "name": "ACC-PAY-D"})
    assert p.code is None and any("workflow_action" in r.message for r in p.checks)
