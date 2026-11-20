"""Supplier payments: pay_supplier, plus the payment's workflow, draft edits and cancellation
as the generic tools reach them (workflow_action, update_document, cancel_document).

A payment is a draft Payment Entry (Pay, to the supplier) allocated to approved invoices,
paid from the company's one bank account in policy. Treasury enters it and submits it for
approval; a payment approver who isn't the payer approves it through the Payment Entry
workflow, which posts it. Every step re-runs the checks the payment was entered with. No
real money moves: a Payment Entry is an accounting record.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from erp_guard import checks as C
from erp_guard.ops import Write
from erp_guard.packs.p2p import APPROVE, REJECT, REVISE, SUBMIT_FOR_APPROVAL
from erp_guard.packs.p2p import checks as P
from erp_guard.packs.p2p.models import Allocation, DocState, Payment, not_editable
from erp_guard.service import Action, InvalidArgs, _decimal, _text, canonicaliser

if TYPE_CHECKING:
    from erp_guard.service import Service

Result = tuple[str, list[C.CheckResult]]
PAYMENT = "Payment Entry"
INVOICE = "Purchase Invoice"
WORKFLOW_ACTIONS = (SUBMIT_FOR_APPROVAL, APPROVE, REJECT, REVISE)


def _block(check: str, message: str, **evidence) -> C.CheckResult:
    return C.CheckResult(check, C.BLOCK, message, evidence)


def _unsupported(svc: Service) -> list[C.CheckResult]:
    """Payments need a connector that models them (ERPNext, the in-memory one)."""
    if hasattr(svc.erp, "list_payments"):
        return []
    return [_block("connector", f"{svc.erp.name} doesn't support supplier payments yet.")]


def _free_text(texts: dict[str, str]) -> list[C.CheckResult]:
    return C.free_text({k: v for k, v in texts.items() if v})


def _describe(allocations) -> str:
    return ", ".join(f"{a.invoice} {P.usd(a.amount)}" for a in allocations)


def _cents(v, key: str) -> Decimal:
    d = _decimal(v, key)
    if d <= 0:
        raise InvalidArgs(f"'{key}' must be more than zero")
    if d != d.quantize(Decimal("0.01")):
        raise InvalidArgs(f"'{key}' must be in whole cents")
    return d


# --- canonical arguments ---------------------------------------------------------------

def _allocations(args: dict) -> list[dict]:
    """[{"invoice", "amount"}], each invoice once. An empty list is accepted here so the
    check can say why (no advance payments)."""
    raw = args.get("invoices")
    if not isinstance(raw, list):
        raise InvalidArgs("'invoices' must be a list of {\"invoice\", \"amount\"}")
    out, seen = [], set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise InvalidArgs(f"invoice {i + 1} must be an object")
        unknown = set(row) - {"invoice", "amount"}
        if unknown:
            raise InvalidArgs(f"invoice {i + 1} has unknown field(s): "
                              f"{', '.join(sorted(unknown))}")
        ref = _text(row, "invoice", required=True, max_len=140)
        if ref in seen:
            raise InvalidArgs(f"{ref} is listed more than once; pay it on one line")
        seen.add(ref)
        out.append({"invoice": ref, "amount": format(_cents(row.get("amount"), "amount"),
                                                      "f")})
    return out


# --- the checks a payment is entered with ----------------------------------------------

def _payment_checks(svc: Service, pay: Payment) -> list[C.CheckResult]:
    """Every check a payment is entered with, for the payment as it is (or would be); re-run
    on submit, approval and edits. The payment itself is left out of the duplicates."""
    erp = svc.erp
    invoices = {a.invoice: erp.get_invoice(a.invoice) for a in pay.allocations}
    owed = {ref: erp.outstanding(ref) for ref, inv in invoices.items() if inv is not None}
    orders = sorted({ln.po for inv in invoices.values() if inv is not None
                     for ln in inv.lines if ln.po})
    po_by_id = {po: erp.get_order(po) for po in orders}
    others: dict[str, Payment] = {p.id: p for p in erp.list_payments(supplier_id=pay.supplier_id)}
    for ref in invoices:
        others.update((p.id, p) for p in erp.list_payments(invoice=ref))
    others.pop(pay.id, None)
    return [
        P.pay_to_supplier(pay.payment_type, pay.party_type),
        C.supplier_usable(erp.get_supplier(pay.supplier_id), pay.supplier_id),
        *P.payable_invoices(pay.supplier_id, list(pay.allocations), invoices, owed,
                            pay.unallocated),
        *P.duplicate_payment(list(pay.allocations), list(others.values()), pay.supplier_id,
                             svc.today(), svc.policy.payment.dup_days),
        P.payer_separation(pay.payer, invoices, po_by_id),
        P.paying_account(pay.paid_from, svc.policy),
    ]


def _summary_tail(pay: Payment) -> str:
    return (f"{P.usd(pay.amount)} to {pay.supplier_id} from {pay.paid_from}: "
            f"{_describe(pay.allocations)}")


# --- pay_supplier ----------------------------------------------------------------------

def _proposed(svc: Service, canon: dict) -> Payment:
    return Payment("(new)", canon["supplier"], svc.actor, DocState.DRAFT, svc.today(),
                   svc.policy.payment.bank_account,
                   tuple(Allocation(a["invoice"], Decimal(a["amount"]))
                         for a in canon["invoices"]))


def _eval_pay(svc: Service, canon: dict) -> Result:
    summary = f"Pay {canon['supplier']}."
    if (bad := _unsupported(svc)):
        return summary, bad
    pay = _proposed(svc, canon)
    res = [P.role_allowed(svc.actor, svc.policy, "treasury", "pay suppliers")]
    res += _payment_checks(svc, pay)
    res += _free_text({"notes": canon["notes"]})
    return (f"Pay {_summary_tail(pay)}. A draft payment entered by {svc.actor}; it needs "
            "Submit for Approval and a payment approver's approval (not the payer) before it "
            "posts."), res


def _write_pay(svc: Service, canon: dict) -> tuple[str, str]:
    pay = _proposed(svc, canon)
    made = svc.erp.create_payment(pay.supplier_id, list(pay.allocations), pay.paid_from,
                                  remarks=canon["notes"])
    return made.id, (f"Payment {made.id} entered as a draft ({P.usd(made.amount)} to "
                     f"{made.supplier_id}). Next: Submit for Approval.")


# --- the workflow (reached through workflow_action) ------------------------------------

def _state(pay: Payment, want: DocState, doing: str) -> C.CheckResult:
    if pay.state != want:
        return _block("payment state", f"{pay.id} is {pay.state.value}; {doing} needs "
                      f"{want.value}.")
    return C.CheckResult("payment state", C.PASS, f"{pay.id} is {pay.state.value}.")


def _payer_only(actor: str, pay: Payment, doing: str) -> list[C.CheckResult]:
    if actor != pay.payer:
        return [_block("ownership", f"Only {pay.payer}, who entered {pay.id}, {doing} it.")]
    return []


def evaluate_workflow(svc: Service, w: Write) -> Result:
    actor, ref, action = svc.actor, w.name, w.action or ""
    summary = f"Apply '{action}' to payment {ref}."
    if (bad := _unsupported(svc)):
        return summary, bad
    if action not in WORKFLOW_ACTIONS:
        return summary, [_block("pack", f"'{action}' is not a Payment Entry workflow "
                                f"action. Use one of: {', '.join(WORKFLOW_ACTIONS)}.")]
    pay = svc.erp.get_payment(ref)
    if pay is None:
        return summary, [_block("payment state", f"No payment '{ref}'.")]
    summary = f"Apply '{action}' to payment {ref} ({_summary_tail(pay)}) as {actor}."
    comment = _free_text({"comment": w.reason})
    if action == SUBMIT_FOR_APPROVAL:
        res = [P.role_allowed(actor, svc.policy, "treasury", "submit payments for approval"),
               _state(pay, DocState.DRAFT, "submitting it"),
               *_payer_only(actor, pay, "submits")]
        return summary, res + _payment_checks(svc, pay) + comment
    if action == APPROVE:
        res = [_state(pay, DocState.PENDING, "approval"),
               P.payment_authority(actor, pay, svc.policy)]
        supplier = svc.erp.get_supplier(pay.supplier_id)
        if supplier is not None:  # a missing one is blocked by supplier_usable
            prior = [p for p in svc.erp.list_payments(supplier_id=pay.supplier_id)
                     if p.id != pay.id]
            res.append(P.new_supplier(actor, supplier, prior, svc.today(), svc.policy))
        return summary, res + _payment_checks(svc, pay) + comment
    if action == REJECT:
        res = [_state(pay, DocState.PENDING, "rejection")]
        if svc.policy.approver_limit(actor, "payment") is None:
            res.append(_block("payment authority", f"Only payment approvers reject payments; "
                              f"{actor} isn't one."))
        if actor == pay.payer:
            res.append(_block("payment authority", f"{actor} entered {ref} and can't decide "
                              "on it."))
        if not w.reason:
            res.append(_block("comment", f"Say why in the comment; it goes back to "
                              f"{pay.payer}."))
        return summary, res + comment
    res = [_state(pay, DocState.REJECTED, "revising it"),  # REVISE
           *_payer_only(actor, pay, "revises")]
    return summary, res + comment


def apply_workflow(svc: Service, w: Write) -> tuple[str, str]:
    svc.erp.workflow(PAYMENT, w.name, w.action, w.reason)
    pay = svc.erp.get_payment(w.name)
    note = f" Comment: {w.reason}" if w.reason else ""
    return w.name, f"'{w.action}' applied to {PAYMENT} {w.name}; now {pay.state.value}.{note}"


# --- edits to a draft (reached through update_document) --------------------------------

def _edited(pay: Payment, fields: dict) -> tuple[Payment | None, list[str]]:
    """The payment as it would be after the edit (allocations in the draft's row order),
    or what's wrong with the edit."""
    rows = fields.get("references")
    if rows is None:
        return pay, []
    if not isinstance(rows, list) or len(rows) != len(pay.allocations):
        have = len(rows) if isinstance(rows, list) else 0
        return None, [f"references: {have} rows given, {pay.id} has "
                      f"{len(pay.allocations)}"]
    problems, allocs = [], []
    for i, (a, row) in enumerate(zip(pay.allocations, rows, strict=True)):
        if "allocated_amount" not in row:
            allocs.append(a)
            continue
        try:
            v = _cents(row["allocated_amount"], "allocated_amount")
        except InvalidArgs as exc:
            problems.append(f"references row {i + 1}: {exc}")
            continue
        allocs.append(replace(a, amount=v))
    if problems:
        return None, problems
    return replace(pay, allocations=tuple(allocs)), []


def evaluate_update(svc: Service, w: Write) -> Result:
    ref, name = w.name, "edit after entry"
    summary = f"Update payment {ref}."
    if (bad := _unsupported(svc)):
        return summary, bad
    pay = svc.erp.get_payment(ref)
    if pay is None:
        return summary, [_block(name, f"No payment '{ref}'.")]
    res = []
    if pay.state != DocState.DRAFT:
        res.append(_block(name, f"{ref} is {pay.state.value}; only drafts are edited."))
    if svc.actor != pay.payer:
        res.append(_block(name, f"Only {pay.payer}, who entered {ref}, edits it."))
    bad_fields = not_editable(PAYMENT, w.fields)
    if bad_fields:
        res.append(_block(name, f"These can't be changed on a payment draft: "
                          f"{', '.join(bad_fields)}. Editable: remarks, "
                          "references[].allocated_amount.", fields=bad_fields))
        return summary, res
    edited, problems = _edited(pay, w.fields)
    if problems:
        return summary, res + [_block(name, "; ".join(problems) + ".")]
    if not any(r.level == C.BLOCK for r in res):
        res.append(C.CheckResult(name, C.PASS, f"{ref} is a draft entered by {svc.actor}."))
    res += _payment_checks(svc, edited)
    res += _free_text({"remarks": str(w.fields.get("remarks") or "")})
    return (f"Update draft payment {ref}: {_summary_tail(edited)} (was "
            f"{P.usd(pay.amount)})."), res


def apply_update(svc: Service, w: Write) -> tuple[str, str]:
    fields = dict(w.fields)
    if "references" in fields:  # amounts as numbers for the ERP
        fields["references"] = [{k: Decimal(str(v)) for k, v in row.items()}
                                for row in fields["references"]]
    svc.erp.update_draft(PAYMENT, w.name, fields)
    return w.name, f"Draft payment {w.name} updated: {', '.join(sorted(w.fields))}."


# --- cancel a payment (reached through cancel_document) --------------------------------

def evaluate_cancel(svc: Service, w: Write) -> Result:
    """A posted payment is cancelled only by a payment approver who isn't its payer. A
    draft or rejected one can't be cancelled in ERPNext and erp-guard never deletes: it is
    voided instead (flagged cancelled, kept on record), by its payer or a payment approver,
    so it no longer stands in the way of paying its invoices. A reason is always needed."""
    actor, ref, name = svc.actor, w.name, "cancel payment"
    summary = f"Cancel payment {ref}. Nothing is deleted; it stays on record as cancelled."
    if (bad := _unsupported(svc)):
        return summary, bad
    pay = svc.erp.get_payment(ref)
    if pay is None:
        return summary, [_block(name, f"No payment '{ref}'.")]
    approver = svc.policy.approver_limit(actor, "payment") is not None
    res: list[C.CheckResult] = []
    if pay.state in (DocState.DRAFT, DocState.REJECTED):
        summary = (f"Void draft payment {ref} ({_summary_tail(pay)}). Nothing is deleted; it "
                   "stays on record as a cancelled draft and can't be submitted any more.")
        if actor != pay.payer and not approver:
            res.append(_block(name, f"Only {pay.payer}, who entered {ref}, or a payment "
                              "approver can void it."))
    elif pay.state == DocState.PENDING:
        res.append(_block(name, f"{ref} is Pending Approval; a payment approver rejects it "
                          "first, then it can be voided."))
    elif pay.state == DocState.CANCELLED:
        res.append(_block(name, f"{ref} is already cancelled."))
    else:
        summary = (f"Cancel payment {ref} ({_summary_tail(pay)}); what it paid is owed "
                   "again. Nothing is deleted; it stays on record as cancelled.")
        if not approver:
            res.append(_block(name, f"Only a payment approver cancels a payment; {actor} "
                              "isn't one."))
        if actor == pay.payer:
            res.append(_block(name, f"{actor} paid {ref} and can't also cancel it."))
    if not w.reason:
        res.append(_block(name, "A reason is required."))
    if not any(r.level == C.BLOCK for r in res):
        res.append(C.CheckResult(name, C.PASS, f"{ref} is {pay.state.value}; {actor} can "
                                 "cancel it."))
    return summary, res + _free_text({"reason": w.reason})


def apply_cancel(svc: Service, w: Write) -> tuple[str, str]:
    draft = svc.erp.get_payment(w.name).state != DocState.APPROVED
    svc.erp.cancel_doc(PAYMENT, w.name, w.reason)
    what = "voided (a cancelled draft, kept on record)" if draft else "cancelled"
    return w.name, f"{PAYMENT} {w.name} {what}. Reason: {w.reason}"


# --- the actions -----------------------------------------------------------------------

def _invoices_paid(canon: dict) -> list[tuple[str, str]]:
    """The code is bound to the version of every invoice the payment pays."""
    return [(INVOICE, row["invoice"]) for row in canon["invoices"]]


ACTIONS: list[Action] = [
    Action("pay_supplier", canonicaliser("pay_supplier", {
        "supplier": lambda a: _text(a, "supplier", required=True, max_len=140),
        "invoices": _allocations,
        "notes": lambda a: _text(a, "notes"),
    }), _eval_pay, _write_pay, None, _invoices_paid),
]
