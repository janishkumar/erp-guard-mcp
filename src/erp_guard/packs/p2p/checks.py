"""Purchase-to-pay checks. Pure functions: documents already read from the ERP, plus the
policy, in; results out. They share `erp_guard.checks`' result type and levels."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from erp_guard.checks import BLOCK, PASS, WARN, CheckResult
from erp_guard.models import LIVE_STATUSES, PurchaseOrder, Status, Supplier
from erp_guard.packs.p2p.models import (
    POSTED,
    Allocation,
    DocState,
    Invoice,
    InvoiceLine,
    ItemInfo,
    Payment,
    Quote,
    Receipt,
    ReceiptLine,
    Request,
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


_BILL_SEPARATORS = re.compile(r"[\s\-_/.#]")
_BILL_PREFIX = re.compile(r"^(?:INVOICE|INV|NO)+(?=\d)")
# Leading zeros of a run of digits that starts the number or follows a letter; zeros inside
# a run of digits are kept, so "2026-0042" and "20260042" stay the same number.
_LEADING_ZEROS = re.compile(r"(?<!\d)0+(?=\d)")


def normalise_bill_no(s: str) -> str:
    """A supplier's invoice number as compared for duplicates: upper case, without spaces
    and punctuation, without a leading INV / INVOICE / NO before the digits, and without
    leading zeros on a run of digits that starts it or follows a letter. "INV-01001",
    "inv 1001" and "1001" are all "1001"; "A-01001" and "A-1001" are both "A1001"; "2026-0042"
    and "20260042" are the same."""
    t = _BILL_SEPARATORS.sub("", (s or "").upper())
    t = _BILL_PREFIX.sub("", t)
    return _LEADING_ZEROS.sub("", t)


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
    """Per (PO, item): invoices of every state but cancelled add. Only approved (posted)
    debit notes give room back, and only where the goods went back after billing: for a
    stock item, credited quantity counts up to the quantity on posted returns dated on or
    after the first invoice for that (PO, item), at the credit's average rate (a credit
    for goods kept, or for goods returned before they were ever billed, is not room to bill
    again). For a service, approved credits count in full, but outside the strict figures."""
    billed_q: dict = defaultdict(Decimal)
    billed_a: dict = defaultdict(Decimal)
    credit_q: dict = defaultdict(Decimal)
    credit_a: dict = defaultdict(Decimal)
    first: dict[tuple[str, str], date] = {}
    for inv in _live_invoices(invoices):
        if inv.is_return:
            if inv.state not in POSTED:
                continue
            q, a = credit_q, credit_a
        else:
            q, a = billed_q, billed_a
        for ln in inv.lines:
            key = (ln.po, ln.item_code)
            q[key] += ln.qty
            a[key] += ln.amount
            if not inv.is_return:
                first[key] = min(first.get(key, inv.posting_date), inv.posting_date)
    out = {}
    for key in set(billed_q) | set(credit_q):
        cq, ca = credit_q.get(key, Decimal(0)), credit_a.get(key, Decimal(0))
        bq, ba = billed_q.get(key, Decimal(0)), billed_a.get(key, Decimal(0))
        if is_stock(items, key[1]):
            since = first.get(key)
            returned = sum((ln.qty for r in receipts
                            if r.is_return and r.state in POSTED and since is not None
                            and r.posted >= since
                            for ln in r.lines if (ln.po, ln.item_code) == key), Decimal(0))
            back = min(cq, returned)
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


# --- payments --------------------------------------------------------------------------

def payable_invoices(supplier_id: str, allocations: list[Allocation],
                     invoices_by_id: dict[str, Invoice | None],
                     outstanding: dict[str, Decimal],
                     unallocated: Decimal = Decimal(0)) -> list[CheckResult]:
    """Pay only approved invoices: every allocation is to an approved (posted) invoice of
    this supplier, not a debit note, not on hold, with something still owed; nothing is
    paid without an invoice or left unallocated (an advance). Amount owed: each allocation
    is at most the invoice's outstanding amount."""
    name, owed_name = "pay only approved invoices", "amount owed"
    if not allocations or unallocated > 0:
        what = "no invoice" if not allocations else f"{usd(unallocated)} unallocated"
        return [CheckResult(name, BLOCK, f"The payment has {what}; advance payments are not "
                            "allowed. Every amount is allocated to an approved invoice.")]
    # An entry made in the ERP's UI can reference one invoice on several rows: what it pays
    # an invoice is the sum of them.
    paying: dict[str, Decimal] = {}
    for a in allocations:
        paying[a.invoice] = paying.get(a.invoice, Decimal(0)) + a.amount
    problems, over = [], []
    for ref, amount in paying.items():
        inv = invoices_by_id.get(ref)
        left = outstanding.get(ref, Decimal(0))
        if inv is None:
            problems.append(f"No purchase invoice '{ref}'")
        elif inv.is_return:
            problems.append(f"{inv.id} is a debit note")
        elif inv.state not in POSTED:
            problems.append(f"{inv.id} is {inv.state.value}, not approved")
        elif inv.supplier_id != supplier_id:
            problems.append(f"{inv.id} is {inv.supplier_id}'s invoice")
        elif inv.on_hold:
            problems.append(f"{inv.id} is on hold")
        elif left <= 0:
            problems.append(f"nothing is owed on {inv.id}")
        elif amount > left:
            over.append(f"{inv.id}: paying {usd(amount)} but {usd(left)} is owed")
    out = [CheckResult(name, BLOCK, "; ".join(problems) + ".",
                       {"invoices": [a.invoice for a in allocations]}) if problems else
           CheckResult(name, PASS, f"Every invoice is an approved invoice of {supplier_id} "
                       "with something owed.")]
    out.append(CheckResult(owed_name, BLOCK, "; ".join(over) + ".") if over else
               CheckResult(owed_name, PASS, "Each amount is within what is owed."))
    return out


