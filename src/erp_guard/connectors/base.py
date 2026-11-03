"""The ERP connector protocols.

`ErpConnector` is v1's, deliberately small: suppliers and purchase orders, and no method
for payments, bank details or deleting anything. `P2PConnector` adds the rest of purchase
to pay. Its only payment methods are drafting a payment against invoices and reading
payments (approval goes through the ERP's workflow); there is still no method for bank
details or deleting anything. tests/test_hard_limits.py keeps it that way.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from erp_guard.models import AuditRecord, CostCentre, Line, PurchaseOrder, Status, Supplier
from erp_guard.packs.p2p.models import (
    Allocation,
    Invoice,
    InvoiceLine,
    ItemInfo,
    Payment,
    Quote,
    Receipt,
    ReceiptLine,
    Request,
    RequestLine,
    not_editable,
)


class ErpError(Exception):
    """The ERP refused or failed a request."""


def check_editable(doctype: str, fields: dict) -> None:
    """Refuse a draft update outside the pack's editable fields (models.EDITABLE), with the
    same message from every connector."""
    bad = not_editable(doctype, fields)
    if bad:
        raise ErpError(f"{doctype} drafts can't have these changed: {', '.join(bad)}")


class ErpConnector(Protocol):
    name: str

    # reads
    def whoami(self) -> str: ...
    def list_suppliers(self) -> list[Supplier]: ...
    def get_supplier(self, supplier_id: str) -> Supplier | None: ...
    def get_order(self, order_id: str) -> PurchaseOrder | None: ...
    def list_orders(self, *, supplier_id: str | None = None, cost_centre: str | None = None,
                    since: date | None = None) -> list[PurchaseOrder]: ...
    def get_cost_centre(self, cost_centre: str) -> CostCentre | None: ...
    def item_codes(self) -> set[str]: ...
    def last_purchase_prices(self, item_codes: list[str]) -> dict[str, Decimal]: ...
    def read_audit(self, target: str | None = None, limit: int = 50) -> list[AuditRecord]: ...

    # writes: the complete list
    def create_supplier(self, name: str, tax_id: str | None, notes: str,
                        onboarded_by: str) -> Supplier: ...
    def create_order(self, supplier_id: str, cost_centre: str, lines: list[Line],
                     notes: str, required_by: date) -> PurchaseOrder: ...
    def set_status(self, order_id: str, status: Status, reason: str) -> PurchaseOrder: ...
    def approve_order(self, order_id: str) -> PurchaseOrder: ...
    def cancel_order(self, order_id: str, reason: str) -> PurchaseOrder: ...
    def amend_order(self, order_id: str, lines: list[Line], notes: str) -> PurchaseOrder: ...
    def write_audit(self, record: AuditRecord) -> str: ...


WRITE_METHODS = frozenset({
    "create_supplier", "create_order", "set_status", "approve_order", "cancel_order",
    "amend_order", "write_audit",
})


class P2PConnector(ErpConnector, Protocol):
    """Purchase to pay beyond v1's orders: requests, quotes, receipts, invoices, payments,
    workflow transitions, and master-data change requests.

    Documents are named by the ERP's document type (e.g. "Purchase Invoice") where a
    method works on several. Builders copy the source document (order, receipt, invoice)
    the ERP's own way, keep the lines named per (PO, item code) with the given quantities
    and rates, and drop the rest.
    """

    # reads
    def item_info(self, codes: list[str]) -> dict[str, ItemInfo]: ...
    def get_request(self, id: str) -> Request | None: ...
    def list_requests(self, *, since: date | None = None) -> list[Request]: ...
    def list_quotes(self, *, item_codes: list[str], since: date) -> list[Quote]: ...
    def list_receipts(self, *, po: str | None = None,
                      supplier_id: str | None = None) -> list[Receipt]: ...
    def get_receipt(self, id: str) -> Receipt | None: ...
    def list_invoices(self, *, supplier_id: str | None = None, po: str | None = None,
                      since: date | None = None) -> list[Invoice]: ...
    def get_invoice(self, id: str) -> Invoice | None: ...
    def list_payments(self, *, supplier_id: str | None = None, invoice: str | None = None,
                      since: date | None = None) -> list[Payment]: ...
    def get_payment(self, id: str) -> Payment | None: ...
    def outstanding(self, invoice_id: str) -> Decimal:
        """What is still owed on a posted invoice; nothing on a draft or cancelled one."""
        ...

    # writes
    def create_request(self, cost_centre: str, lines: list[RequestLine],
                       notes: str) -> Request: ...
    def create_rfq(self, request_id: str, suppliers: list[str]) -> str: ...
    def create_quote(self, supplier_id: str, lines: list[Line], rfq: str | None,
                     quoted_on: date) -> Quote: ...
    def create_receipt(self, po: str, lines: list[ReceiptLine], *, is_return: bool = False,
                       return_against: str | None = None) -> Receipt:
        """Records the receipt (or return) and submits it: it records a fact."""
        ...
    def create_invoice(self, supplier_id: str, bill_no: str, bill_date: date,
                       lines: list[InvoiceLine], remarks: str) -> Invoice:
        """A draft; approval goes through the workflow."""
        ...
    def set_invoice_hold(self, id: str, hold: bool, comment: str) -> Invoice: ...
    def create_debit_note(self, invoice_id: str, lines: list[InvoiceLine],
                          remarks: str) -> Invoice: ...
    def create_payment(self, supplier_id: str, allocations: list[Allocation],
                       paid_from: str) -> Payment:
        """A draft paying exactly the allocations; approval goes through the workflow."""
        ...
    def workflow(self, doctype: str, id: str, action: str, comment: str = "") -> None: ...
    def submit_doc(self, doctype: str, id: str) -> None:
        """Submit a document type that has no approval workflow."""
        ...
    def cancel_doc(self, doctype: str, id: str, reason: str) -> None: ...
    def update_draft(self, doctype: str, id: str, fields: dict) -> None:
        """Change a draft. Header fields by the ERP's field name; a child table as a list
        of partial rows, merged in order into the existing rows (same number of rows)."""
        ...
    def close_order(self, po: str, reason: str) -> PurchaseOrder: ...
    def create_change_request(self, target_doctype: str, target_name: str,
                              fields: dict) -> str:
        """Record a change to master data for a second person, pending approval."""
        ...
    def apply_change_request(self, id: str, approve: bool) -> None:
        """Approve (and apply) or reject a pending change request."""
        ...


P2P_WRITE_METHODS = frozenset({
    "create_request", "create_rfq", "create_quote", "create_receipt", "create_invoice",
    "set_invoice_hold", "create_debit_note", "create_payment", "workflow", "submit_doc",
    "cancel_doc", "update_draft", "close_order", "create_change_request",
    "apply_change_request",
})
# The only methods anywhere in the protocols that name payments.
PAYMENT_METHODS = frozenset({"create_payment", "get_payment", "list_payments"})
