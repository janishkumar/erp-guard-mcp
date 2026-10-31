"""In-process ERP for unit tests and the fast eval run. Same protocol as the real ones."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal

from po_guard.connectors.base import ErpError
from po_guard.models import AuditRecord, CostCentre, Line, PurchaseOrder, Status, Supplier


class MemoryErp:
    name = "memory"

    def __init__(self, user: str = "requester@example.com", today: date | None = None):
        self.user = user
        self.today = today or date.today()
        self.suppliers: dict[str, Supplier] = {}
        self.orders: dict[str, PurchaseOrder] = {}
        self.cost_centres: dict[str, CostCentre] = {}
        self.items: set[str] = set()
        self.audit: list[AuditRecord] = []
        self._seq = {"SUP": 0, "PO": 0, "AUD": 0}

    def as_user(self, user: str) -> MemoryErp:
        """A view of the same ERP state, acting as another user (shared storage)."""
        view = object.__new__(MemoryErp)
        view.__dict__ = self.__dict__.copy()
        view.user = user
        return view

    def _next(self, kind: str) -> str:
        self._seq[kind] += 1
        return f"{kind}-{self._seq[kind]:05d}"

    # reads -----------------------------------------------------------------------------
    def whoami(self) -> str:
        return self.user

    def list_suppliers(self) -> list[Supplier]:
        return list(self.suppliers.values())

    def get_supplier(self, supplier_id: str) -> Supplier | None:
        return self.suppliers.get(supplier_id)

    def get_order(self, order_id: str) -> PurchaseOrder | None:
        return self.orders.get(order_id)

    def list_orders(self, *, supplier_id=None, cost_centre=None, since=None):
        return [
            o for o in self.orders.values()
            if (supplier_id is None or o.supplier_id == supplier_id)
            and (cost_centre is None or o.cost_centre == cost_centre)
            and (since is None or o.order_date >= since)
        ]

    def get_cost_centre(self, cost_centre: str) -> CostCentre | None:
        return self.cost_centres.get(cost_centre)

    def item_codes(self) -> set[str]:
        return set(self.items)

    def last_purchase_prices(self, item_codes):
        out: dict[str, Decimal] = {}
        approved = sorted((o for o in self.orders.values() if o.status == Status.APPROVED),
                          key=lambda o: (o.order_date, o.id))
        for o in approved:
            for ln in o.lines:
                if ln.item_code in item_codes:
                    out[ln.item_code] = ln.rate
        return out

    def read_audit(self, target=None, limit=50):
        rows = [a for a in self.audit if target is None or a.target == target]
        return list(reversed(rows))[:limit]

    # writes ----------------------------------------------------------------------------
    def create_supplier(self, name, tax_id, notes, onboarded_by):
        s = Supplier(self._next("SUP"), name, tax_id, notes=notes, onboarded_by=onboarded_by)
        self.suppliers[s.id] = s
        return s

    def create_order(self, supplier_id, cost_centre, lines, notes, required_by):
        o = PurchaseOrder(self._next("PO"), supplier_id, cost_centre, self.user, Status.DRAFT,
                          self.today, tuple(lines), notes)
        self.orders[o.id] = o
        return o

    def _replace(self, order_id: str, **changes) -> PurchaseOrder:
        if order_id not in self.orders:
            raise ErpError(f"No purchase order {order_id}")
        o = dataclasses.replace(self.orders[order_id], **changes)
        self.orders[order_id] = o
        return o

    def set_status(self, order_id, status, reason):
        return self._replace(order_id, status=status)

    def approve_order(self, order_id):
        return self._replace(order_id, status=Status.APPROVED, approver=self.user)

    def cancel_order(self, order_id, reason):
        return self._replace(order_id, status=Status.CANCELLED)

    def amend_order(self, order_id, lines, notes):
        return self._replace(order_id, lines=tuple(lines), notes=notes, status=Status.DRAFT)

    def write_audit(self, record):
        rid = self._next("AUD")
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        self.audit.append(dataclasses.replace(record, id=rid, timestamp=ts))
        return rid

    # seeding (not part of the protocol) ------------------------------------------------
    def add_supplier(self, s: Supplier) -> None:
        self.suppliers[s.id] = s

    def add_order(self, o: PurchaseOrder) -> None:
        self.orders[o.id] = o

    def add_cost_centre(self, cc: CostCentre) -> None:
        self.cost_centres[cc.id] = cc

    def add_items(self, codes) -> None:
        self.items.update(codes)


def line(item_code: str, qty, rate, description: str = "") -> Line:
    return Line(item_code, Decimal(str(qty)), Decimal(str(rate)), description)
