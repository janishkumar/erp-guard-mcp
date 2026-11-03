"""Purchase-to-pay documents as the pack's checks see them, whatever the ERP.

Quantities and amounts are positive Decimals. A return (a goods return, or a debit note
against an invoice) says so with `is_return`, and its quantities are what goes back, also
positive; the checks subtract them. Lines are matched to the purchase order per
(PO, item code), summed over lines: an order never carries one item code twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from erp_guard.models import Line


class DocState(StrEnum):
    """A document's state. Documents with an approval workflow go Draft, Pending Approval,
    Approved or Rejected; SUBMITTED is for documents without one (receipts, requests for
    quotation, supplier quotations), which are submitted directly."""

    DRAFT = "Draft"
    PENDING = "Pending Approval"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    SUBMITTED = "Submitted"
    CANCELLED = "Cancelled"


# Documents in these states are posted: they count as received, invoiced or paid.
POSTED = frozenset({DocState.APPROVED, DocState.SUBMITTED})
# Not yet posted, but on their way: they count where drafts must (e.g. already received).
OPEN = frozenset({DocState.DRAFT, DocState.PENDING, DocState.REJECTED})


# What a draft may have changed (update_draft), by ERPNext field name: header fields, and
# per child table the row fields. Everything else is refused by every connector: amounts,
# parties, stock and payment flags are set only by the builders the checks ran against.
# ERPNext's Material Request has no notes field, so only its quantities change.
EDITABLE: dict[str, tuple[frozenset[str], dict[str, frozenset[str]]]] = {
    "Purchase Invoice": (frozenset({"bill_no", "bill_date", "remarks"}),
                         {"items": frozenset({"qty", "rate"})}),
    "Payment Entry": (frozenset({"remarks"}),
                      {"references": frozenset({"allocated_amount"})}),
    "Material Request": (frozenset(), {"items": frozenset({"qty"})}),
    "Supplier Quotation": (frozenset(), {"items": frozenset({"qty", "rate"})}),
}


def not_editable(doctype: str, fields: dict) -> list[str]:
    """The fields of `fields` (rows as table[].field) that EDITABLE doesn't allow."""
    if doctype not in EDITABLE:
        return sorted(fields) or ["(any field)"]
    header, tables = EDITABLE[doctype]
    bad = []
    for k, v in fields.items():
        if k in tables:
            if not isinstance(v, list) or not all(isinstance(r, dict) for r in v):
                bad.append(f"{k}[]")
                continue
            bad += [f"{k}[].{f}" for f in sorted({f for r in v for f in r} - tables[k])]
        elif k not in header:
            bad.append(k)
    return sorted(set(bad))


@dataclass(frozen=True)
class ItemInfo:
    code: str
    is_stock: bool
    stock_uom: str


@dataclass(frozen=True)
class RequestLine:
    item_code: str
    qty: Decimal


@dataclass(frozen=True)
class Request:
    """A purchase request (ERPNext: Material Request of type Purchase)."""

    id: str
    requester: str
    cost_centre: str
    state: DocState
    lines: tuple[RequestLine, ...]
    created: date

    def estimated_value(self, prices: dict[str, Decimal]) -> Decimal:
        """Quantity times each item's price (e.g. its last purchase price). An item with
        no price counts as nothing, so callers check `prices` covers every line."""
        return sum((ln.qty * prices.get(ln.item_code, Decimal(0)) for ln in self.lines),
                   Decimal(0))


@dataclass(frozen=True)
class Rfq:
    """A request for quotation: the suppliers asked to quote for a purchase request's
    items. It has no approval; it is submitted directly."""

    id: str
    request: str | None
    suppliers: tuple[str, ...]
    state: DocState
    created_by: str


@dataclass(frozen=True)
class Quote:
    """A supplier quotation, optionally in answer to a request for quotation."""

    id: str
    supplier_id: str
    rfq: str | None
    state: DocState
    lines: tuple[Line, ...]
    quoted_on: date
    entered_by: str = ""


@dataclass(frozen=True)
class ReceiptLine:
    po: str
    item_code: str
    qty: Decimal


@dataclass(frozen=True)
class Receipt:
    """A goods receipt, or with `is_return` a return of goods received on
    `return_against`."""

    id: str
    supplier_id: str
    receiver: str
    state: DocState
    posted: date
    lines: tuple[ReceiptLine, ...]
    is_return: bool = False
    return_against: str | None = None


@dataclass(frozen=True)
class InvoiceLine:
    po: str
    item_code: str
    qty: Decimal
    rate: Decimal
    receipt: str | None = None

    @property
    def amount(self) -> Decimal:
        return self.qty * self.rate


@dataclass(frozen=True)
class Invoice:
    """A supplier invoice, or with `is_return` a debit note against `return_against`."""

    id: str
    supplier_id: str
    bill_no: str
    bill_date: date
    entered_by: str
    state: DocState
    lines: tuple[InvoiceLine, ...]
    approver: str | None = None
    on_hold: bool = False
    is_return: bool = False
    return_against: str | None = None
    is_paid: bool = False
    remarks: str = ""
    posted: date | None = None  # ERPNext's posting date; the bill date when not known

    @property
    def total(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), Decimal(0))

    @property
    def posting_date(self) -> date:
        return self.posted or self.bill_date


@dataclass(frozen=True)
class Allocation:
    invoice: str
    amount: Decimal


@dataclass(frozen=True)
class Payment:
    """A payment to a supplier (ERPNext: Payment Entry, Pay), allocated to invoices."""

    id: str
    supplier_id: str
    payer: str
    state: DocState
    posted: date
    paid_from: str
    allocations: tuple[Allocation, ...]
    approver: str | None = None
    unallocated: Decimal = Decimal(0)

    @property
    def amount(self) -> Decimal:
        return sum((a.amount for a in self.allocations), self.unallocated)
