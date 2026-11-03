"""Supplier invoices and debit notes: enter_supplier_invoice, enter_debit_note,
hold_invoice and release_invoice, plus the invoice's workflow, draft edits and cancellation
as the generic tools reach them (workflow_action, update_document, cancel_document).

An invoice is entered as a draft against approved orders, matched three ways (order,
receipt, invoice) before anything is written; a mismatch writes nothing. Stock lines are
matched to the order's posted receipts, oldest first. The draft is submitted for approval
by AP and approved by an invoice approver through the Purchase Invoice workflow; every
step re-runs the checks the invoice was entered with.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from erp_guard import checks as C
from erp_guard.models import PurchaseOrder
from erp_guard.ops import Write
from erp_guard.packs.p2p import APPROVE, REJECT, REVISE, SUBMIT_FOR_APPROVAL
from erp_guard.packs.p2p import checks as P
from erp_guard.packs.p2p.models import (
    POSTED,
    DocState,
    Invoice,
    InvoiceLine,
    ItemInfo,
    Receipt,
    not_editable,
)
from erp_guard.service import Action, InvalidArgs, _decimal, _text, canonicaliser

if TYPE_CHECKING:
    from erp_guard.service import Service

Result = tuple[str, list[C.CheckResult]]
INVOICE = "Purchase Invoice"
WORKFLOW_ACTIONS = (SUBMIT_FOR_APPROVAL, APPROVE, REJECT, REVISE)


def _block(check: str, message: str, **evidence) -> C.CheckResult:
    return C.CheckResult(check, C.BLOCK, message, evidence)


def _unsupported(svc: Service) -> list[C.CheckResult]:
    """Invoices need a connector that models them (ERPNext, the in-memory one)."""
    if hasattr(svc.erp, "list_invoices"):
        return []
    return [_block("connector", f"{svc.erp.name} doesn't support supplier invoices yet.")]


def _describe(lines) -> str:
    return ", ".join(f"{ln.qty.normalize():f} x {ln.item_code} @ {P.usd(ln.rate)}"
                     + (f" ({ln.po})" if getattr(ln, "po", "") else "") for ln in lines)


# --- canonical arguments ---------------------------------------------------------------

def _positive(ln: dict, key: str, i: int) -> str:
    v = _decimal(ln.get(key), key)
    if v <= 0:
        raise InvalidArgs(f"line {i + 1}: '{key}' must be more than zero")
    return format(v, "f")


def _rows(args: dict, allowed: set[str]) -> list[tuple[int, dict]]:
    raw = args.get("lines")
    if not isinstance(raw, list) or not raw:
        raise InvalidArgs("'lines' must be a non-empty list")
    out = []
    for i, ln in enumerate(raw):
        if not isinstance(ln, dict):
            raise InvalidArgs(f"line {i + 1} must be an object")
        unknown = set(ln) - allowed
        if unknown:
            raise InvalidArgs(f"line {i + 1} has unknown field(s): {', '.join(sorted(unknown))}")
        out.append((i, ln))
    return out


def _invoice_lines(args: dict) -> list[dict]:
    """[{"po", "item_code", "qty", "rate"}], each (order, item) once."""
    out, seen = [], set()
    for i, ln in _rows(args, {"po", "item_code", "qty", "rate"}):
        po = _text(ln, "po", required=True, max_len=140)
        item = _text(ln, "item_code", required=True, max_len=140)
        if (po, item) in seen:
            raise InvalidArgs(f"{item} of {po} is on more than one line; put it on one line")
        seen.add((po, item))
        out.append({"po": po, "item_code": item, "qty": _positive(ln, "qty", i),
                    "rate": _positive(ln, "rate", i)})
    return out


def _note_lines(args: dict) -> list[dict]:
    """[{"item_code", "qty", "rate"?, "po"?}]: rate defaults to the invoice's, po is only
    needed when the invoice has the item from two orders."""
    out, seen = [], set()
    for i, ln in _rows(args, {"po", "item_code", "qty", "rate"}):
        po = _text(ln, "po", max_len=140)
        item = _text(ln, "item_code", required=True, max_len=140)
        if (po, item) in seen:
            raise InvalidArgs(f"{item} is on more than one line; put it on one line")
        seen.add((po, item))
        rate = _positive(ln, "rate", i) if ln.get("rate") not in (None, "") else ""
        out.append({"po": po, "item_code": item, "qty": _positive(ln, "qty", i),
                    "rate": rate})
    return out


def _iso_date(a: dict, key: str) -> str:
    v = _text(a, key, required=True, max_len=10)
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError as exc:
        raise InvalidArgs(f"'{key}' must be an ISO date, e.g. 2026-10-29") from exc


def _ref(key: str):
    return lambda a: _text(a, key, required=True, max_len=140)


# --- what the checks read --------------------------------------------------------------

@dataclass
class _Context:
    po_by_id: dict[str, PurchaseOrder | None]
    receipts: list[Receipt]
    invoices: list[Invoice]  # on the same orders, the invoice being checked left out
    items: dict[str, ItemInfo]


def _context(svc: Service, pos, codes, exclude: str | None = None) -> _Context:
    erp = svc.erp
    po_by_id = {p: (erp.get_order(p) if p else None) for p in sorted(set(pos))}
    receipts: dict[str, Receipt] = {}
    invoices: dict[str, Invoice] = {}
    for p in po_by_id:
        if not p:
            continue
        receipts.update((r.id, r) for r in erp.list_receipts(po=p))
        invoices.update((i.id, i) for i in erp.list_invoices(po=p) if i.id != exclude)
    return _Context(po_by_id, list(receipts.values()), list(invoices.values()),
                    erp.item_info(sorted(set(codes))))


def _receipt_left(r: Receipt, po: str, item: str, ctx: _Context) -> Decimal:
    """What a receipt brought in of (po, item), less posted returns against it and less
    what live invoices already match to it. Approved debit notes on it give room back only
    up to the goods returned against it on or after the first invoice of it (as in
    checks.invoiced)."""
    def on(lines) -> Decimal:
        return sum((ln.qty for ln in lines if (ln.po, ln.item_code) == (po, item)),
                   Decimal(0))
    live = [i for i in ctx.invoices if i.state != DocState.CANCELLED]
    bills = [i for i in live if not i.is_return
             and any(ln.receipt == r.id and (ln.po, ln.item_code) == (po, item)
                     for ln in i.lines)]
    since = min((i.posting_date for i in bills), default=None)
    returns = [x for x in ctx.receipts if x.is_return and x.return_against == r.id
               and x.state in POSTED]
    back = sum((on(x.lines) for x in returns), Decimal(0))
    back_after = sum((on(x.lines) for x in returns
                      if since is not None and x.posted >= since), Decimal(0))
    billed = sum((on(ln for ln in i.lines if ln.receipt == r.id) for i in bills), Decimal(0))
    credited = sum((on(ln for ln in i.lines if ln.receipt == r.id) for i in live
                    if i.is_return and i.state in POSTED), Decimal(0))
    return on(r.lines) - back - (billed - min(credited, back_after))


def _match_receipts(rows: list[dict], ctx: _Context) -> list[InvoiceLine]:
    """Invoice lines from the tool's lines. A stock line is split over the order's posted
    receipts of that item, oldest first, by what each has left to invoice; anything beyond
    stays on the last one (the quantity check then blocks it). Services, and stock lines
    with no posted receipt at all, are against the order alone."""
    out = []
    for row in rows:
        po, item = row["po"], row["item_code"]
        need, rate = Decimal(row["qty"]), Decimal(row["rate"])
        if not P.is_stock(ctx.items, item):
            out.append(InvoiceLine(po, item, need, rate))
            continue
        recs = sorted((r for r in ctx.receipts if r.state in POSTED and not r.is_return
                       and any((ln.po, ln.item_code) == (po, item) for ln in r.lines)),
                      key=lambda r: (r.posted, r.id))
        if not recs:
            out.append(InvoiceLine(po, item, need, rate))
            continue
        split: list[list] = []
        for r in recs:
            take = min(max(_receipt_left(r, po, item, ctx), Decimal(0)), need)
            if take > 0:
                split.append([r.id, take])
                need -= take
        if need > 0:
            if split:
                split[-1][1] += need
            else:
                split.append([recs[-1].id, need])
        out += [InvoiceLine(po, item, q, rate, rid) for rid, q in split]
    return out


def _free_text(texts: dict[str, str]) -> list[C.CheckResult]:
    return C.free_text({k: v for k, v in texts.items() if v})


def _invoice_checks(svc: Service, inv: Invoice, ctx: _Context) -> list[C.CheckResult]:
    """Every check an invoice is entered with; re-run on submit, approval and edits."""
    lines = list(inv.lines)
    others = [i for i in svc.erp.list_invoices(supplier_id=inv.supplier_id) if i.id != inv.id]
    return [
        C.supplier_usable(svc.erp.get_supplier(inv.supplier_id), inv.supplier_id),
        P.invoice_po_state(ctx.po_by_id),
        P.invoice_supplier(inv.supplier_id, ctx.po_by_id),
        P.three_way_qty(lines, ctx.po_by_id, ctx.receipts, ctx.invoices, ctx.items),
        P.three_way_price(lines, ctx.po_by_id, svc.policy.invoice.price_tolerance_pct),
        P.invoice_total(lines, ctx.po_by_id, ctx.invoices, ctx.receipts, ctx.items),
        P.receipt_required(lines, ctx.items, ctx.receipts),
        *P.duplicate_invoice(inv.supplier_id, inv.bill_no, inv.total, inv.bill_date, others,
                             svc.policy),
        P.paid_at_entry(inv),
        *_free_text({"remarks": inv.remarks}),
    ]


def _existing_checks(svc: Service, inv: Invoice) -> tuple[list[C.CheckResult], _Context]:
    """The entry checks of an invoice or debit note already in the ERP."""
    ctx = _context(svc, [ln.po for ln in inv.lines], [ln.item_code for ln in inv.lines],
                   exclude=inv.id)
    if inv.is_return:
        return _note_checks(svc, inv.return_against or "", list(inv.lines), inv.id) \
            + [P.paid_at_entry(inv), *_free_text({"remarks": inv.remarks})], ctx
    return _invoice_checks(svc, inv, ctx), ctx


# --- enter_supplier_invoice ------------------------------------------------------------

def _proposed(svc: Service, canon: dict) -> tuple[Invoice, _Context]:
    rows = canon["lines"]
    ctx = _context(svc, [r["po"] for r in rows], [r["item_code"] for r in rows])
    inv = Invoice("(new)", canon["supplier"], canon["bill_no"],
                  date.fromisoformat(canon["bill_date"]), svc.actor, DocState.DRAFT,
                  tuple(_match_receipts(rows, ctx)), remarks=canon["remarks"])
    return inv, ctx


def _eval_enter(svc: Service, canon: dict) -> Result:
    actor = svc.actor
    summary = f"Enter invoice {canon['bill_no']} from {canon['supplier']}."
    if (bad := _unsupported(svc)):
        return summary, bad
    inv, ctx = _proposed(svc, canon)
    res = [P.role_allowed(actor, svc.policy, "ap", "enter supplier invoices")]
    res += _invoice_checks(svc, inv, ctx)
    return (f"Enter invoice {inv.bill_no} dated {inv.bill_date.isoformat()} from "
            f"{inv.supplier_id}: {_describe(inv.lines)}; total {P.usd(inv.total)}. A draft, "
            f"entered by {actor}; it needs submitting for approval and an invoice "
            "approver's approval before it can be paid."), res


def _write_enter(svc: Service, canon: dict) -> tuple[str, str]:
    inv, _ = _proposed(svc, canon)
    made = svc.erp.create_invoice(inv.supplier_id, inv.bill_no, inv.bill_date,
                                  list(inv.lines), inv.remarks)
    return made.id, (f"Invoice {inv.bill_no} entered as draft {made.id} "
                     f"({P.usd(made.total)}). Next: Submit for Approval.")


# --- enter_debit_note ------------------------------------------------------------------

def _note_lines_on(orig: Invoice, rows: list[dict]) -> tuple[list[InvoiceLine], list[str]]:
    """The note's lines, each on the original invoice's order line and receipt."""
    out, problems = [], []
    for row in rows:
        cands = [ln for ln in orig.lines if ln.item_code == row["item_code"]
                 and (not row["po"] or ln.po == row["po"])]
        if not cands:
            problems.append(f"{row['item_code']} is not on {orig.id}")
            continue
        if len({ln.po for ln in cands}) > 1:
            problems.append(f"{row['item_code']} is on {orig.id} from more than one order; "
                            "say which with 'po'")
            continue
        first = cands[0]
        rate = Decimal(row["rate"]) if row["rate"] else first.rate
        out.append(InvoiceLine(first.po, first.item_code, Decimal(row["qty"]), rate,
                               first.receipt))
    return out, problems


def _note_checks(svc: Service, orig_ref: str, lines: list[InvoiceLine],
                 exclude: str | None = None, problems: list[str] | None = None
                 ) -> list[C.CheckResult]:
    """A debit note: against a posted (approved) invoice, its lines on that invoice within
    the quantity not yet credited, its total within the amount not yet credited."""
    orig = svc.erp.get_invoice(orig_ref) if orig_ref else None
    if orig is None:
        return [_block("debit note", f"No purchase invoice '{orig_ref}'.")]
    if orig.is_return or orig.state not in POSTED:
        what = "a debit note itself" if orig.is_return else orig.state.value
        return [_block("debit note", f"{orig.id} is {what}; a debit note is entered against "
                       "an approved invoice.")]
    res = [C.CheckResult("debit note", C.PASS, f"{orig.id} is an approved invoice.")]
    notes = [n for n in svc.erp.list_invoices(supplier_id=orig.supplier_id)
             if n.is_return and n.return_against == orig.id and n.id != exclude
             and n.state != DocState.CANCELLED]
    on_orig = defaultdict(Decimal)
    for ln in orig.lines:
        on_orig[(ln.po, ln.item_code)] += ln.qty
    for n in notes:
        for ln in n.lines:
            on_orig[(ln.po, ln.item_code)] -= ln.qty
    problems = list(problems or [])
    for ln in lines:
        key = (ln.po, ln.item_code)
        if key not in on_orig:
            problems.append(f"{ln.item_code} on {ln.po} is not on {orig.id}")
        elif ln.qty > on_orig[key]:
            problems.append(f"{ln.item_code}: crediting {ln.qty.normalize():f} but only "
                            f"{max(on_orig[key], Decimal(0)).normalize():f} invoiced on "
                            f"{orig.id} is not yet credited")
    res.append(_block("debit note lines", "; ".join(problems) + ".") if problems else
               C.CheckResult("debit note lines", C.PASS, f"Every line is on {orig.id}."))
    total = sum((ln.amount for ln in lines), Decimal(0))
    res.append(P.debit_note_limit(orig, notes, total))
    return res


def _eval_note(svc: Service, canon: dict) -> Result:
    ref = canon["invoice"]
    summary = f"Enter a debit note against {ref}."
    if (bad := _unsupported(svc)):
        return summary, bad
    res = [P.role_allowed(svc.actor, svc.policy, "ap", "enter debit notes")]
    orig = svc.erp.get_invoice(ref)
    lines, problems = _note_lines_on(orig, canon["lines"]) if orig else ([], [])
    res += _note_checks(svc, ref, lines, problems=problems)
    res += _free_text({"reason": canon["reason"]})
    if lines:
        total = sum((ln.amount for ln in lines), Decimal(0))
        summary = (f"Enter a debit note against {ref}: {_describe(lines)}; total "
                   f"{P.usd(total)}. A draft; it is approved like an invoice and reduces "
                   f"what is owed on {ref}.")
    return summary, res


def _write_note(svc: Service, canon: dict) -> tuple[str, str]:
    orig = svc.erp.get_invoice(canon["invoice"])
    lines, _ = _note_lines_on(orig, canon["lines"])
    note = svc.erp.create_debit_note(orig.id, lines, canon["reason"])
    return note.id, (f"Debit note {note.id} entered against {orig.id} "
                     f"({P.usd(note.total)}). Next: Submit for Approval.")


# --- hold_invoice / release_invoice ----------------------------------------------------

def _eval_hold(svc: Service, canon: dict, hold: bool) -> Result:
    ref, text = canon["invoice"], canon["reason" if hold else "comment"]
    verb = "Hold" if hold else "Release"
    summary = (f"{verb} invoice {ref}" + (" so it isn't paid until released." if hold
                                          else " for payment."))
    if (bad := _unsupported(svc)):
        return summary, bad
    res = [P.hold_rights(svc.actor, svc.policy)]
    inv = svc.erp.get_invoice(ref)
    if inv is None:
        res.append(_block("hold", f"No purchase invoice '{ref}'."))
    elif hold:
        if inv.is_return or inv.state not in POSTED:
            what = "a debit note" if inv.is_return else inv.state.value
            res.append(_block("hold", f"{ref} is {what}; only approved invoices are held."))
        elif inv.on_hold:
            res.append(_block("hold", f"{ref} is already on hold."))
        elif svc.erp.outstanding(ref) <= 0:
            res.append(_block("hold", f"Nothing is owed on {ref}, so there's nothing to "
                              "hold."))
    elif not inv.on_hold:
        res.append(_block("hold", f"{ref} isn't on hold."))
    res += _free_text({"reason" if hold else "comment": text})
    return summary, res


def _write_hold(svc: Service, canon: dict, hold: bool) -> tuple[str, str]:
    ref = canon["invoice"]
    text = canon["reason" if hold else "comment"]
    svc.erp.set_invoice_hold(ref, hold, text)
    return ref, (f"Invoice {ref} on hold: {text}" if hold
                 else f"Invoice {ref} released for payment: {text}")


# --- the workflow (reached through workflow_action) ------------------------------------

def _state(inv: Invoice, want: set[DocState], doing: str) -> C.CheckResult:
    if inv.state not in want:
        return _block("invoice state", f"{inv.id} is {inv.state.value}; {doing} needs "
                      f"{' or '.join(sorted(s.value for s in want))}.")
    return C.CheckResult("invoice state", C.PASS, f"{inv.id} is {inv.state.value}.")


def evaluate_workflow(svc: Service, w: Write) -> Result:
    actor, ref, action = svc.actor, w.name, w.action or ""
    summary = f"Apply '{action}' to purchase invoice {ref}."
    if (bad := _unsupported(svc)):
        return summary, bad
    if action not in WORKFLOW_ACTIONS:
        return summary, [_block("pack", f"'{action}' is not a Purchase Invoice workflow "
                                f"action. Use one of: {', '.join(WORKFLOW_ACTIONS)}.")]
    inv = svc.erp.get_invoice(ref)
    if inv is None:
        return summary, [_block("invoice state", f"No purchase invoice '{ref}'.")]
    kind = "debit note" if inv.is_return else "invoice"
    summary = f"Apply '{action}' to {kind} {ref} ({P.usd(inv.total)}) as {actor}."
    comment = _free_text({"comment": w.reason})
    if action == SUBMIT_FOR_APPROVAL:
        res = [P.role_allowed(actor, svc.policy, "ap", f"submit a {kind} for approval"),
               _state(inv, {DocState.DRAFT}, "submitting it")]
        return summary, res + _existing_checks(svc, inv)[0] + comment
    if action == APPROVE:
        checks, ctx = _existing_checks(svc, inv)
        res = [_state(inv, {DocState.PENDING}, "approval"),
               P.invoice_separation(actor, inv, ctx.po_by_id),
               P.invoice_authority(actor, inv, svc.policy)]
        return summary, res + checks + comment
    if action == REJECT:
        res = [_state(inv, {DocState.PENDING}, "rejection"),
               P.hold_rights(actor, svc.policy)]
        if actor == inv.entered_by:
            res.append(_block("invoice separation", f"{actor} entered {ref} and can't decide "
                              "on it."))
        if not w.reason:
            res.append(_block("comment", "Say why in the comment; it goes back to "
                              f"{inv.entered_by}."))
        return summary, res + comment
    res = [_state(inv, {DocState.REJECTED}, "revising it")]  # REVISE
    if actor != inv.entered_by:
        res.append(_block("ownership", f"Only {inv.entered_by}, who entered {ref}, revises "
                          "it."))
    return summary, res + comment


def apply_workflow(svc: Service, w: Write) -> tuple[str, str]:
    svc.erp.workflow(INVOICE, w.name, w.action, w.reason)
    inv = svc.erp.get_invoice(w.name)
    note = f" Comment: {w.reason}" if w.reason else ""
    return w.name, f"'{w.action}' applied to {INVOICE} {w.name}; now {inv.state.value}.{note}"


# --- edits to a draft (reached through update_document) --------------------------------

def _edited(inv: Invoice, fields: dict) -> tuple[Invoice | None, list[str]]:
    """The invoice as it would be after the edit, or what's wrong with the edit."""
    problems, changes = [], {}
    if "bill_no" in fields:
        v = fields["bill_no"]
        if not isinstance(v, str) or not v.strip():
            problems.append("bill_no must be text")
        else:
            changes["bill_no"] = v.strip()
    if "bill_date" in fields:
        try:
            changes["bill_date"] = date.fromisoformat(str(fields["bill_date"]))
        except ValueError:
            problems.append("bill_date must be an ISO date")
    if "remarks" in fields:
        changes["remarks"] = str(fields["remarks"] or "")
    rows = fields.get("items")
    if rows is not None:
        if inv.is_return:
            problems.append(f"{inv.id} is a debit note; its lines don't change (enter "
                            "another debit note instead)")
        elif not isinstance(rows, list) or len(rows) != len(inv.lines):
            have = len(rows) if isinstance(rows, list) else 0
            problems.append(f"items: {have} rows given, {inv.id} has {len(inv.lines)}")
        else:
            lines = []
            for i, (ln, row) in enumerate(zip(inv.lines, rows, strict=True)):
                upd = {}
                for k in ("qty", "rate"):
                    if k in row:
                        try:
                            v = Decimal(str(row[k]))
                        except InvalidOperation:
                            v = Decimal(0)
                        if not v.is_finite() or v <= 0:
                            problems.append(f"items row {i + 1}: {k} must be more than zero")
                        upd[k] = v
                lines.append(replace(ln, **upd))
            changes["lines"] = tuple(lines)
    if problems:
        return None, problems
    return replace(inv, **changes), []