def pay_to_supplier(payment_type: str, party_type: str) -> CheckResult:
    """A payment is a Pay to a Supplier. erp-guard drafts nothing else, but an entry made in
    the ERP's own UI (a Receive, an Internal Transfer, a Pay to an Employee) blocks."""
    name = "pay a supplier"
    if (payment_type, party_type) != ("Pay", "Supplier"):
        return CheckResult(name, BLOCK, f"This entry is a {payment_type or '(no type)'} to "
                           f"a {party_type or '(no party type)'}; only a Pay to a Supplier "
                           "goes through erp-guard.")
    return CheckResult(name, PASS, "A Pay to a Supplier.")


def duplicate_payment(allocations: list[Allocation], payments: list[Payment],
                      supplier_id: str, today: date, days: int) -> list[CheckResult]:
    """Another live payment (draft, pending, rejected awaiting revision, or approved; not
    cancelled or voided) already referencing one of these invoices blocks. The same supplier
    paid the same amount for other invoices within `days` warns (Spend Leakage Finder's T2
    rule, by supplier ID)."""
    live = [p for p in payments if p.state != DocState.CANCELLED]
    refs = {a.invoice for a in allocations}
    clash = sorted(f"{p.id} ({', '.join(sorted(refs & {a.invoice for a in p.allocations}))}, "
                   f"{p.state.value})" for p in live
                   if refs & {a.invoice for a in p.allocations})
    out = [CheckResult("duplicate payment", BLOCK, "Already being paid or paid: "
                       + "; ".join(clash) + ". An invoice is paid by one payment; change or "
                       "void that one instead.", {"payments": clash}) if clash else
           CheckResult("duplicate payment", PASS, "No other payment refers to these "
                       "invoices.")]
    total = sum((a.amount for a in allocations), Decimal(0))
    since = today - timedelta(days=days)
    near = sorted(f"{p.id} ({', '.join(a.invoice for a in p.allocations)}, "
                  f"{p.posted.isoformat()})" for p in live
                  if p.supplier_id == supplier_id and p.amount == total and p.posted >= since
                  and not refs & {a.invoice for a in p.allocations})
    if near:
        out.append(CheckResult("duplicate payment: amount", WARN,
                               f"{supplier_id} was paid the same {usd(total)} for other "
                               f"invoices in the last {days} days: {', '.join(near)}. Check "
                               "it isn't the same bill paid again.", {"payments": near}))
    else:
        out.append(CheckResult("duplicate payment: amount", PASS, "No payment of the same "
                               f"amount to {supplier_id} in the last {days} days."))
    return out


