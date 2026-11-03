"""Purchase-to-pay checks. Pure functions: documents already read from the ERP, plus the
policy, in; results out. They share `erp_guard.checks`' result type and levels."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from erp_guard.checks import BLOCK, PASS, WARN, CheckResult
from erp_guard.models import PurchaseOrder, Status
from erp_guard.packs.p2p.models import (
    POSTED,
    DocState,
    Invoice,
    InvoiceLine,
    ItemInfo,
    Receipt,
    ReceiptLine,
)
from erp_guard.policy import Policy


def _per_item(lines) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(Decimal)
    for ln in lines:
        out[ln.item_code] += ln.qty
    return out


def _net_received(receipts: list[Receipt], po: str) -> dict[str, Decimal]:
    """Quantity per item received on the order: receipts add, returns subtract. Drafts count
    (they will be posted); cancelled documents don't."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for r in receipts:
        if r.state == DocState.CANCELLED:
            continue
        sign = -1 if r.is_return else 1
        for ln in r.lines:
            if ln.po == po:
                out[ln.item_code] += sign * ln.qty
    return out


def _qty(x: Decimal) -> str:
    return format(x.normalize(), "f")


def role_allowed(actor: str, policy: Policy, role: str, what: str) -> CheckResult:
    if role in policy.roles.get(actor, set()):
        return CheckResult("role", PASS, f"{actor} holds the {role} role.")
    return CheckResult("role", BLOCK, f"{actor} doesn't hold the {role} role, so can't {what}.",
                       {"actor": actor, "role": role})


def receipt_po_state(po: PurchaseOrder | None, ref: str) -> CheckResult:
    name = "receipt against approved PO"
    if po is None:
        return CheckResult(name, BLOCK, f"No purchase order '{ref}'.")
    if po.status != Status.APPROVED or po.closed:
        now = "closed" if po.closed and po.status == Status.APPROVED else po.status.value
        return CheckResult(name, BLOCK,
                           f"{po.id} is {now}; goods are received only against an approved, "
                           "open order.", {"status": now})
    return CheckResult(name, PASS, f"{po.id} is approved and open.")


def stock_items(codes: list[str], infos: dict[str, ItemInfo]) -> CheckResult:
    """Only stock items are received; services are invoiced against the order."""
    bad = sorted(c for c in codes if c in infos and not infos[c].is_stock)
    if bad:
        return CheckResult("stock items", BLOCK,
                           f"{', '.join(bad)} is not a stock item; services are invoiced "
                           "against the order, not received.", {"items": bad})
    return CheckResult("stock items", PASS, "All items are stock items.")


def over_receipt(po: PurchaseOrder, receipts: list[Receipt], new: list[ReceiptLine],
                 pct: Decimal) -> CheckResult:
    """Received (draft and submitted receipts, less returns) plus the new quantity must stay
    within the ordered quantity plus the tolerance."""
    ordered = _per_item(po.lines)
    received = _net_received(receipts, po.id)
    problems, items = [], []
    for item, qty in sorted(_per_item(new).items()):
        if item not in ordered:
            problems.append(f"{item} is not on {po.id}")
            items.append(item)
            continue
        limit = ordered[item] * (1 + pct / 100)
        total = received.get(item, Decimal(0)) + qty
        if total > limit:
            problems.append(
                f"{item}: receiving {_qty(qty)} would make {_qty(total)} received against "
                f"{_qty(ordered[item])} ordered (the most allowed is {_qty(limit)}, "
                f"{_qty(pct)}% over)")
            items.append(item)
    if problems:
        return CheckResult("over-receipt", BLOCK, "; ".join(problems) + ".",
                           {"items": items, "tolerance_pct": str(pct)})
    return CheckResult("over-receipt", PASS,
                       f"Within the ordered quantity plus {_qty(pct)}%.")