def evaluate_update(svc: Service, w: Write) -> Result:
    ref = w.name
    summary = f"Update purchase invoice {ref}."
    if (bad := _unsupported(svc)):
        return summary, bad
    name = "edit after entry"
    inv = svc.erp.get_invoice(ref)
    if inv is None:
        return summary, [_block(name, f"No purchase invoice '{ref}'.")]
    res = [P.paid_at_entry(w.fields)]
    if inv.state != DocState.DRAFT:
        res.append(_block(name, f"{ref} is {inv.state.value}; only drafts are edited."))
    if svc.actor != inv.entered_by:
        res.append(_block(name, f"Only {inv.entered_by}, who entered {ref}, edits it."))
    bad_fields = not_editable(INVOICE, w.fields)
    if bad_fields:
        res.append(_block(name, f"These can't be changed on an invoice draft: "
                          f"{', '.join(bad_fields)}. Editable: bill_no, bill_date, remarks, "
                          "items[].qty, items[].rate.", fields=bad_fields))
        return summary, res
    edited, problems = _edited(inv, w.fields)
    if problems:
        return summary, res + [_block(name, "; ".join(problems) + ".")]
    if not any(r.level == C.BLOCK for r in res):
        res.append(C.CheckResult(name, C.PASS, f"{ref} is a draft entered by {svc.actor}."))
    res += _existing_checks(svc, edited)[0]
    return (f"Update draft invoice {ref}: {_describe(edited.lines)}; total "
            f"{P.usd(edited.total)} (was {P.usd(inv.total)})."), res