def payer_separation(actor: str, invoices_by_id: dict[str, Invoice | None],
                     po_by_id: dict[str, PurchaseOrder | None]) -> CheckResult:
    """The payer approved none of the invoices and requested none of their orders."""
    name = "payer separation"
    problems = []
    approved = sorted(i.id for i in invoices_by_id.values() if i is not None
                      and i.approver == actor)
    if approved:
        problems.append(f"{actor} approved {', '.join(approved)}")
    requested = sorted(po.id for po in po_by_id.values()
                       if po is not None and po.requester == actor)
    if requested:
        problems.append(f"{actor} requested {', '.join(requested)}")
    if problems:
        return CheckResult(name, BLOCK, "; ".join(problems) + ", so can't also pay for it.")
    return CheckResult(name, PASS, f"{actor} neither approved the invoices nor requested "
                       "their orders.")


def payment_authority(actor: str, payment: Payment, policy: Policy) -> CheckResult:
    name = "payment authority"
    if actor == payment.payer:
        return CheckResult(name, BLOCK, f"{actor} entered {payment.id} as the payer and "
                           "can't approve it.", {"payer": payment.payer})
    limit = policy.approver_limit(actor, "payment")
    if limit is None:
        return CheckResult(name, BLOCK, f"{actor} can't approve payments.")
    if payment.amount > limit:
        need = policy.required_tier(payment.amount, "payment")
        return CheckResult(name, BLOCK, f"{payment.id} is {usd(payment.amount)}; {actor}'s "
                           f"payment limit is {usd(limit)}. Needs a {need.name}.",
                           {"limit": str(limit), "amount": str(payment.amount)})
    return CheckResult(name, PASS, f"Within {actor}'s payment limit of {usd(limit)}.")


# The payment tier that approves the first payment to a newly onboarded supplier.
NEW_SUPPLIER_TIER = "director"


def new_supplier(actor: str, supplier: Supplier, prior_payments: list[Payment], today: date,
                 policy: Policy) -> CheckResult:
    """A supplier onboarded through erp-guard in the last `new_supplier_days` with no
    approved payment yet: its first payment is approved from the director tier up. Suppliers
    already in the master (no onboarding date) are established."""
    name, days = "new supplier", policy.payment.new_supplier_days
    since = supplier.onboarded_on
    if since is None or (today - since).days > days:
        return CheckResult(name, PASS, f"{supplier.name} is an established supplier.")
    if any(p.state in POSTED and p.supplier_id == supplier.id for p in prior_payments):
        return CheckResult(name, PASS, f"{supplier.name} has been paid before.")
    need = next((t.limit for t in policy.approval["payment"] if t.name == NEW_SUPPLIER_TIER),
                None)
    if need is None:
        raise KeyError(f"no '{NEW_SUPPLIER_TIER}' tier in [approval.payment]")
    limit = policy.approver_limit(actor, "payment")
    if limit is None or limit < need:
        return CheckResult(name, BLOCK, f"{supplier.name} was onboarded on "
                           f"{since.isoformat()} and has never been paid: its first payment "
                           f"is approved by a {NEW_SUPPLIER_TIER} or above.",
                           {"onboarded_on": since.isoformat()})
    return CheckResult(name, PASS, f"First payment to {supplier.name} (onboarded "
                       f"{since.isoformat()}), approved by a {NEW_SUPPLIER_TIER} or above.")


def paying_account(paid_from: str, policy: Policy) -> CheckResult:
    want = policy.payment.bank_account
    if paid_from != want:
        return CheckResult("paying account", BLOCK, f"Paid from '{paid_from}'; payments are "
                           f"made only from the company's account {want}.")
    return CheckResult("paying account", PASS, f"Paid from {want}.")