def receiver_independence(actor: str, po: PurchaseOrder) -> list[CheckResult]:
    name = "receiver independence"
    out = []
    if po.approver and actor == po.approver:
        out.append(CheckResult(name, BLOCK,
                               f"{actor} approved {po.id} and can't also receive its goods.",
                               {"approver": po.approver}))
    if actor == po.requester:
        out.append(CheckResult(name, WARN,
                               f"{actor} requested {po.id} and is also receiving its goods; "
                               "someone else should confirm the delivery.",
                               {"requester": po.requester}))
    return out or [CheckResult(name, PASS,
                               f"{actor} neither requested nor approved {po.id}.")]


def receipt_returnable(receipt: Receipt | None, ref: str) -> CheckResult:
    name = "receipt state"
    if receipt is None:
        return CheckResult(name, BLOCK, f"No purchase receipt '{ref}'.")
    if receipt.is_return:
        return CheckResult(name, BLOCK, f"{receipt.id} is itself a return; return goods from "
                           "the receipt that brought them in.")
    if receipt.state != DocState.SUBMITTED:
        return CheckResult(name, BLOCK, f"{receipt.id} is {receipt.state.value}; only a "
                           "submitted receipt can have goods returned.")
    return CheckResult(name, PASS, f"{receipt.id} is submitted.")


def return_limit(receipt: Receipt, returns: list[Receipt], new: list[ReceiptLine]
                 ) -> CheckResult:
    """Returned quantity per item must not exceed what the receipt brought in less what has
    already gone back (drafts included, cancelled returns not)."""
    received = _per_item(receipt.lines)
    back = _per_item(ln for r in returns
                     if r.is_return and r.return_against == receipt.id
                     and r.state != DocState.CANCELLED for ln in r.lines)
    problems, items = [], []
    for item, qty in sorted(_per_item(new).items()):
        left = received.get(item, Decimal(0)) - back.get(item, Decimal(0))
        if item not in received:
            problems.append(f"{item} is not on {receipt.id}")
        elif qty > left:
            problems.append(f"{item}: returning {_qty(qty)} but only {_qty(left)} of "
                            f"{_qty(received[item])} received is still with us")
        else:
            continue
        items.append(item)
    if problems:
        return CheckResult("return limit", BLOCK, "; ".join(problems) + ".", {"items": items})
    return CheckResult("return limit", PASS, "Within what the receipt brought in.")


# --- supplier invoices -----------------------------------------------------------------

def usd(x: Decimal) -> str:
    return f"${x:,.2f}"


_BILL_SEPARATORS = re.compile(r"[\s\-_/.#]+")
_BILL_PREFIX = re.compile(r"^(?:INVOICE|INV|NO)+(?=\d)")
_LEADING_ZEROS = re.compile(r"(?<!\d)0+(?=\d)")


def normalise_bill_no(s: str) -> str:
    """A supplier's invoice number as compared for duplicates: upper case, without spaces
    and punctuation, without a leading INV / INVOICE / NO before the digits, and without
    leading zeros on any run of digits. "INV-01001", "inv 1001" and "1001" are all "1001";
    "A-01001" and "A-1001" are both "A1001"."""
    parts = _BILL_SEPARATORS.split((s or "").upper())
    t = "".join(_LEADING_ZEROS.sub("", part) for part in parts)
    return _BILL_PREFIX.sub("", t)


def _live_invoices(invoices: list[Invoice]) -> list[Invoice]:
    return [i for i in invoices if i.state != DocState.CANCELLED]


@dataclass(frozen=True)
class Invoiced:
    """What is already invoiced of one (PO, item), net of the debit notes that give room
    back. `qty`/`amount` count every such credit; `strict_qty`/`strict_amount` leave out the
    credits on services, so a new invoice that needs those credits can be flagged."""

    qty: Decimal = Decimal(0)
    amount: Decimal = Decimal(0)
    strict_qty: Decimal = Decimal(0)
    strict_amount: Decimal = Decimal(0)


