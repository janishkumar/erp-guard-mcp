"""In-process ERP for unit tests and the fast eval run. Same protocol as the real ones."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal

from erp_guard.checks import BANK_KEY
from erp_guard.connectors.base import ErpError, check_editable
from erp_guard.models import AuditRecord, CostCentre, Line, PurchaseOrder, Status, Supplier
from erp_guard.packs.p2p import APPROVE, REJECT, REVISE, SUBMIT_FOR_APPROVAL, WITHDRAW
from erp_guard.packs.p2p.models import (
    EDITABLE,
    OPEN,
    POSTED,
    DocState,
    Invoice,
    ItemInfo,
    Payment,
    Quote,
    Receipt,
    Request,
)

# Document types with an approval workflow, and those submitted directly; each by the
# attribute that stores its documents.
_WORKFLOW_TYPES = {"Material Request": "requests", "Purchase Order": "orders",
                   "Purchase Invoice": "invoices", "Payment Entry": "payments",
                   "ERP Guard Change Request": "change_requests"}
_SUBMIT_TYPES = {"Purchase Receipt": "receipts", "Request for Quotation": "rfqs",
                 "Supplier Quotation": "quotes"}
# Workflow action -> (states it applies in, the state it leads to), as the seeded
# workflows. Withdraw is the purchase order's only.
_TRANSITIONS = {
    SUBMIT_FOR_APPROVAL: ({DocState.DRAFT}, DocState.PENDING),
    APPROVE: ({DocState.PENDING}, DocState.APPROVED),
    REJECT: ({DocState.PENDING}, DocState.REJECTED),
    REVISE: ({DocState.REJECTED}, DocState.DRAFT),
    WITHDRAW: ({DocState.PENDING}, DocState.DRAFT),
}


class MemoryErp:
    name = "memory"

    def __init__(self, user: str = "requester@example.com", today: date | None = None,
                 bank_account: str | None = None):
        self.user = user
        self.today = today or date.today()
        # The one account payments may be made from (ERPNext's client enforces the same).
        if bank_account is None:
            from erp_guard.policy import load_policy
            bank_account = load_policy().payment.bank_account
        self.bank_account = bank_account
        self.suppliers: dict[str, Supplier] = {}
        self.orders: dict[str, PurchaseOrder] = {}
        self.cost_centres: dict[str, CostCentre] = {}
        self.items: set[str] = set()
        self.audit: list[AuditRecord] = []
        # Purchase to pay. Items without an ItemInfo are stock items in "Nos".
        self.item_infos: dict[str, ItemInfo] = {}
        self.requests: dict[str, Request] = {}
        self.rfqs: dict[str, dict] = {}
        self.quotes: dict[str, Quote] = {}
        self.receipts: dict[str, Receipt] = {}
        self.invoices: dict[str, Invoice] = {}
        self.payments: dict[str, Payment] = {}
        self.change_requests: dict[str, dict] = {}
        # Fields written that the domain models don't carry (reasons, comments, ...).
        self.extra: dict[tuple[str, str], dict] = {}
        self._seq: dict[str, int] = {"SUP": 0, "PO": 0, "AUD": 0}

    def as_user(self, user: str) -> MemoryErp:
        """A view of the same ERP state, acting as another user (shared storage)."""
        view = object.__new__(MemoryErp)
        view.__dict__ = self.__dict__.copy()
        view.user = user
        return view

    def _next(self, kind: str) -> str:
        self._seq[kind] = self._seq.get(kind, 0) + 1
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
        s = Supplier(self._next("SUP"), name, tax_id, notes=notes, onboarded_by=onboarded_by,
                     onboarded_on=self.today)
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

    # purchase to pay: reads ------------------------------------------------------------
    def item_info(self, codes):
        return {c: self.item_infos.get(c, ItemInfo(c, True, "Nos"))
                for c in codes if c in self.items}

    def get_request(self, id):
        return self.requests.get(id)

    def list_requests(self, *, since=None):
        return [r for r in self.requests.values() if since is None or r.created >= since]

    def list_quotes(self, *, item_codes, since):
        codes = set(item_codes)
        return [q for q in self.quotes.values() if q.quoted_on >= since
                and any(ln.item_code in codes for ln in q.lines)]

    def list_receipts(self, *, po=None, supplier_id=None):
        return [r for r in self.receipts.values()
                if (po is None or any(ln.po == po for ln in r.lines))
                and (supplier_id is None or r.supplier_id == supplier_id)]

    def get_receipt(self, id):
        return self.receipts.get(id)

    def list_invoices(self, *, supplier_id=None, po=None, since=None):
        return [i for i in self.invoices.values()
                if (supplier_id is None or i.supplier_id == supplier_id)
                and (po is None or any(ln.po == po for ln in i.lines))
                and (since is None or i.bill_date >= since)]

    def get_invoice(self, id):
        return self.invoices.get(id)

    def list_payments(self, *, supplier_id=None, invoice=None, since=None):
        return [p for p in self.payments.values()
                if (supplier_id is None or p.supplier_id == supplier_id)
                and (invoice is None or any(a.invoice == invoice for a in p.allocations))
                and (since is None or p.posted >= since)]

    def get_payment(self, id):
        return self.payments.get(id)

    def outstanding(self, invoice_id):
        """A posted invoice's total less posted payments and debit notes against it."""
        inv = self._get("Purchase Invoice", invoice_id)
        if inv.state not in POSTED or inv.is_return:
            return Decimal(0)
        paid = sum((a.amount for p in self.payments.values() if p.state in POSTED
                    for a in p.allocations if a.invoice == invoice_id), Decimal(0))
        credited = sum((n.total for n in self.invoices.values() if n.is_return
                        and n.return_against == invoice_id and n.state in POSTED), Decimal(0))
        return inv.total - paid - credited

    # purchase to pay: writes -----------------------------------------------------------
    def _store_of(self, doctype: str) -> dict:
        attr = _WORKFLOW_TYPES.get(doctype) or _SUBMIT_TYPES.get(doctype)
        if attr is None:
            raise ErpError(f"{doctype} is not a purchase-to-pay document")
        return getattr(self, attr)

    def _get(self, doctype: str, id: str):
        doc = self._store_of(doctype).get(id)
        if doc is None:
            raise ErpError(f"No {doctype} {id}")
        return doc

    def _state(self, doctype: str, id: str) -> DocState:
        doc = self._get(doctype, id)
        if isinstance(doc, dict):
            return doc["state"]
        if isinstance(doc, PurchaseOrder):
            return DocState(doc.status.value)
        return doc.state

    def _set(self, doctype: str, id: str, state: DocState | None = None, **changes) -> None:
        store, doc = self._store_of(doctype), self._get(doctype, id)
        if isinstance(doc, dict):
            doc.update(changes, **({"state": state} if state else {}))
            return
        if state is not None:
            changes["status" if isinstance(doc, PurchaseOrder) else "state"] = (
                Status(state.value) if isinstance(doc, PurchaseOrder) else state)
        store[id] = dataclasses.replace(doc, **changes)

    def _keep(self, doctype: str, id: str, **fields) -> None:
        self.extra.setdefault((doctype, id), {}).update(fields)

    def create_request(self, cost_centre, lines, notes):
        r = Request(self._next("MR"), self.user, cost_centre, DocState.DRAFT, tuple(lines),
                    self.today)
        self.requests[r.id] = r
        self._keep("Material Request", r.id, notes=notes)
        return r

    def create_rfq(self, request_id, suppliers):
        self._get("Material Request", request_id)
        rid = self._next("RFQ")
        self.rfqs[rid] = {"id": rid, "request": request_id, "suppliers": list(suppliers),
                          "state": DocState.DRAFT}
        return rid

    def create_quote(self, supplier_id, lines, rfq, quoted_on):
        if rfq is not None:
            req = self._get("Material Request", self._get("Request for Quotation", rfq)["request"])
            asked = {ln.item_code for ln in req.lines}
            missing = [ln.item_code for ln in lines if ln.item_code not in asked]
            if missing:
                raise ErpError(f"Not on {rfq}: " + "; ".join(missing))
        q = Quote(self._next("SQ"), supplier_id, rfq, DocState.DRAFT, tuple(lines), quoted_on)
        self.quotes[q.id] = q
        return q

    def _order_items(self, po: str) -> set[str]:
        order = self.orders.get(po)
        if order is None:
            raise ErpError(f"No purchase order {po}")
        return {ln.item_code for ln in order.lines}

    def create_receipt(self, po, lines, *, is_return=False, return_against=None):
        """Lines must name items of the order (and, for a return, of the receipt)."""
        items = self._order_items(po)
        if is_return:
            if return_against is None:
                raise ErpError("A return names the receipt it returns goods from")
            items &= {ln.item_code for ln in self._get("Purchase Receipt", return_against).lines}
        for ln in lines:
            if ln.po != po or ln.item_code not in items:
                raise ErpError(f"{ln.item_code} on {ln.po} is not on {return_against or po}")
        r = Receipt(self._next("PR"), self.orders[po].supplier_id, self.user,
                    DocState.SUBMITTED, self.today, tuple(lines), is_return,
                    return_against if is_return else None)
        self.receipts[r.id] = r
        return r

    def _check_invoice_lines(self, lines) -> None:
        for ln in lines:
            if ln.item_code not in self._order_items(ln.po):
                raise ErpError(f"{ln.item_code} is not on {ln.po}")
            if ln.receipt is not None:
                self._get("Purchase Receipt", ln.receipt)

    def create_invoice(self, supplier_id, bill_no, bill_date, lines, remarks):
        if not lines:
            raise ErpError("An invoice needs at least one line")
        self._check_invoice_lines(lines)
        inv = Invoice(self._next("PI"), supplier_id, bill_no, bill_date, self.user,
                      DocState.DRAFT, tuple(lines), remarks=remarks)
        self.invoices[inv.id] = inv
        return inv

    def set_invoice_hold(self, id, hold, comment):
        self._set("Purchase Invoice", id, on_hold=hold)
        self._keep("Purchase Invoice", id, hold_comment=comment)
        return self.invoices[id]

    def create_debit_note(self, invoice_id, lines, remarks):
        orig = self._get("Purchase Invoice", invoice_id)
        on_orig = {(ln.po, ln.item_code) for ln in orig.lines}
        for ln in lines:
            if (ln.po, ln.item_code) not in on_orig:
                raise ErpError(f"{ln.item_code} on {ln.po} is not on {invoice_id}")
        note = Invoice(self._next("PI"), orig.supplier_id, orig.bill_no, orig.bill_date,
                       self.user, DocState.DRAFT, tuple(lines), is_return=True,
                       return_against=invoice_id, remarks=remarks)
        self.invoices[note.id] = note
        return note

    def create_payment(self, supplier_id, allocations, paid_from):
        if not allocations:
            raise ErpError("A payment pays at least one invoice")
        if paid_from != self.bank_account:
            raise ErpError("bank details are never written: ['paid_from']")
        for a in allocations:
            self._get("Purchase Invoice", a.invoice)
        p = Payment(self._next("PE"), supplier_id, self.user, DocState.DRAFT, self.today,
                    paid_from, tuple(allocations))
        self.payments[p.id] = p
        return p

    def workflow(self, doctype, id, action, comment=""):
        if doctype not in _WORKFLOW_TYPES:
            raise ErpError(f"{doctype} has no approval workflow")
        state = self._state(doctype, id)
        valid, nxt = _TRANSITIONS.get(action, (set(), None))
        if action == WITHDRAW and doctype != "Purchase Order":
            valid = set()
        if state not in valid:
            raise ErpError(f"{action} is not available on {doctype} {id} ({state.value})")
        approver = {"approver": self.user} if action == APPROVE and doctype in (
            "Purchase Order", "Purchase Invoice", "Payment Entry") else {}
        self._set(doctype, id, nxt, **approver)
        if comment:
            self._keep(doctype, id, comment=comment)

    def submit_doc(self, doctype, id):
        if doctype in _WORKFLOW_TYPES:
            raise ErpError(f"{doctype} has an approval workflow; it is submitted by "
                           "approving it")
        if self._state(doctype, id) != DocState.DRAFT:
            raise ErpError(f"{doctype} {id} is not a draft")
        self._set(doctype, id, DocState.SUBMITTED)

    def cancel_doc(self, doctype, id, reason):
        if doctype == "Purchase Order":
            self.cancel_order(id, reason)
            return
        state = self._state(doctype, id)
        if state == DocState.CANCELLED:
            raise ErpError(f"{doctype} {id} is already cancelled")
        if state not in POSTED:
            raise ErpError(f"{doctype} {id} isn't submitted; only submitted documents are "
                           "cancelled")
        self._set(doctype, id, DocState.CANCELLED)
        self._keep(doctype, id, reason=reason)

    def update_draft(self, doctype, id, fields):
        """Only the fields models.EDITABLE allows; rows merge in order into the document's
        lines. A payment's remarks aren't on the model and are kept aside."""
        check_editable(doctype, fields)
        if self._state(doctype, id) not in OPEN:
            raise ErpError(f"{doctype} {id} isn't a draft")
        doc = self._get(doctype, id)
        fields = dict(fields)
        changes: dict = {}
        for table in EDITABLE[doctype][1]:
            if table not in fields:
                continue
            rows = fields.pop(table)
            lines = doc.allocations if doctype == "Payment Entry" else doc.lines
            if len(rows) != len(lines):
                raise ErpError(f"{table}: {len(rows)} rows given, the draft has {len(lines)}")
            new = []
            for ln, row in zip(lines, rows, strict=True):
                upd = {("amount" if k == "allocated_amount" else k): Decimal(str(v))
                       for k, v in row.items()}
                new.append(dataclasses.replace(ln, **upd))
            changes["allocations" if doctype == "Payment Entry" else "lines"] = tuple(new)
        if doctype == "Purchase Invoice":
            for k in ("bill_no", "remarks"):
                if k in fields:
                    changes[k] = str(fields.pop(k))
            if "bill_date" in fields:
                d = fields.pop("bill_date")
                changes["bill_date"] = d if isinstance(d, date) else date.fromisoformat(d)
        self._set(doctype, id, **changes)
        if fields:
            self._keep(doctype, id, **fields)

    def close_order(self, po, reason):
        self._order_items(po)
        self._keep("Purchase Order", po, reason=reason)
        return self._replace(po, closed=True)

    def create_change_request(self, target_doctype, target_name, fields):
        bank = sorted(k for k in fields if BANK_KEY.search(k))
        if bank:
            raise ErpError(f"bank details are never written: {bank}")
        if target_doctype == "Item" and target_name not in self.items or \
                target_doctype == "Supplier" and target_name not in self.suppliers:
            raise ErpError(f"No {target_doctype} {target_name}")
        cid = self._next("ECR")
        self.change_requests[cid] = {
            "id": cid, "target_doctype": target_doctype, "target_name": target_name,
            "fields": dict(fields), "requested_by": self.user, "state": DocState.PENDING,
            "status": "Pending", "decided_by": None}
        return cid

    def apply_change_request(self, id, approve):
        """As ERPNext: the change is applied first, and only then is the request approved;
        if applying fails, the request stays pending."""
        rec = self._get("ERP Guard Change Request", id)
        if rec["state"] != DocState.PENDING:
            raise ErpError(f"Change request {id} isn't pending ({rec['state'].value})")
        if approve:
            self._apply_master(rec["target_doctype"], rec["target_name"], rec["fields"])
        rec.update(state=DocState.APPROVED if approve else DocState.REJECTED,
                   status="Applied" if approve else "Rejected", decided_by=self.user)

    def _apply_master(self, doctype: str, name: str, fields: dict) -> None:
        fields = dict(fields)
        if doctype == "Supplier" and name not in self.suppliers or \
                doctype == "Item" and name not in self.items:
            raise ErpError(f"No {doctype} {name}")
        if doctype == "Supplier":
            s, ch = self.suppliers[name], {}
            for src, dst, conv in (("supplier_name", "name", str), ("tax_id", "tax_id", str),
                                   ("disabled", "disabled", bool),
                                   ("on_hold", "on_hold", bool)):
                if src in fields:
                    ch[dst] = conv(fields.pop(src))
            self.suppliers[name] = dataclasses.replace(s, **ch)
        elif doctype == "Item":
            info = self.item_info([name])[name]
            if "is_stock_item" in fields:
                info = dataclasses.replace(info, is_stock=bool(fields.pop("is_stock_item")))
            if "stock_uom" in fields:
                info = dataclasses.replace(info, stock_uom=str(fields.pop("stock_uom")))
            self.item_infos[name] = info
        if fields:
            self._keep(doctype, name, **fields)

    # seeding (not part of the protocol) ------------------------------------------------
    def add_supplier(self, s: Supplier) -> None:
        self.suppliers[s.id] = s

    def add_order(self, o: PurchaseOrder) -> None:
        self.orders[o.id] = o

    def add_cost_centre(self, cc: CostCentre) -> None:
        self.cost_centres[cc.id] = cc

    def add_items(self, codes) -> None:
        self.items.update(codes)

    def add_item_info(self, info: ItemInfo) -> None:
        self.items.add(info.code)
        self.item_infos[info.code] = info


def line(item_code: str, qty, rate, description: str = "") -> Line:
    return Line(item_code, Decimal(str(qty)), Decimal(str(rate)), description)