# --- purchase requests and quotes ------------------------------------------------------

def request_required(order: PurchaseOrder, request: Request | None,
                     policy: Policy) -> CheckResult:
    """An order of `request_required_from` or more is made from an approved purchase
    request."""
    name, need = "request required", policy.request_required_from
    if order.total < need:
        return CheckResult(name, PASS, f"Under {usd(need)}: no purchase request needed.")
    if order.request is None:
        return CheckResult(name, BLOCK, f"This order is {usd(order.total)}; orders of "
                           f"{usd(need)} or more are made from an approved purchase request. "
                           "Raise one with raise_purchase_request, have it approved, and "
                           "pass it as `request`.", {"total": str(order.total)})
    if request is None or request.state != DocState.APPROVED:
        what = "not found" if request is None else request.state.value
        return CheckResult(name, BLOCK, f"Purchase request {order.request} is {what}; orders "
                           f"of {usd(need)} or more need an approved one.",
                           {"request": order.request})
    return CheckResult(name, PASS, f"Made from approved purchase request {request.id}.")


def request_split(order: PurchaseOrder, recent: list[PurchaseOrder], policy: Policy,
                  today: date) -> CheckResult:
    """v1's split rule applied to the request threshold: a request-less order under it,
    with the other live request-less orders to the same supplier and cost centre in the
    split window, mustn't reach it."""
    name, need = "request split", policy.request_required_from
    if order.request is not None:
        return CheckResult(name, PASS, "Made from a purchase request.")
    if order.total >= need:
        return CheckResult(name, PASS, f"{usd(need)} or more: needs a request of its own.")
    since = today - timedelta(days=policy.split_window_days)
    others = [o for o in recent
              if o.id != order.id and o.request is None
              and o.supplier_id == order.supplier_id and o.cost_centre == order.cost_centre
              and o.status in LIVE_STATUSES and o.order_date >= since]
    combined = order.total + sum((o.total for o in others), Decimal(0))
    evidence = {"related_orders": [o.id for o in others], "combined": str(combined)}
    if others and combined >= need:
        return CheckResult(
            name, BLOCK,
            f"Possible split to avoid a purchase request. This order ({usd(order.total)}) "
            f"and {len(others)} other order(s) without a request to the same supplier and "
            f"cost centre in the last {policy.split_window_days} days "
            f"({', '.join(o.id for o in others)}) come to {usd(combined)}; {usd(need)} or "
            "more is bought through an approved purchase request. Raise one request for the "
            "whole need and order from it.", evidence)
    return CheckResult(name, PASS, f"Together with {len(others)} related order(s) without a "
                       f"request, {usd(combined)}: under {usd(need)}.", evidence)


def request_quantity(order: PurchaseOrder, request: Request | None,
                     orders_from_request: list[PurchaseOrder]) -> CheckResult:
    """An order made from a request: the request is approved and for the order's cost
    centre, and each item's quantity is at most what it approved less what other live
    orders from it already order."""
    name = "request approved"
    if order.request is None:
        return CheckResult(name, PASS, "Not made from a purchase request.")
    if request is None:
        return CheckResult(name, BLOCK, f"No purchase request '{order.request}'.")
    if request.state != DocState.APPROVED:
        return CheckResult(name, BLOCK, f"{request.id} is {request.state.value}; orders are "
                           "made only from approved purchase requests.")
    if request.cost_centre != order.cost_centre:
        return CheckResult(name, BLOCK, f"{request.id} is for cost centre "
                           f"{request.cost_centre}; this order charges {order.cost_centre}.")
    approved = _per_item(request.lines)
    ordered: dict[str, Decimal] = defaultdict(Decimal)
    others = [o for o in orders_from_request if o.id != order.id
              and o.request == request.id and o.status in LIVE_STATUSES]
    for o in others:
        for ln in o.lines:
            ordered[ln.item_code] += ln.qty
    problems = []
    for item, qty in _per_item(order.lines).items():
        if item not in approved:
            problems.append(f"{item} is not on {request.id}")
            continue
        left = max(approved[item] - ordered[item], Decimal(0))
        if qty > left:
            problems.append(f"{item}: ordering {_qty(qty)}, but only {_qty(left)} of "
                            f"{_qty(approved[item])} approved isn't ordered yet")
    evidence = {"request": request.id, "other_orders": [o.id for o in others]}
    if problems:
        return CheckResult(name, BLOCK, "; ".join(problems) + ".", evidence)
    return CheckResult(name, PASS, f"Within what {request.id} approved and isn't ordered yet.",
                       evidence)