def invoiced(invoices: list[Invoice], receipts: list[Receipt],
             items: dict[str, ItemInfo]) -> dict[tuple[str, str], Invoiced]:
    """Per (PO, item): invoices of every state but cancelled add. A debit note gives room
    back only where the goods went back: for a stock item, credited quantity counts up to
    the quantity on posted returns (a credit for goods kept is a price adjustment, not room
    to bill them again), at the credit's average rate. For a service, credits count in
    full, but outside the strict figures."""
    billed_q: dict = defaultdict(Decimal)
    billed_a: dict = defaultdict(Decimal)
    credit_q: dict = defaultdict(Decimal)
    credit_a: dict = defaultdict(Decimal)
    for inv in _live_invoices(invoices):
        q, a = (credit_q, credit_a) if inv.is_return else (billed_q, billed_a)
        for ln in inv.lines:
            q[(ln.po, ln.item_code)] += ln.qty
            a[(ln.po, ln.item_code)] += ln.amount
    returned: dict = defaultdict(Decimal)
    for r in receipts:
        if r.is_return and r.state in POSTED:
            for ln in r.lines:
                returned[(ln.po, ln.item_code)] += ln.qty
    out = {}
    for key in set(billed_q) | set(credit_q):
        cq, ca = credit_q.get(key, Decimal(0)), credit_a.get(key, Decimal(0))
        bq, ba = billed_q.get(key, Decimal(0)), billed_a.get(key, Decimal(0))
        if is_stock(items, key[1]):
            back = min(cq, returned.get(key, Decimal(0)))
            back_a = ca * back / cq if cq else Decimal(0)
            out[key] = Invoiced(bq - back, ba - back_a, bq - back, ba - back_a)
        else:
            out[key] = Invoiced(bq - cq, ba - ca, bq, ba)
    return out


def received_qty(receipts: list[Receipt]) -> dict[tuple[str, str], Decimal]:
    """Quantity per (PO, item) on posted receipts, less posted returns: what can be
    invoiced against."""
    out: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for r in receipts:
        if r.state not in POSTED:
            continue
        sign = -1 if r.is_return else 1
        for ln in r.lines:
            out[(ln.po, ln.item_code)] += sign * ln.qty
    return out


def is_stock(items: dict[str, ItemInfo], code: str) -> bool:
    # An item the ERP didn't describe is treated as stock: the stricter match.
    info = items.get(code)
    return info is None or info.is_stock


def invoice_po_state(po_by_id: dict[str, PurchaseOrder | None]) -> CheckResult:
    name = "invoice against approved PO"
    problems = []
    for ref, po in sorted(po_by_id.items()):
        if not ref:
            problems.append("a line isn't against a purchase order")
        elif po is None:
            problems.append(f"no purchase order '{ref}'")
        elif po.status != Status.APPROVED or po.closed:
            now = "closed" if po.closed and po.status == Status.APPROVED else po.status.value
            problems.append(f"{po.id} is {now}")
    if problems:
        return CheckResult(name, BLOCK, "Invoices are entered only against approved, open "
                           "orders: " + "; ".join(problems) + ".")
    return CheckResult(name, PASS, "Every order on the invoice is approved and open.")


def invoice_supplier(invoice_supplier: str, po_by_id: dict[str, PurchaseOrder | None]
                     ) -> CheckResult:
    name = "invoice supplier"
    other = sorted(f"{po.id} ({po.supplier_id})" for po in po_by_id.values()
                   if po is not None and po.supplier_id != invoice_supplier)
    if other:
        return CheckResult(name, BLOCK, f"The invoice is from {invoice_supplier}, but "
                           f"{', '.join(other)} is another supplier's order.",
                           {"orders": other})
    return CheckResult(name, PASS, f"Every order on the invoice is from {invoice_supplier}.")