def apply_update(svc: Service, w: Write) -> tuple[str, str]:
    fields = dict(w.fields)
    if isinstance(fields.get("bill_no"), str):  # as checked: without surrounding spaces
        fields["bill_no"] = fields["bill_no"].strip()
    if "items" in fields:  # numbers as numbers for the ERP
        fields["items"] = [{k: Decimal(str(v)) for k, v in row.items()}
                           for row in fields["items"]]
    svc.erp.update_draft(INVOICE, w.name, fields)
    return w.name, f"Draft invoice {w.name} updated: {', '.join(sorted(w.fields))}."


# --- cancel an invoice (reached through cancel_document) -------------------------------

def _evaluate_void(svc: Service, inv: Invoice, w: Write) -> Result:
    """A draft or rejected invoice can't be cancelled in ERPNext and erp-guard never deletes:
    it is voided instead (flagged cancelled, kept on record, no further workflow action),
    by the person who entered it or an invoice approver."""
    actor, name = svc.actor, "cancel invoice"
    kind = "debit note" if inv.is_return else "invoice"
    summary = (f"Void draft {kind} {inv.id} ({P.usd(inv.total)}). Nothing is deleted; it stays "
               "on record as a cancelled draft and can't be submitted any more.")
    if actor != inv.entered_by and \
            svc.policy.approver_limit(actor, "purchase_invoice") is None:
        res = [_block(name, f"Only {inv.entered_by}, who entered {inv.id}, or an invoice "
                      "approver can void it.")]
    else:
        res = [C.CheckResult(name, C.PASS, f"{inv.id} is {inv.state.value}; {actor} can "
                             "void it.")]
    return summary, res + _free_text({"reason": w.reason})