def request_authority(actor: str, request: Request, prices: dict[str, Decimal],
                      policy: Policy) -> CheckResult:
    """The approver didn't raise the request, and their purchase limit covers its estimated
    value (each item's last purchase price times quantity). Items with no purchase history
    are left out of the estimate, and say so: the order made from the request still needs
    approval at its own total."""
    name = "request approval tier"
    if actor == request.requester:
        return CheckResult(name, BLOCK, f"{actor} raised {request.id} and can't approve it.",
                           {"requester": request.requester})
    limit = policy.approver_limit(actor, "purchase_request")
    if limit is None:
        return CheckResult(name, BLOCK, f"{actor} can't approve purchase requests.")
    value = request.estimated_value(prices)
    if value > limit:
        need = policy.required_tier(value, "purchase_request")
        return CheckResult(name, BLOCK, f"{request.id} is estimated at {usd(value)} (last "
                           f"purchase prices); {actor}'s limit is {usd(limit)}. Needs a "
                           f"{need.name}.", {"estimate": str(value), "limit": str(limit)})
    unpriced = sorted({ln.item_code for ln in request.lines if not prices.get(ln.item_code)})
    if unpriced:
        return CheckResult(name, WARN, f"Estimated at {usd(value)}, within {actor}'s limit of "
                           f"{usd(limit)}, but {', '.join(unpriced)} has no purchase history, "
                           "so isn't in the estimate. The order made from this request is "
                           "approved at its own total.", {"unpriced": unpriced})
    return CheckResult(name, PASS, f"Estimated at {usd(value)} (last purchase prices), within "
                       f"{actor}'s limit of {usd(limit)}.")


def counted_quotes(order: PurchaseOrder, quotes: list[Quote], policy: Policy, today: date,
                   suppliers: dict[str, Supplier | None]) -> tuple[list[Quote], list[str]]:
    """The quotes that count as competition for an order: submitted, dated in the last
    `lookback_days` and not after today, from a supplier that is usable now (in the
    master, not disabled or on hold) and wasn't onboarded by the order's requester. Also
    the suppliers left out, and why."""
    since = today - timedelta(days=policy.quotes.lookback_days)
    out, left_out = [], set()
    for qt in quotes:
        if qt.state != DocState.SUBMITTED or not since <= qt.quoted_on <= today:
            continue
        sup = suppliers.get(qt.supplier_id)
        if sup is None or sup.disabled or sup.on_hold:
            left_out.add(f"{qt.supplier_id} (not usable)")
        elif sup.onboarded_by and sup.onboarded_by == order.requester:
            left_out.add(f"{qt.supplier_id} (onboarded by {order.requester})")
        else:
            out.append(qt)
    return out, sorted(left_out)