def three_way_qty(lines: list[InvoiceLine], po_by_id: dict[str, PurchaseOrder | None],
                  receipts: list[Receipt], invoices: list[Invoice],
                  items: dict[str, ItemInfo]) -> CheckResult:
    """Per (PO, item): stock items up to what was received less what is already invoiced;
    services (non-stock) up to what was ordered less what is already invoiced (see
    `invoiced` for how debit notes count). Billing a service again on room a debit note
    freed warns."""
    name = "three-way match: quantity"
    received, done = received_qty(receipts), invoiced(invoices, receipts, items)
    new: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for ln in lines:
        new[(ln.po, ln.item_code)] += ln.qty
    problems, bad, rebill = [], [], []
    for (ref, item), qty in sorted(new.items()):
        po = po_by_id.get(ref)
        if po is None:
            continue  # invoice_po_state says why
        ordered = _per_item(po.lines)
        if item not in ordered:
            problems.append(f"{item} is not on {ref}")
            bad.append(item)
            continue
        stock = is_stock(items, item)
        base = received.get((ref, item), Decimal(0)) if stock else ordered[item]
        so_far = done.get((ref, item), Invoiced())
        left = base - so_far.qty
        if qty > left:
            what = "received" if stock else "ordered"
            problems.append(f"{item} on {ref}: invoicing {_qty(qty)} but only "
                            f"{_qty(max(left, Decimal(0)))} of {_qty(base)} {what} is not "
                            "yet invoiced")
            bad.append(item)
        elif qty > base - so_far.strict_qty:
            rebill.append(f"{item} on {ref}")
    if problems:
        return CheckResult(name, BLOCK, "; ".join(problems) + ". A mismatched invoice goes "
                           "back to the supplier; nothing is entered.", {"items": bad})
    if rebill:
        return CheckResult(name, WARN, "Re-billing credited quantity: "
                           + ", ".join(rebill) + " was billed and credited by a debit note "
                           "before; check the service was delivered again.",
                           {"rebilled": rebill})
    return CheckResult(name, PASS, "Within what was received (stock items) or ordered "
                       "(services) and not yet invoiced.")


def three_way_price(lines: list[InvoiceLine], po_by_id: dict[str, PurchaseOrder | None],
                    pct: Decimal) -> CheckResult:
    name = "three-way match: price"
    problems, bad = [], []
    for ln in lines:
        po = po_by_id.get(ln.po)
        rate = {x.item_code: x.rate for x in po.lines}.get(ln.item_code) if po else None
        if rate is None:
            continue
        limit = rate * (1 + pct / 100)
        if ln.rate > limit:
            problems.append(f"{ln.item_code} on {ln.po} at {usd(ln.rate)} against the order's "
                            f"{usd(rate)} (at most {usd(limit)}, {_qty(pct)}% over)")
            bad.append(ln.item_code)
    if problems:
        return CheckResult(name, BLOCK, "; ".join(problems) + ".", {"items": bad})
    return CheckResult(name, PASS, f"Every rate is within {_qty(pct)}% of the order's.")


def invoice_total(lines: list[InvoiceLine], po_by_id: dict[str, PurchaseOrder | None],
                  invoices: list[Invoice], receipts: list[Receipt],
                  items: dict[str, ItemInfo]) -> CheckResult:
    """Per order: this invoice's lines plus what is already invoiced stay within the
    order's total. Debit notes count as in `invoiced`: on stock items only for goods that
    went back; re-billing a service's credited amount warns."""
    name = "invoice total"
    already: dict[str, Decimal] = defaultdict(Decimal)
    strict: dict[str, Decimal] = defaultdict(Decimal)
    for (ref, _), inv in invoiced(invoices, receipts, items).items():
        already[ref] += inv.amount
        strict[ref] += inv.strict_amount
    new: dict[str, Decimal] = defaultdict(Decimal)
    for ln in lines:
        new[ln.po] += ln.amount
    problems, rebill = [], []
    for ref, amount in sorted(new.items()):
        po = po_by_id.get(ref)
        if po is None:
            continue
        total = already[ref] + amount
        if total > po.total:
            problems.append(f"{ref}: {usd(amount)} on this invoice and "
                            f"{usd(already[ref])} already invoiced make "
                            f"{usd(total)} against an order of {usd(po.total)}")
        elif strict[ref] + amount > po.total:
            rebill.append(ref)
    if rebill and not problems:
        return CheckResult(name, WARN, "Re-billing credited quantity: within "
                           f"{', '.join(rebill)}'s total only because of a debit note on a "
                           "service.", {"rebilled": rebill})
    if problems:
        return CheckResult(name, BLOCK, "; ".join(problems) + ".")
    return CheckResult(name, PASS, "Within each order's total.")