def evaluate_cancel(svc: Service, w: Write) -> Result:
    actor, ref = svc.actor, w.name
    summary = (f"Cancel purchase invoice {ref}. Nothing is deleted; it stays on record as "
               "cancelled.")
    if (bad := _unsupported(svc)):
        return summary, bad
    name = "cancel invoice"
    inv = svc.erp.get_invoice(ref)
    if inv is None:
        return summary, [_block(name, f"No purchase invoice '{ref}'.")]
    if inv.state in (DocState.DRAFT, DocState.REJECTED):
        return _evaluate_void(svc, inv, w)
    res: list[C.CheckResult] = []
    if inv.state == DocState.PENDING:
        res.append(_block(name, f"{ref} is Pending Approval; an invoice approver rejects it "
                          "first, then it can be voided."))
    elif inv.state not in POSTED:
        res.append(_block(name, f"{ref} is already {inv.state.value}."))
    if svc.policy.approver_limit(actor, "purchase_invoice") is None:
        res.append(_block(name, f"Only an invoice approver cancels an invoice; {actor} "
                          "isn't one."))
    paid = sorted(p.id for p in svc.erp.list_payments(invoice=ref)
                  if p.state != DocState.CANCELLED)
    if paid:
        res.append(_block(name, f"Payment(s) refer to {ref} ({', '.join(paid)}); an "
                          "invoice with a payment against it isn't cancelled.",
                          payments=paid))
    if not inv.is_return:
        notes = [n for n in svc.erp.list_invoices(supplier_id=inv.supplier_id)
                 if n.is_return and n.return_against == ref and n.state != DocState.CANCELLED]
        posted = sorted(n.id for n in notes if n.state in POSTED)
        open_ = sorted(n.id for n in notes if n.state not in POSTED)
        if posted:
            res.append(_block(name, f"{ref} has approved debit note(s) against it "
                              f"({', '.join(posted)}); cancel those first.", notes=posted))
        if open_:
            res.append(C.CheckResult(name, C.WARN, f"Draft debit note(s) refer to {ref} "
                                     f"({', '.join(open_)}); they can't be approved once it "
                                     "is cancelled.", {"notes": open_}))
    if not any(r.level == C.BLOCK for r in res):
        res.append(C.CheckResult(name, C.PASS, f"{ref} can be cancelled by {actor}."))
    return summary, res + _free_text({"reason": w.reason})


