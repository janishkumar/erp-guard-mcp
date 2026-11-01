"""ERP-neutral records the checks and connectors share."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Status(StrEnum):
    """erp-guard's view of an order's lifecycle, mapped onto each ERP's own states."""

    DRAFT = "Draft"
    PENDING = "Pending Approval"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    CANCELLED = "Cancelled"


# Orders in these states still commit budget and count towards split detection.
LIVE_STATUSES = {Status.DRAFT, Status.PENDING, Status.APPROVED}


@dataclass(frozen=True)
class Supplier:
    id: str
    name: str
    tax_id: str | None = None
    disabled: bool = False
    on_hold: bool = False
    onboarded_by: str | None = None
    notes: str = ""
    onboarded_on: date | None = None


@dataclass(frozen=True)
class Line:
    item_code: str
    qty: Decimal
    rate: Decimal
    description: str = ""

    @property
    def amount(self) -> Decimal:
        return self.qty * self.rate


@dataclass(frozen=True)
class PurchaseOrder:
    id: str
    supplier_id: str
    cost_centre: str
    requester: str
    status: Status
    order_date: date
    lines: tuple[Line, ...]
    notes: str = ""
    approver: str | None = None
    # Purchase to pay: the purchase request and supplier quotations the order was made
    # from, why there's no competition if so, and whether it was closed.
    request: str | None = None
    quotations: tuple[str, ...] = ()
    sole_source_reason: str = ""
    closed: bool = False

    @property
    def total(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), Decimal(0))


@dataclass(frozen=True)
class CostCentre:
    id: str
    annual_budget: Decimal


@dataclass(frozen=True)
class AuditRecord:
    action: str
    actor: str
    outcome: str  # written | blocked | refused | error
    payload_hash: str
    checks: list[dict]
    target: str | None = None
    detail: str = ""
    timestamp: str = ""
    id: str | None = None


@dataclass
class Snapshot:
    """What the checks need to know about the ERP, gathered by the service per action."""

    actor: str
    today: date
    suppliers: list[Supplier] = field(default_factory=list)
    supplier: Supplier | None = None
    order: PurchaseOrder | None = None
    recent_orders: list[PurchaseOrder] = field(default_factory=list)
    cost_centre: CostCentre | None = None
    committed: Decimal = Decimal(0)
    last_prices: dict[str, Decimal] = field(default_factory=dict)