def receipt_required(lines: list[InvoiceLine], items: dict[str, ItemInfo],
                     receipts: list[Receipt]) -> CheckResult:
    """No receipt, no invoice: every stock line is against a posted receipt of its order
    and item."""
    name = "no receipt, no invoice"
    by_id = {r.id: r for r in receipts}
    problems = []
    for ln in lines:
        if not is_stock(items, ln.item_code):
            continue
        r = by_id.get(ln.receipt or "")
        if r is None or r.state not in POSTED or r.is_return or not any(
                (x.po, x.item_code) == (ln.po, ln.item_code) for x in r.lines):
            where = f"receipt {ln.receipt} isn't a posted receipt of it" if ln.receipt \
                else "no posted receipt"
            problems.append(f"{ln.item_code} on {ln.po}: {where}")
    if problems:
        return CheckResult(name, BLOCK, "Stock items are invoiced only once received: "
                           + "; ".join(problems) + ".")
    return CheckResult(name, PASS, "Every stock line is against a posted receipt.")


def _digits_key(amount: Decimal) -> str:
    """Spend Leakage Finder's transposition key: the amount's digits in cents, sorted."""
    return "".join(sorted(str(int((abs(amount) * 100).to_integral_value()))))


def duplicate_invoice(supplier_id: str, bill_no: str, total: Decimal, bill_date: date,
                      existing: list[Invoice], policy: Policy) -> list[CheckResult]:
    """The same supplier's invoices (not cancelled, not debit notes): the same number after
    normalising blocks; the same total under another number within 30 days (T2), or a total
    within 0.5% or with the same digits transposed within 14 days (T4), warns."""
    others = [i for i in _live_invoices(existing)
              if i.supplier_id == supplier_id and not i.is_return]
    key = normalise_bill_no(bill_no)
    same = sorted(f"{i.id} ({i.bill_no})" for i in others
                  if key and normalise_bill_no(i.bill_no) == key)
    out = []
    if same:
        out.append(CheckResult("duplicate invoice: number", BLOCK,
                               f"{supplier_id} already has invoice number {bill_no} on "
                               f"record: {', '.join(same)}.", {"invoices": same}))
    else:
        out.append(CheckResult("duplicate invoice: number", PASS,
                               f"No other invoice {bill_no} from {supplier_id}."))
    p, near = policy.invoice, []
    for i in others:
        if key and normalise_bill_no(i.bill_no) == key:
            continue
        days = abs((i.bill_date - bill_date).days)
        if i.total == total and days <= p.dup_amount_days:
            near.append(f"{i.id} ({i.bill_no}, same total {usd(total)}, {days} days apart)")
        elif days <= p.near_days and i.total != total and (
                abs(i.total - total) <= p.near_pct / 100 * max(i.total, total)
                or _digits_key(i.total) == _digits_key(total)):
            near.append(f"{i.id} ({i.bill_no}, {usd(i.total)} against {usd(total)}, "
                        f"{days} days apart)")
    if near:
        out.append(CheckResult("duplicate invoice: amount", WARN,
                               f"Possible duplicate of {', '.join(sorted(near))}. Check it "
                               "isn't the same bill again.", {"invoices": sorted(near)}))
    else:
        out.append(CheckResult("duplicate invoice: amount", PASS,
                               "No invoice from this supplier with the same or a near total "
                               "in the window."))
    return out