def apply_cancel(svc: Service, w: Write) -> tuple[str, str]:
    draft = svc.erp.get_invoice(w.name).state not in POSTED
    svc.erp.cancel_doc(INVOICE, w.name, w.reason)
    what = "voided (a cancelled draft, kept on record)" if draft else "cancelled"
    return w.name, f"{INVOICE} {w.name} {what}. Reason: {w.reason}"


# --- the actions -----------------------------------------------------------------------

ACTIONS: list[Action] = [
    Action("enter_supplier_invoice", canonicaliser("enter_supplier_invoice", {
        "supplier": _ref("supplier"),
        "bill_no": _ref("bill_no"),
        "bill_date": lambda a: _iso_date(a, "bill_date"),
        "lines": _invoice_lines,
        "remarks": lambda a: _text(a, "remarks"),
    }), _eval_enter, _write_enter),
    Action("enter_debit_note", canonicaliser("enter_debit_note", {
        "invoice": _ref("invoice"),
        "lines": _note_lines,
        "reason": lambda a: _text(a, "reason", required=True),
    }), _eval_note, _write_note, "invoice"),
    Action("hold_invoice", canonicaliser("hold_invoice", {
        "invoice": _ref("invoice"),
        "reason": lambda a: _text(a, "reason", required=True),
    }), lambda svc, c: _eval_hold(svc, c, True), lambda svc, c: _write_hold(svc, c, True),
        "invoice"),
    Action("release_invoice", canonicaliser("release_invoice", {
        "invoice": _ref("invoice"),
        "comment": lambda a: _text(a, "comment", required=True),
    }), lambda svc, c: _eval_hold(svc, c, False), lambda svc, c: _write_hold(svc, c, False),
        "invoice"),
]