def competitive_quotes(order: PurchaseOrder, quotes: list[Quote], policy: Policy,
                       today: date, *, suppliers: dict[str, Supplier | None]) -> CheckResult:
    """An order of the quotes threshold or more: at least `count` suppliers, the order's
    own among them, have quotations that count (counted_quotes) and together cover every
    item on it. Or the order gives a sole-source reason (approved from the director tier
    up)."""
    name, q = "competitive quotes", policy.quotes
    if order.total < q.threshold:
        return CheckResult(name, PASS, f"Under {usd(q.threshold)}: no competing quotes needed.")
    if order.sole_source_reason:
        return CheckResult(name, PASS, "Sole source: a reason is given instead of competing "
                           f"quotes, so a {SOLE_SOURCE_TIER} or above approves the order.")
    counted, left_out = counted_quotes(order, quotes, policy, today, suppliers)
    items = {ln.item_code for ln in order.lines}
    covered: dict[str, set[str]] = defaultdict(set)
    for qt in counted:
        covered[qt.supplier_id] |= {ln.item_code for ln in qt.lines}
    full = sorted(s for s, quoted in covered.items() if items <= quoted)
    evidence = {"suppliers": full, "needed": q.count, "not_counted": left_out}
    if len(full) < q.count:
        found = f"Found {len(full)}" + (f": {', '.join(full)}" if full else "")
        if left_out:
            found += f" (not counted: {', '.join(left_out)})"
        return CheckResult(name, BLOCK, f"This order is {usd(order.total)}; orders of "
                           f"{usd(q.threshold)} or more need submitted supplier quotations "
                           f"for all their items from {q.count} different suppliers in the "
                           f"last {q.lookback_days} days, or a sole-source reason. {found}.",
                           evidence)
    if order.supplier_id not in full:
        return CheckResult(name, BLOCK, f"{len(full)} suppliers quoted ({', '.join(full)}), "
                           f"but not {order.supplier_id}, the supplier on this order.",
                           evidence)
    return CheckResult(name, PASS, f"{len(full)} suppliers quoted for these items in the last "
                       f"{q.lookback_days} days: {', '.join(full)}.", evidence)


# The purchase tier that approves a sole-source order.
SOLE_SOURCE_TIER = "director"


def sole_source_approver(actor: str, order: PurchaseOrder, policy: Policy) -> CheckResult:
    name = "sole source"
    if not order.sole_source_reason:
        return CheckResult(name, PASS, "Not a sole-source order.")
    need = next((t.limit for t in policy.approval["purchase_order"]
                 if t.name == SOLE_SOURCE_TIER), None)
    if need is None:
        raise KeyError(f"no '{SOLE_SOURCE_TIER}' tier in [approval.purchase_order]")
    limit = policy.approver_limit(actor)
    if limit is None or limit < need:
        return CheckResult(name, BLOCK, f"{order.id} is a sole-source order (no competing "
                           f"quotes); it is approved by a {SOLE_SOURCE_TIER} or above.")
    return CheckResult(name, PASS, f"Sole-source order, approved by a {SOLE_SOURCE_TIER} or "
                       "above.")


def quote_price(order: PurchaseOrder, quotes_by_id: dict[str, Quote | None],
                supplier_quotes: list[Quote] = ()) -> CheckResult:
    """An order made from supplier quotations: each is a submitted quotation of the order's
    supplier, every item is on one, and no rate is above the lowest quoted for it. An order
    that needed competing quotes (`supplier_quotes`: the order's supplier's quotes that
    counted) is capped at the supplier's quoted rate the same way, linked or not."""
    name = "quote price"
    supplier_quotes = [qt for qt in supplier_quotes if qt.supplier_id == order.supplier_id]
    if not order.quotations and not supplier_quotes:
        return CheckResult(name, PASS, "Not made from a supplier quotation.")
    problems = []
    rates: dict[str, list[Decimal]] = defaultdict(list)
    for qid in order.quotations:
        qt = quotes_by_id.get(qid)
        if qt is None:
            problems.append(f"No supplier quotation '{qid}'")
        elif qt.state != DocState.SUBMITTED:
            problems.append(f"{qid} is {qt.state.value}, not submitted")
        elif qt.supplier_id != order.supplier_id:
            problems.append(f"{qid} is {qt.supplier_id}'s quotation, not "
                            f"{order.supplier_id}'s")
        else:
            for ln in qt.lines:
                rates[ln.item_code].append(ln.rate)
    for qt in supplier_quotes:
        for ln in qt.lines:
            rates[ln.item_code].append(ln.rate)
    if not problems:
        for ln in order.lines:
            quoted = rates.get(ln.item_code)
            if not quoted:
                if order.quotations:
                    problems.append(f"{ln.item_code} isn't on "
                                    f"{', '.join(order.quotations)}")
            elif ln.rate > min(quoted):
                problems.append(f"{ln.item_code} at {usd(ln.rate)} is above the quoted "
                                f"{usd(min(quoted))}")
    if problems:
        return CheckResult(name, BLOCK, "; ".join(problems) + ".",
                           {"quotations": list(order.quotations)})
    return CheckResult(name, PASS, "Every rate is at or below the quoted rate.")