def _truthy(v) -> bool:
    if isinstance(v, bool) or v is None:
        return bool(v)
    if isinstance(v, str) and not v.strip():
        return False
    try:
        return Decimal(str(v).strip()) != 0
    except InvalidOperation:
        return True


def paid_at_entry(fields_or_invoice: dict | Invoice) -> CheckResult:
    """`is_paid` would record the invoice as paid when entered, skipping every payment
    check."""
    v = fields_or_invoice.get("is_paid") if isinstance(fields_or_invoice, dict) \
        else fields_or_invoice.is_paid
    if _truthy(v):
        return CheckResult("paid at entry", BLOCK, "An invoice is never marked paid when "
                           "entered (is_paid); it is paid with pay_supplier after approval.")
    return CheckResult("paid at entry", PASS, "Not marked paid at entry.")


def invoice_separation(actor: str, inv: Invoice, po_by_id: dict[str, PurchaseOrder | None]
                       ) -> CheckResult:
    name = "invoice separation"
    problems = []
    if actor == inv.entered_by:
        problems.append(f"{actor} entered {inv.id}")
    requested = sorted(po.id for po in po_by_id.values()
                       if po is not None and po.requester == actor)
    if requested:
        problems.append(f"{actor} requested {', '.join(requested)}")
    if problems:
        return CheckResult(name, BLOCK, "; ".join(problems) + ", so can't approve the "
                           "invoice.", {"entered_by": inv.entered_by})
    return CheckResult(name, PASS, f"{actor} neither entered the invoice nor requested its "
                       "orders.")


def invoice_authority(actor: str, inv: Invoice, policy: Policy) -> CheckResult:
    name = "invoice authority"
    limit = policy.approver_limit(actor, "purchase_invoice")
    if limit is None:
        return CheckResult(name, BLOCK, f"{actor} can't approve supplier invoices.")
    if inv.total > limit:
        need = policy.required_tier(inv.total, "purchase_invoice")
        return CheckResult(name, BLOCK, f"{inv.id} totals {usd(inv.total)}; {actor}'s "
                           f"invoice limit is {usd(limit)}. Needs a {need.name}.",
                           {"limit": str(limit), "total": str(inv.total)})
    return CheckResult(name, PASS, f"Within {actor}'s invoice limit of {usd(limit)}.")


def debit_note_limit(original: Invoice, notes: list[Invoice], new_total: Decimal
                     ) -> CheckResult:
    """A debit note is at most the original invoice's amount not already credited (draft
    and posted notes count, cancelled ones don't)."""
    name = "debit note limit"
    credited = sum((n.total for n in _live_invoices(notes)
                    if n.is_return and n.return_against == original.id), Decimal(0))
    left = original.total - credited
    if new_total > left:
        return CheckResult(name, BLOCK, f"A debit note of {usd(new_total)} against "
                           f"{original.id} ({usd(original.total)}, {usd(credited)} already "
                           f"credited) is more than the {usd(left)} left.",
                           {"left": str(left)})
    return CheckResult(name, PASS, f"Within the {usd(left)} of {original.id} not yet "
                       "credited.")


def hold_rights(actor: str, policy: Policy) -> CheckResult:
    """Holds and releases are for invoice approvers (the controller role or an invoice
    limit)."""
    if "controller" in policy.roles.get(actor, set()) or \
            policy.approver_limit(actor, "purchase_invoice") is not None:
        return CheckResult("hold", PASS, f"{actor} approves supplier invoices.")
    return CheckResult("hold", BLOCK, f"Only invoice approvers hold or release invoices; "
                       f"{actor} isn't one.")
