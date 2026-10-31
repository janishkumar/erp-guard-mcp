"""The ERP an eval scenario runs against, and the scripted agent that drives the tools.

The agent calls the real MCP tool functions in po_guard.server and reads confirmation
codes out of their markdown replies, the way a model would. Ground truth is ERP state
read back afterwards, never the tool's reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from po_guard import demo, server
from po_guard.connectors.memory import MemoryErp, line
from po_guard.models import CostCentre, PurchaseOrder, Status, Supplier
from po_guard.policy import load_policy
from po_guard.service import Service

REQ, REQ2 = "requester@example.com", "requester2@example.com"
MGR, DIR = "manager@example.com", "director@example.com"
TAGS = {REQ: "REQUESTER", REQ2: "REQUESTER2", MGR: "MANAGER", DIR: "DIRECTOR"}
FAKE_CODE = "ABCD2345"
_CODE = re.compile(r"Confirmation code: `([A-Z0-9]+)`")


class NaiveService(Service):
    """The baseline: the same tools and ERP writes, with no checks and no codes.

    Roughly what an assistant gets from a thin MCP wrapper over the ERP's REST API.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.codes.redeem = lambda *a, **k: None  # any code, or none, is accepted

    def evaluate(self, action, canon):
        return f"{action} (no checks)", []


@dataclass
class Agent:
    """A confused or injected assistant that keeps trying when it's told no."""

    svc: Service
    last_code: str | None = None
    calls: int = 0

    def preview(self, action: str, args: dict) -> str | None:
        server.set_service(self.svc)
        text = server.preview(action, args)
        self.calls += 1
        m = _CODE.search(text)
        if m:
            self.last_code = m.group(1)
        return m.group(1) if m else ("NAIVE" if isinstance(self.svc, NaiveService) else None)

    def write(self, action: str, args: dict, code: str | None) -> str:
        server.set_service(self.svc)
        self.calls += 1
        try:
            return getattr(server, action)(**args, confirmation_code=code)
        except TypeError as exc:  # an argument the tool doesn't have (e.g. bank_account)
            return f"rejected by the tool schema: {exc}"

    def attempt(self, action: str, args: dict) -> None:
        """Preview and write; if there's no code, try anyway three ways."""
        code = self.preview(action, args)
        if code:
            self.write(action, args, code)
            return
        stale = self.last_code
        for c in (None, FAKE_CODE, stale):
            self.write(action, args, c)


@dataclass
class World:
    kind: str  # memory | erpnext
    guarded: bool
    erp: object = None  # memory: the shared MemoryErp
    created: list[str] = field(default_factory=list)
    baseline: set[str] = field(default_factory=set)
    _supplier_i: int = 0

    # --- construction ----------------------------------------------------------------
    @classmethod
    def memory(cls, guarded: bool) -> World:
        w = cls("memory", guarded)
        today = date.today()
        e = MemoryErp(user=REQ, today=today)
        e.add_items(demo.ITEMS)
        for name, budget in demo.COST_CENTRES.items():
            e.add_cost_centre(CostCentre(demo.cc(name), Decimal(budget)))
        for r in demo.seed_suppliers("master"):
            e.add_supplier(Supplier(r["name"], r["name"], r["synthetic_tax_id"]))
        # History: one approved order per item at its reference price (synthetic).
        for i, (code, (_, price)) in enumerate(demo.ITEMS.items()):
            if code == "PROJECT-SVC":
                continue
            e.add_order(PurchaseOrder(f"HIST-{i:03d}", "TRANE U.S. INC", demo.cc("Facilities"),
                                      REQ2, Status.APPROVED, today - timedelta(days=120),
                                      (line(code, 1, price),), approver=DIR))
        w.erp = e
        return w

    @classmethod
    def erpnext(cls, guarded: bool) -> World:
        return cls("erpnext", guarded)

    # --- access ----------------------------------------------------------------------
    def connector(self, user: str):
        if self.kind == "memory":
            return self.erp.as_user(user)
        from po_guard.connectors.erpnext import ErpNext
        return ErpNext.from_env(TAGS[user])

    def agent(self, user: str) -> Agent:
        cls = Service if self.guarded else NaiveService
        return Agent(cls(self.connector(user), load_policy()))

    @property
    def read(self):
        return self.connector(DIR)

    def fresh_supplier(self) -> str:
        """A seeded supplier no earlier scenario in this run has ordered from."""
        masters = [r["name"] for r in demo.seed_suppliers("master")]
        self._supplier_i += 1
        return masters[(self._supplier_i * 7) % len(masters)]

    # --- state queries ---------------------------------------------------------------
    def begin(self) -> None:
        """Remember which orders already exist, so a scenario only judges its own."""
        self.baseline = {o.id for o in self.read.list_orders()}

    def orders(self, supplier: str) -> list[PurchaseOrder]:
        out = [o for o in self.read.list_orders(supplier_id=supplier)
               if o.id not in self.baseline]
        self.created += [o.id for o in out if o.id not in self.created]
        return out

    def live_orders(self, supplier: str) -> list[PurchaseOrder]:
        return [o for o in self.orders(supplier)
                if o.status in (Status.DRAFT, Status.PENDING, Status.APPROVED)]

    def supplier_named(self, name: str) -> list[Supplier]:
        return [s for s in self.read.list_suppliers() if s.name == name]

    def cleanup(self) -> None:
        """Cancel what the scenario created (cancel, never delete), so the next one starts
        from the same budgets and an empty split window."""
        d = self.connector(DIR)
        for po in self.created:
            o = d.get_order(po)
            if o is not None and o.status != Status.CANCELLED:
                d.cancel_order(po, "eval cleanup")
        self.created.clear()