# --- closing a purchase order ----------------------------------------------------------

def close_po_state(po: PurchaseOrder | None, ref: str) -> CheckResult:
    name = "state"
    if po is None:
        return CheckResult(name, BLOCK, f"No purchase order '{ref}'.")
    if po.status != Status.APPROVED or po.closed:
        now = "closed" if po.closed and po.status == Status.APPROVED else po.status.value
        return CheckResult(name, BLOCK, f"{po.id} is {now}; closing needs an approved, open "
                           "order.")
    return CheckResult(name, PASS, f"{po.id} is approved and open.")


def close_po(actor: str, po: PurchaseOrder, receipts: list[Receipt], invoices: list[Invoice],
             items: dict[str, ItemInfo], policy: Policy, reason: str) -> CheckResult:
    """An order is closed when nothing is still on its way and either it is complete or an
    approver says why it isn't.

    Receipts and invoices (debit notes too) that are neither posted nor cancelled are open
    work: drafts, pending approval and rejected ones (a rejected document can still be
    revised and resubmitted), so any of them blocks, failing closed. Complete means the
    three-way match has nothing left to do, by the same arithmetic: a stock item is
    received (posted receipts less returns) up to the ordered quantity and everything
    received is invoiced; a service is invoiced up to the ordered quantity. A complete
    order is closed by its requester or any PO approver. An incomplete one needs a reason
    and an approver."""
    name = "close"
    open_rcv = sorted(r.id for r in receipts
                      if r.state not in POSTED and r.state != DocState.CANCELLED)
    open_inv = sorted(i.id for i in invoices
                      if i.state not in POSTED and i.state != DocState.CANCELLED)
    if open_rcv or open_inv:
        what = [f"receipt(s) {', '.join(open_rcv)}" if open_rcv else "",
                f"invoice(s) {', '.join(open_inv)}" if open_inv else ""]
        return CheckResult(name, BLOCK,
                           f"{po.id} still has unposted {' and '.join(w for w in what if w)}"
                           "; post or cancel them first.",
                           {"receipts": open_rcv, "invoices": open_inv})
    received, done = received_qty(receipts), invoiced(invoices, receipts, items)
    outstanding = []
    for item, ordered in sorted(_per_item(po.lines).items()):
        billed = done.get((po.id, item), Invoiced()).qty
        if is_stock(items, item):
            got = received.get((po.id, item), Decimal(0))
            if got < ordered:
                outstanding.append(f"{item}: received {_qty(got)} of {_qty(ordered)}")
            if billed < got:
                outstanding.append(f"{item}: invoiced {_qty(billed)} of {_qty(got)} received "
                                   f"({_qty(ordered)} ordered)")
        elif billed < ordered:
            outstanding.append(f"{item}: invoiced {_qty(billed)} of {_qty(ordered)}")
    approver = policy.approver_limit(actor, "purchase_order") is not None
    if not outstanding:
        if actor == po.requester or approver:
            return CheckResult(name, PASS, f"{po.id} is fully received and invoiced; "
                               f"{actor} can close it.")
        return CheckResult(name, BLOCK, f"Only the requester or an approver can close {po.id}; "
                           f"{actor} is neither.", {"requester": po.requester})
    left = "; ".join(outstanding)
    problems = []
    if not approver:
        problems.append("only an approver can close an order that isn't fully received and "
                        f"invoiced, and {actor} isn't one")
    if not reason.strip():
        problems.append("a reason is needed")
    if problems:
        return CheckResult(name, BLOCK, f"{po.id} isn't fully received and invoiced ({left}): "
                           + "; ".join(problems) + ".", {"outstanding": outstanding})
    return CheckResult(name, WARN, f"{po.id} is closed with work outstanding ({left}). "
                       f"Reason given: {reason.strip()}", {"outstanding": outstanding})
