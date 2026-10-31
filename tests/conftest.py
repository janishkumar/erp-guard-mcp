from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from po_guard.connectors.memory import MemoryErp, line
from po_guard.models import CostCentre, PurchaseOrder, Status, Supplier
from po_guard.policy import load_policy
from po_guard.service import Service

TODAY = date(2026, 10, 29)
REQ = "requester@example.com"
REQ2 = "requester2@example.com"
MGR = "manager@example.com"
DIR = "director@example.com"


@pytest.fixture
def policy():
    return load_policy()


@pytest.fixture
def erp():
    e = MemoryErp(user=REQ, today=TODAY)
    e.add_items({"LAPTOP-14", "TONER-K", "SALT-TON", "CONSULT-HR"})
    e.add_cost_centre(CostCentre("Fleet", Decimal("500000")))
    e.add_cost_centre(CostCentre("IT", Decimal("200000")))
    e.add_supplier(Supplier("SUP-HERC", "HERC RENTALS INC", "36-1111111"))
    e.add_supplier(Supplier("SUP-CANON", "CANON U.S.A., INC.", "11-2222222"))
    e.add_supplier(Supplier("SUP-HOLD", "MORTON SALT INC", on_hold=True))
    e.add_supplier(Supplier("SUP-MGR", "QUICK FIX SERVICES LLC", onboarded_by=MGR))
    # History: an approved order sets last prices and uses some IT budget.
    e.add_order(PurchaseOrder("PO-HIST1", "SUP-CANON", "IT", REQ2, Status.APPROVED,
                              TODAY - timedelta(days=60),
                              (line("LAPTOP-14", 10, 1200), line("TONER-K", 20, 90))))
    return e


@pytest.fixture
def svc(erp, policy):
    return Service(erp, policy, today=lambda: TODAY)


def as_user(erp: MemoryErp, policy, user: str) -> Service:
    return Service(erp.as_user(user), policy, today=lambda: TODAY)


def po_args(supplier="SUP-HERC", cc="Fleet", qty=1, rate=1000, item="SALT-TON", **kw):
    args = {"supplier": supplier, "cost_centre": cc,
            "lines": [{"item_code": item, "qty": qty, "rate": rate}]}
    args.update(kw)
    return args


def run(svc: Service, action: str, args: dict):
    """Preview, then write with the code the preview issued (the happy path)."""
    p = svc.preview(action, args)
    return p, svc.execute(action, args, p.code)
