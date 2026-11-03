"""ERPNext connector: talks to ERPNext over its REST API (token auth, one API key per ERP user).

The HTTP layer is a second wall behind the service's checks: it never sends DELETE, only
calls allowlisted methods, writes only document types the `writable` predicate allows
(the scope rules), and never writes bank details. Even a bug in this module can't delete,
administer the system or redirect a payment.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import quote

import httpx

from erp_guard.checks import BANK_KEY as _BANK_KEY
from erp_guard.connectors.base import ErpError, check_editable
from erp_guard.models import AuditRecord, CostCentre, Line, PurchaseOrder, Status, Supplier
from erp_guard.packs.p2p import APPROVE, REJECT, REVISE, SUBMIT_FOR_APPROVAL, WITHDRAW
from erp_guard.packs.p2p.models import (
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
    RequestLine,
)
from erp_guard.policy import Policy, load_policy

AUDIT_DOCTYPE = "ERP Guard Audit"
CHANGE_REQUEST = "ERP Guard Change Request"

# v1's document types, read through /api/resource.
RESOURCES = frozenset({"Supplier", "Purchase Order", "Item", "Cost Center", AUDIT_DOCTYPE})
# What a client writes before the service gives it the scope rules: v1's writes only (the
# audit doctype is create-only whatever this says).
V1_WRITABLE = frozenset({"Supplier", "Purchase Order"})
# Keys that make a document a child row; rows are only written inside their parent.
_ROW_KEYS = ("parent", "parenttype", "parentfield")

_BUY = "erpnext.buying.doctype"
_STOCK = "erpnext.stock.doctype"
_ACC = "erpnext.accounts.doctype"
# (source doctype, target doctype) -> ERPNext's mapper. Mappers return an unsaved draft.
MAPPERS: dict[tuple[str, str], str] = {
    ("Material Request", "Purchase Order"):
        f"{_STOCK}.material_request.material_request.make_purchase_order",
    ("Material Request", "Request for Quotation"):
        f"{_STOCK}.material_request.material_request.make_request_for_quotation",
    ("Request for Quotation", "Supplier Quotation"):
        f"{_BUY}.request_for_quotation.request_for_quotation.make_supplier_quotation_from_rfq",
    ("Supplier Quotation", "Purchase Order"):
        f"{_BUY}.supplier_quotation.supplier_quotation.make_purchase_order",
    ("Purchase Order", "Purchase Receipt"):
        f"{_BUY}.purchase_order.purchase_order.make_purchase_receipt",
    ("Purchase Order", "Purchase Invoice"):
        f"{_BUY}.purchase_order.purchase_order.make_purchase_invoice",
    ("Purchase Receipt", "Purchase Invoice"):
        f"{_STOCK}.purchase_receipt.purchase_receipt.make_purchase_invoice",
    ("Purchase Receipt", "Purchase Receipt"):
        f"{_STOCK}.purchase_receipt.purchase_receipt.make_purchase_return",
    ("Purchase Invoice", "Purchase Invoice"):
        f"{_ACC}.purchase_invoice.purchase_invoice.make_debit_note",
    ("Purchase Invoice", "Payment Entry"):
        f"{_ACC}.payment_entry.payment_entry.get_payment_entry",
}
PO_UPDATE_STATUS = f"{_BUY}.purchase_order.purchase_order.update_status"
RUN_DOC_METHOD = "frappe.handler.run_doc_method"
# Controller methods reachable through run_doc_method: (doctype, method). In v16 hold and
# release are methods on the Purchase Invoice controller, not module functions.
DOC_METHODS = frozenset({("Purchase Invoice", "block_invoice"),
                         ("Purchase Invoice", "unblock_invoice")})

GET_ONLY_METHODS = frozenset({"frappe.desk.form.load.getdoctype"})
READ_METHODS = frozenset({"frappe.auth.get_logged_user", "frappe.client.get_list",
                          "frappe.client.get", "frappe.model.workflow.get_transitions",
                          *MAPPERS.values()})
# Write methods (POST only), and where each names the document type it writes.
WRITE_METHODS = {
    "frappe.client.insert": "doc", "frappe.client.save": "doc", "frappe.client.submit": "doc",
    "frappe.model.workflow.apply_workflow": "doc", "frappe.client.cancel": "doctype",
    RUN_DOC_METHOD: "dt", PO_UPDATE_STATUS: "Purchase Order",
}
METHODS = GET_ONLY_METHODS | READ_METHODS | frozenset(WRITE_METHODS)
HTTP_VERBS = frozenset({"GET", "POST", "PUT"})
# Set by our code on a Payment Entry, and then only to the policy's bank account.
_PAYMENT_BANK_KEYS = frozenset({"paid_from", "bank_account"})

_SUPPLIER_FIELDS = ["name", "supplier_name", "tax_id", "disabled", "on_hold",
                    "erp_guard_onboarded_by", "erp_guard_notes", "creation"]


class HardLimitError(ErpError):
    """A request the connector must never make."""


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


def _fields(obj, doctype: str | None = None):
    """Yield (doctype of the enclosing document, key, value) for every key in a body."""
    if isinstance(obj, dict):
        dt = obj.get("doctype") or doctype  # a falsy doctype never replaces the context
        for k, v in obj.items():
            yield dt, str(k), v
            yield from _fields(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _fields(v)


class FrappeClient:
    def __init__(self, base_url: str, api_key: str, api_secret: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 30.0,
                 writable: Callable[[str], bool] = lambda d: d in V1_WRITABLE,
                 allowed_bank_values: Iterable[str] = ()):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
            headers={"Authorization": f"token {api_key}:{api_secret}",
                     "Accept": "application/json"},
        )
        self.writable = writable
        self.allowed_bank_values = frozenset(allowed_bank_values)

    def _require_writable(self, doctype, creating: bool = False) -> None:
        if doctype == AUDIT_DOCTYPE:
            # The trail is append-only: records are created, never changed.
            if not creating:
                raise HardLimitError(f"{AUDIT_DOCTYPE} records are never changed")
            return
        if not isinstance(doctype, str) or not doctype or not self.writable(doctype):
            raise HardLimitError(f"doctype not writable: {doctype}")

    def _write_targets(self, method: str, body) -> list[tuple[object, bool]]:
        """The document types a write method touches, read from its arguments, each with
        whether the write only creates a new document of that type."""
        where = WRITE_METHODS[method]
        body = body if isinstance(body, dict) else {}
        if where == "doctype":
            return [(body.get("doctype"), False)]
        if where == "dt":
            if (body.get("dt"), body.get("method")) not in DOC_METHODS or not body.get("dn"):
                raise HardLimitError(f"document method not allowed: {body.get('method')}")
            if not isinstance(body.get("args"), dict | None):
                raise HardLimitError("document method args must be an object")
            return [(body["dt"], False)]
        if where == "doc":
            doc = body.get("doc")
            if not isinstance(doc, dict):
                raise HardLimitError(f"{method} needs the document as an object")
            _refuse_child_row(doc)
            return [(doc.get("doctype"), method == "frappe.client.insert")]
        return [(where, False)]

    def _check_bank(self, body, doctype: str | None) -> None:
        bad = set()
        for dt, key, value in _fields(body, doctype):
            if value is None or value == "":
                continue
            if dt == "Payment Entry" and key in _PAYMENT_BANK_KEYS:
                if value not in self.allowed_bank_values:
                    bad.add(key)
            elif _BANK_KEY.search(key):
                bad.add(key)
        if bad:
            raise HardLimitError(f"bank details are never written: {sorted(bad)}")

    def _guard(self, verb: str, path: str, body, params: dict | None = None) -> None:
        if verb not in HTTP_VERBS:
            raise HardLimitError(f"{verb} is never sent")
        if verb != "GET" and params:
            # ERPNext reads the query string as arguments when there's no JSON body, and
            # the checks below only inspect the body.
            raise HardLimitError(f"{verb} carries its arguments in the body only")
        m = re.fullmatch(r"/api/(resource|method)/([^/?]+)(/[^?]*)?", path)
        if not m:
            raise HardLimitError(f"path not allowed: {path}")
        kind, name = m.group(1), httpx.URL("/" + m.group(2)).path[1:]
        name = name.replace("%20", " ")
        doctype = None
        if kind == "resource":
            doctype = name
            # ERPNext writes the path's doctype whatever the body says; so must the checks.
            if isinstance(body, dict) and "doctype" in body and body["doctype"] != name:
                raise HardLimitError(f"body doctype {body['doctype']!r} != path {name}")
            if verb == "GET":
                if name not in RESOURCES and not self.writable(name):
                    raise HardLimitError(f"doctype not allowed: {name}")
            else:
                if isinstance(body, dict):
                    _refuse_child_row({"doctype": name, **body})
                # POST to the collection creates; anything naming a record changes it.
                self._require_writable(name, creating=verb == "POST" and not m.group(3))
        elif name not in METHODS:
            raise HardLimitError(f"method not allowed: {name}")
        elif name in GET_ONLY_METHODS and verb != "GET":
            raise HardLimitError(f"{name} is read with GET only")
        elif name in WRITE_METHODS:
            if verb != "POST":
                raise HardLimitError(f"{name} is sent with POST only")
            for target, creating in self._write_targets(name, body):
                self._require_writable(target, creating)
        if body is not None:
            self._check_bank(body, doctype)

    def request(self, verb: str, path: str, params: dict | None = None, body=None):
        self._guard(verb, path, body, params)
        try:
            r = self.http.request(verb, path, params=params,
                                  json=body if body is not None else None)
        except httpx.HTTPError as exc:
            raise ErpError(f"ERPNext unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise ErpError(f"ERPNext {r.status_code} on {verb} {path}: {_error_text(r)}")
        return r.json()

    def get_doc(self, doctype: str, name: str) -> dict | None:
        try:
            return self.request("GET", _res(doctype, name))["data"]
        except ErpError as exc:
            if " 404 " in str(exc):
                return None
            raise

    def get_list(self, doctype: str, fields: list[str], filters: list | None = None,
                 order_by: str | None = None, limit: int = 0) -> list[dict]:
        params = {"fields": json.dumps(fields), "limit_page_length": str(limit)}
        if filters:
            params["filters"] = json.dumps(filters)
        if order_by:
            params["order_by"] = order_by
        return self.request("GET", _res(doctype), params=params)["data"]


def _refuse_child_row(doc: dict) -> None:
    """Child rows are written through their parent document, never on their own (that would
    skip the parent's checks). Scope classifies child-table types as never writable too."""
    found = [k for k in _ROW_KEYS if doc.get(k)]
    if found:
        raise HardLimitError(
            f"{doc.get('doctype')} rows of {doc.get('parenttype') or 'a parent'} are written "
            f"through their parent document, never on their own ({', '.join(found)} set)")


def _res(doctype: str, name: str | None = None) -> str:
    p = f"/api/resource/{quote(doctype)}"
    return p + (f"/{quote(name, safe='')}" if name else "")


def _error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:300]
    msgs = data.get("_server_messages")
    if msgs:
        try:
            return "; ".join(json.loads(m).get("message", m) for m in json.loads(msgs))[:500]
        except (ValueError, AttributeError):
            pass
    return str(data.get("exception") or data.get("exc_type") or data)[:500]


# Not carried into an amendment: identity, bookkeeping, and state ERPNext sets again.
_NOT_AMENDED = frozenset({"name", "owner", "creation", "modified", "modified_by", "docstatus",
                          "amended_from", "status", "workflow_state"})
_NOT_COPIED_ROW = frozenset({"name", "owner", "creation", "modified", "modified_by",
                             "docstatus", "parent", "parenttype", "parentfield"})


def _copy(doc: dict, drop: frozenset[str]) -> dict:
    """A document without `drop` and private (`_x`) keys; child rows lose their identity."""
    out = {}
    for k, v in doc.items():
        if k in drop or k.startswith("_"):
            continue
        if isinstance(v, list) and v and all(isinstance(r, dict) for r in v):
            v = [_copy(r, _NOT_COPIED_ROW) for r in v]
        out[k] = v
    return out


# Purchase Order workflow ("Purchase Order Approval", seeded by `make seed`): its draft
# states. The action names live with the pack (erp_guard.packs.p2p).
_DRAFT_STATES = {"Draft": Status.DRAFT, "Pending Approval": Status.PENDING,
                 "Rejected": Status.REJECTED}
# The action that takes an order in this state back to Draft.
_BACK_TO_DRAFT = {"Rejected": REVISE, "Pending Approval": WITHDRAW}


def _order_status(d: dict) -> Status:
    """docstatus first: submitted orders were approved (history orders predate the
    workflow), and a plain cancel leaves workflow_state at "Approved" in Frappe v16. A
    cancelled draft keeps its workflow state and carries the `erp_guard_cancelled` flag."""
    if d.get("docstatus") == 2:
        return Status.CANCELLED
    if d.get("docstatus") == 1:
        return Status.APPROVED
    if d.get("erp_guard_cancelled"):
        return Status.CANCELLED
    return _DRAFT_STATES.get(d.get("workflow_state") or "Draft", Status.DRAFT)


def _order_from_doc(d: dict) -> PurchaseOrder:
    status = _order_status(d)
    rows = d.get("items", [])
    lines = tuple(
        Line(i["item_code"], Decimal(str(i["qty"])), Decimal(str(i["rate"])),
             i.get("description") or "")
        for i in rows
    )
    requests = [i["material_request"] for i in rows if i.get("material_request")]
    return PurchaseOrder(
        id=d["name"], supplier_id=d["supplier"],
        cost_centre=d.get("erp_guard_cost_centre") or "",
        requester=d["owner"], status=status,
        order_date=_date(d["transaction_date"]), lines=lines,
        notes=d.get("erp_guard_notes") or "", approver=d.get("erp_guard_approver") or None,
        request=requests[0] if requests else None,
        quotations=tuple(dict.fromkeys(i["supplier_quotation"] for i in rows
                                       if i.get("supplier_quotation"))),
        closed=d.get("status") == "Closed",
    )


# --- purchase to pay: ERPNext documents as the pack's models ---------------------------

# Document types with an approval workflow (seeded by `make seed`); the rest are submitted
# directly. submit_doc asks ERPNext's metadata, not this list.
_WORKFLOW_TYPES = frozenset({"Material Request", "Purchase Order", "Purchase Invoice",
                             "Payment Entry", CHANGE_REQUEST})
_WF_DRAFT_STATES = {"Draft": DocState.DRAFT, "Pending Approval": DocState.PENDING,
                    "Rejected": DocState.REJECTED}


def _date(v) -> date:
    return date.fromisoformat(str(v)[:10])


def _dec(v) -> Decimal:
    return Decimal(str(v or 0))


def _doc_state(d: dict, workflow: bool) -> DocState:
    """docstatus first (a plain cancel leaves workflow_state "Approved" in v16; documents
    submitted before their workflow existed were approved), then the workflow state."""
    if d.get("docstatus") == 2:
        return DocState.CANCELLED
    if d.get("docstatus") == 1:
        return DocState.APPROVED if workflow else DocState.SUBMITTED
    return _WF_DRAFT_STATES.get(d.get("workflow_state") or "Draft", DocState.DRAFT) \
        if workflow else DocState.DRAFT


def _request(d: dict, rows: list[dict]) -> Request:
    centres = [r["cost_center"] for r in rows if r.get("cost_center")]
    return Request(d["name"], d["owner"], centres[0] if centres else "",
                   _doc_state(d, True),
                   tuple(RequestLine(r["item_code"], _dec(r["qty"])) for r in rows),
                   _date(d["transaction_date"]))


def _quote(d: dict, rows: list[dict]) -> Quote:
    rfqs = [r["request_for_quotation"] for r in rows if r.get("request_for_quotation")]
    return Quote(d["name"], d["supplier"], rfqs[0] if rfqs else None, _doc_state(d, False),
                 tuple(Line(r["item_code"], _dec(r["qty"]), _dec(r["rate"])) for r in rows),
                 _date(d["transaction_date"]))


def _receipt(d: dict, rows: list[dict]) -> Receipt:
    # A return's quantities are negative in ERPNext; the model's are what moved.
    return Receipt(d["name"], d["supplier"], d["owner"], _doc_state(d, False),
                   _date(d["posting_date"]),
                   tuple(ReceiptLine(r.get("purchase_order") or "", r["item_code"],
                                     abs(_dec(r["qty"]))) for r in rows),
                   bool(d.get("is_return")), d.get("return_against") or None)


def _invoice(d: dict, rows: list[dict]) -> Invoice:
    return Invoice(
        d["name"], d["supplier"], d.get("bill_no") or "",
        _date(d.get("bill_date") or d["posting_date"]), d["owner"], _doc_state(d, True),
        tuple(InvoiceLine(r.get("purchase_order") or "", r["item_code"], abs(_dec(r["qty"])),
                          _dec(r["rate"]), r.get("purchase_receipt") or None) for r in rows),
        approver=d.get("erp_guard_approver") or None, on_hold=bool(d.get("on_hold")),
        is_return=bool(d.get("is_return")), return_against=d.get("return_against") or None,
        is_paid=bool(d.get("is_paid")), remarks=d.get("remarks") or "")


def _payment(d: dict, rows: list[dict]) -> Payment:
    # Payments reference supplier invoices only, so the allocations (plus anything
    # unallocated) are the whole paid amount. A reference to anything else (an order, for an
    # advance) is refused rather than left out.
    other = [f"{r.get('reference_doctype')} {r.get('reference_name')}" for r in rows
             if r.get("reference_doctype") != "Purchase Invoice"]
    if other:
        raise ErpError(f"Payment Entry {d['name']} references {', '.join(other)}, not only "
                       "supplier invoices")
    return Payment(
        d["name"], d.get("party") or "", d["owner"], _doc_state(d, True),
        _date(d["posting_date"]), d.get("paid_from") or "",
        tuple(Allocation(r["reference_name"], _dec(r["allocated_amount"])) for r in rows),
        approver=d.get("erp_guard_approver") or None,
        unallocated=_dec(d.get("unallocated_amount")))


# doctype -> (child table field, its DocType, header fields, row fields, parser)
_P2P_DOCS: dict[str, tuple[str, str, list[str], list[str], Callable]] = {
    "Material Request": ("items", "Material Request Item",
                         ["name", "owner", "docstatus", "workflow_state", "transaction_date"],
                         ["item_code", "qty", "cost_center"], _request),
    "Supplier Quotation": ("items", "Supplier Quotation Item",
                           ["name", "owner", "docstatus", "supplier", "transaction_date"],
                           ["item_code", "qty", "rate", "request_for_quotation"], _quote),
    "Purchase Receipt": ("items", "Purchase Receipt Item",
                         ["name", "owner", "docstatus", "supplier", "posting_date",
                          "is_return", "return_against"],
                         ["item_code", "qty", "purchase_order"], _receipt),
    "Purchase Invoice": ("items", "Purchase Invoice Item",
                         ["name", "owner", "docstatus", "workflow_state", "supplier",
                          "bill_no", "bill_date", "posting_date", "erp_guard_approver",
                          "on_hold", "is_return", "return_against", "is_paid", "remarks"],
                         ["item_code", "qty", "rate", "purchase_order", "purchase_receipt"],
                         _invoice),
    "Payment Entry": ("references", "Payment Entry Reference",
                      ["name", "owner", "docstatus", "workflow_state", "party",
                       "posting_date", "paid_from", "erp_guard_approver",
                       "unallocated_amount"],
                      ["reference_doctype", "reference_name", "allocated_amount"], _payment),
}


def _keep_rows(rows: list[dict], wanted: dict[tuple, dict], key: Callable[[dict], tuple],
               source: str) -> list[dict]:
    """The rows of a mapped draft named in `wanted`, each updated with its values; rows not
    named are dropped. Only the first row with a key is kept (an order carries an item on
    one line). A key with no row is an error: the document would differ from the request."""
    out, seen = [], set()
    for r in rows:
        k = key(r)
        if k in wanted and k not in seen:
            seen.add(k)
            out.append({**r, **wanted[k]})
    missing = [k for k in wanted if k not in seen]
    if missing:
        raise ErpError(f"Not on {source}: " + "; ".join(" ".join(map(str, k)) for k in missing))
    return out


def _one_per_key(keys: list[tuple]) -> None:
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise ErpError("One line per order and item: " + "; ".join(" ".join(map(str, k))
                                                                  for k in dup))


def _plain(v):
    """Field values as JSON: dates as ISO strings, Decimals as numbers."""
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


class ErpNext:
    name = "erpnext"

    def __init__(self, client: FrappeClient, company: str,
                 supplier_group: str = "Demo Suppliers"):
        self.c, self.company, self.supplier_group = client, company, supplier_group
        self._meta: dict[str, dict] = {}

    @classmethod
    def from_env(cls, user_tag: str | None = None, policy: Policy | None = None) -> ErpNext:
        """Credentials from the environment. `user_tag` (e.g. "MANAGER") picks the
        per-user keys that `make seed` writes, instead of ERPNEXT_API_KEY/SECRET. The one
        bank account payments may use comes from the policy."""
        kname, sname = (("ERPNEXT_API_KEY", "ERPNEXT_API_SECRET") if user_tag is None else
                        (f"ERPNEXT_KEY_{user_tag}", f"ERPNEXT_SECRET_{user_tag}"))
        missing = [k for k in (kname, sname) if not os.environ.get(k)]
        if missing:
            raise SystemExit(f"Set {', '.join(missing)} (make seed writes them to .env).")
        policy = policy or load_policy()
        client = FrappeClient(os.environ.get("ERPNEXT_URL", "http://localhost:8080"),
                              os.environ[kname], os.environ[sname],
                              allowed_bank_values={policy.payment.bank_account})
        return cls(client, os.environ.get("ERPNEXT_COMPANY", "Lakeside Demo Utilities"),
                   os.environ.get("ERPNEXT_SUPPLIER_GROUP", "Demo Suppliers"))

    # reads -----------------------------------------------------------------------------
    def whoami(self) -> str:
        return self.c.request("GET", "/api/method/frappe.auth.get_logged_user")["message"]

    # any document type -----------------------------------------------------------------
    def call(self, method: str, /, **kwargs):
        """An allowlisted method by its dotted path; returns its `message`. (`method` is
        positional-only: run_doc_method takes a `method` argument of its own.)"""
        path = f"/api/method/{method}"
        if method in GET_ONLY_METHODS:
            return self.c.request("GET", path, params=kwargs).get("message")
        return self.c.request("POST", path, body=kwargs).get("message")

    def use_scope(self, rules, pack_owned: Callable[[str], bool]) -> None:
        """Write only the document types the scope rules allow (generic, later-phase and
        pack-owned ones, never child tables), decided from each type's metadata as this user
        reads it. The service calls this with its own rules and packs, so the client's
        limit and the service's routing agree."""
        from erp_guard.generic.scope import writable_predicate
        self.c.writable = writable_predicate(rules, self.get_meta, pack_owned)

    def roles(self) -> list[str]:
        """The ERPNext roles of the user this connector acts as."""
        rows = self.call("frappe.client.get_list", doctype="Has Role", parent="User",
                         fields=["role"], filters=[["parent", "=", self.whoami()]],
                         limit_page_length=0)
        return [r["role"] for r in rows or []]

    def get_meta(self, doctype: str) -> dict:
        """The DocType document (fields, permissions, is_submittable, ...) with its child
        tables' DocTypes under `child_doctypes`, by name. `__workflow_docs` lists the
        type's active Workflow (empty when it has none). Cached for the connector's life."""
        if doctype not in self._meta:
            docs = self.c.request("GET", "/api/method/frappe.desk.form.load.getdoctype",
                                  params={"doctype": doctype})["docs"]
            main = next(d for d in docs if d["name"] == doctype)
            self._meta[doctype] = {**main, "child_doctypes": {
                d["name"]: d for d in docs if d["name"] != doctype}}
        return self._meta[doctype]

    def list_doctypes(self) -> list[dict]:
        """Every DocType that isn't a child table."""
        return self.find("DocType", [["istable", "=", 0]],
                         ["name", "module", "issingle", "istable", "is_submittable"],
                         "name asc", 0)

    def get_doc(self, doctype: str, name: str) -> dict | None:
        try:
            return self.c.request("GET", "/api/method/frappe.client.get",
                                  params={"doctype": doctype, "name": name})["message"]
        except ErpError as exc:
            if " 404 " in str(exc):
                return None
            raise

    def find(self, doctype: str, filters: list | None = None, fields: list[str] | None = None,
             order_by: str | None = None, limit: int = 20) -> list[dict]:
        body = {"doctype": doctype, "fields": fields or ["name"], "filters": filters or [],
                "order_by": order_by, "limit_page_length": limit}
        return self.call("frappe.client.get_list", **body)

    def map_doc(self, source_doctype: str, source_name: str, target_doctype: str) -> dict:
        """ERPNext's own draft of `target_doctype` from a source document (unsaved)."""
        method = MAPPERS.get((source_doctype, target_doctype))
        if method is None:
            raise HardLimitError(f"no mapper from {source_doctype} to {target_doctype}")
        if target_doctype == "Payment Entry":
            return self.call(method, dt=source_doctype, dn=source_name)
        return self.call(method, source_name=source_name)

    def insert(self, doc: dict) -> dict:
        return self.call("frappe.client.insert", doc=doc)

    def save(self, doc: dict) -> dict:
        return self.call("frappe.client.save", doc=doc)

    def _require(self, doctype: str, name: str) -> dict:
        doc = self.get_doc(doctype, name)
        if doc is None:
            raise ErpError(f"No {doctype} {name}")
        return doc

    def submit(self, doctype: str, name: str) -> dict:
        # frappe.client.submit saves the document it's given, so send the current one.
        return self.call("frappe.client.submit", doc=self._require(doctype, name))

    def cancel(self, doctype: str, name: str) -> dict:
        return self.call("frappe.client.cancel", doctype=doctype, name=name)

    def amend(self, doctype: str, name: str, fields: dict) -> dict:
        """A new draft copied from a cancelled document, with `amended_from` set (ERPNext
        names it <name>-1), and `fields` applied."""
        src = self._require(doctype, name)
        if src.get("docstatus") != 2:
            raise ErpError(f"{doctype} {name} isn't cancelled; only cancelled documents "
                           "can be amended")
        doc = {**_copy(src, _NOT_AMENDED), "amended_from": name, **fields}
        return self.insert(doc)

    def apply_workflow(self, doctype: str, name: str, action: str) -> dict:
        # ERPNext loads the document itself; only its identity is sent.
        return self.call("frappe.model.workflow.apply_workflow",
                         doc={"doctype": doctype, "name": name}, action=action)

    def transitions(self, doctype: str, name: str) -> list[str]:
        rows = self.call("frappe.model.workflow.get_transitions",
                         doc={"doctype": doctype, "name": name})
        return [t["action"] for t in rows or []]

    @staticmethod
    def _supplier(d: dict) -> Supplier:
        return Supplier(d["name"], d.get("supplier_name") or d["name"], d.get("tax_id") or None,
                        bool(d.get("disabled")), bool(d.get("on_hold")),
                        d.get("erp_guard_onboarded_by") or None, d.get("erp_guard_notes") or "",
                        # Onboarded through erp-guard: since its creation. Master data that
                        # predates erp-guard has no onboarding date.
                        _date(d["creation"]) if d.get("erp_guard_onboarded_by")
                        and d.get("creation") else None)

    def list_suppliers(self) -> list[Supplier]:
        return [self._supplier(d) for d in self.c.get_list("Supplier", _SUPPLIER_FIELDS)]

    def get_supplier(self, supplier_id: str) -> Supplier | None:
        d = self.c.get_doc("Supplier", supplier_id)
        return self._supplier(d) if d else None

    def get_order(self, order_id: str) -> PurchaseOrder | None:
        d = self.c.get_doc("Purchase Order", order_id)
        return _order_from_doc(d) if d else None

    def list_orders(self, *, supplier_id=None, cost_centre=None, since=None):
        filters = [["company", "=", self.company]]
        if supplier_id:
            filters.append(["supplier", "=", supplier_id])
        if cost_centre:
            filters.append(["erp_guard_cost_centre", "=", cost_centre])
        if since:
            filters.append(["transaction_date", ">=", since.isoformat()])
        heads = self.c.get_list(
            "Purchase Order",
            ["name", "supplier", "erp_guard_cost_centre", "owner", "docstatus",
             "workflow_state", "erp_guard_cancelled", "transaction_date",
             "erp_guard_approver", "erp_guard_notes", "status"],
            filters, order_by="transaction_date desc")
        if not heads:
            return []
        items = self.c.request("POST", "/api/method/frappe.client.get_list", body={
            "doctype": "Purchase Order",
            "fields": ["name", "`tabPurchase Order Item`.item_code as item_code",
                       "`tabPurchase Order Item`.qty as qty",
                       "`tabPurchase Order Item`.rate as rate",
                       "`tabPurchase Order Item`.material_request as material_request",
                       "`tabPurchase Order Item`.supplier_quotation as supplier_quotation",
                       "`tabPurchase Order Item`.idx as idx"],
            "filters": [["name", "in", [h["name"] for h in heads]]],
            "limit_page_length": 0,
        })["message"]
        by_po: dict[str, list[dict]] = {}
        for it in sorted(items, key=lambda r: (r["name"], r["idx"] or 0)):
            if it.get("item_code"):
                by_po.setdefault(it["name"], []).append(it)
        return [_order_from_doc({**h, "items": by_po.get(h["name"], [])}) for h in heads]

    def get_cost_centre(self, cost_centre: str) -> CostCentre | None:
        d = self.c.get_doc("Cost Center", cost_centre)
        if not d:
            return None
        return CostCentre(d["name"], Decimal(str(d.get("erp_guard_annual_budget") or 0)))

    def item_codes(self) -> set[str]:
        return {d["name"] for d in self.c.get_list("Item", ["name"], [["disabled", "=", 0]])}

    def last_purchase_prices(self, item_codes):
        if not item_codes:
            return {}
        rows = self.c.get_list("Item", ["name", "last_purchase_rate"],
                               [["name", "in", list(item_codes)]])
        return {r["name"]: Decimal(str(r["last_purchase_rate"])) for r in rows
                if r.get("last_purchase_rate")}

    def read_audit(self, target=None, limit=50):
        filters = [["target", "=", target]] if target else None
        rows = self.c.get_list(
            AUDIT_DOCTYPE,
            ["name", "action", "actor", "outcome", "target", "payload_hash", "checks",
             "detail", "creation"],
            filters, order_by="creation desc", limit=limit)
        return [AuditRecord(r["action"], r["actor"], r["outcome"], r.get("payload_hash") or "",
                            json.loads(r.get("checks") or "[]"), r.get("target") or None,
                            r.get("detail") or "", str(r["creation"])[:19], r["name"])
                for r in rows]

    # writes ----------------------------------------------------------------------------
    def create_supplier(self, name, tax_id, notes, onboarded_by):
        body = {"supplier_name": name, "supplier_group": self.supplier_group,
                "supplier_type": "Company", "erp_guard_onboarded_by": onboarded_by,
                "erp_guard_notes": notes}
        if tax_id:
            body["tax_id"] = tax_id
        return self._supplier(self.c.request("POST", _res("Supplier"), body=body)["data"])

    def _items(self, lines: list[Line], cost_centre: str, schedule: date) -> list[dict]:
        return [{"item_code": ln.item_code, "qty": float(ln.qty), "rate": float(ln.rate),
                 "description": ln.description or ln.item_code,
                 "schedule_date": schedule.isoformat(), "cost_center": cost_centre}
                for ln in lines]

    def create_order(self, supplier_id, cost_centre, lines, notes, required_by):
        body = {"supplier": supplier_id, "company": self.company, "ignore_pricing_rule": 1,
                "transaction_date": date.today().isoformat(),
                "schedule_date": required_by.isoformat(),
                "erp_guard_cost_centre": cost_centre, "erp_guard_notes": notes,
                "items": self._items(lines, cost_centre, required_by)}
        return _order_from_doc(self.c.request("POST", _res("Purchase Order"), body=body)["data"])

    # Order lifecycle on the "Purchase Order Approval" workflow. ERPNext checks each action
    # against the user's roles; the service's checks have run before any of these.
    def _put(self, order_id: str, body: dict) -> PurchaseOrder:
        d = self.c.request("PUT", _res("Purchase Order", order_id), body=body)["data"]
        return _order_from_doc(d)

    def _current(self, order_id: str) -> dict:
        current = self.c.get_doc("Purchase Order", order_id)
        if current is None:
            raise ErpError(f"No purchase order {order_id}")
        return current

    def _transition(self, order_id: str, action: str) -> PurchaseOrder:
        return _order_from_doc(self.apply_workflow("Purchase Order", order_id, action))

    def set_status(self, order_id, status, reason):
        """Submit for approval (a rejected order is revised back to Draft first), or reject
        with the reason saved on the order before the transition."""
        if status is Status.PENDING:
            if self._current(order_id).get("workflow_state") == "Rejected":
                self._transition(order_id, REVISE)
            return self._transition(order_id, SUBMIT_FOR_APPROVAL)
        if status is Status.REJECTED:
            self._put(order_id, {"erp_guard_reason": reason or ""})
            return self._transition(order_id, REJECT)
        raise ErpError(f"An ERPNext purchase order isn't set to {status.value} directly")

    def approve_order(self, order_id):
        """Approve submits the order. The workflow's Approved state records the acting user
        in `erp_guard_approver` (an update-field expression the seed sets), so the approver
        is ERPNext's own record, whichever way the order was approved."""
        return self._transition(order_id, APPROVE)

    def cancel_order(self, order_id, reason):
        """An approved (submitted) order is cancelled in ERPNext; a draft in any workflow
        state gets the `erp_guard_cancelled` flag and the reason, and stays a draft. Nothing
        is deleted."""
        if self._current(order_id)["docstatus"] == 1:
            self.cancel("Purchase Order", order_id)
            return self.get_order(order_id)
        return self._put(order_id, {"erp_guard_cancelled": 1, "erp_guard_reason": reason})

    def amend_order(self, order_id, lines, notes):
        """New lines on a draft. A rejected order is revised and a pending one withdrawn
        back to Draft first, so an amended order always needs resubmitting (an approval
        previewed before the amendment no longer finds it pending)."""
        current = self._current(order_id)
        back = _BACK_TO_DRAFT.get(current.get("workflow_state"))
        if back:
            self._transition(order_id, back)
        schedule = date.fromisoformat(str(current["schedule_date"])[:10])
        return self._put(order_id, {
            "items": self._items(lines, current.get("erp_guard_cost_centre") or "", schedule),
            "erp_guard_notes": notes})

    def write_audit(self, record):
        body = {"action": record.action, "actor": record.actor, "outcome": record.outcome,
                "target": record.target or "", "payload_hash": record.payload_hash,
                "checks": json.dumps(record.checks), "detail": record.detail}
        return self.c.request("POST", _res(AUDIT_DOCTYPE), body=body)["data"]["name"]

    # purchase to pay: reads ------------------------------------------------------------
    def _p2p_get(self, doctype: str, id: str):
        d = self.get_doc(doctype, id)
        if d is None:
            return None
        table, _, _, _, parse = _P2P_DOCS[doctype]
        return parse(d, d.get(table) or [])

    def _p2p_list(self, doctype: str, filters: list, order_by: str) -> list:
        """Documents with their rows in two requests (headers, then rows by parent)."""
        table, child, heads_f, rows_f, parse = _P2P_DOCS[doctype]
        heads = list({h["name"]: h for h in self.find(
            doctype, [["company", "=", self.company], *filters], heads_f, order_by, 0)}
            .values())  # a filter on rows can repeat a header
        if not heads:
            return []
        rows = self.find(doctype, [["name", "in", [h["name"] for h in heads]]],
                         ["name", *(f"`tab{child}`.{f} as {f}" for f in rows_f),
                          f"`tab{child}`.idx as idx"], None, 0)
        by: dict[str, list[dict]] = {}
        for r in sorted(rows, key=lambda r: (r["name"], r.get("idx") or 0)):
            if r.get(rows_f[0]):
                by.setdefault(r["name"], []).append(r)
        return [parse(h, by.get(h["name"], [])) for h in heads]

    def item_info(self, codes):
        if not codes:
            return {}
        rows = self.find("Item", [["name", "in", list(codes)]],
                         ["name", "is_stock_item", "stock_uom"], None, 0)
        return {r["name"]: ItemInfo(r["name"], bool(r["is_stock_item"]), r["stock_uom"] or "")
                for r in rows}

    def get_request(self, id):
        return self._p2p_get("Material Request", id)

    def list_requests(self, *, since=None):
        f = [["material_request_type", "=", "Purchase"]]
        if since:
            f.append(["transaction_date", ">=", since.isoformat()])
        return self._p2p_list("Material Request", f, "transaction_date asc, name asc")

    def list_quotes(self, *, item_codes, since):
        if not item_codes:
            return []
        return self._p2p_list("Supplier Quotation", [
            ["transaction_date", ">=", since.isoformat()],
            ["Supplier Quotation Item", "item_code", "in", list(item_codes)]],
            "transaction_date asc, name asc")

    def list_receipts(self, *, po=None, supplier_id=None):
        f = []
        if supplier_id:
            f.append(["supplier", "=", supplier_id])
        if po:
            f.append(["Purchase Receipt Item", "purchase_order", "=", po])
        return self._p2p_list("Purchase Receipt", f, "posting_date asc, name asc")

    def get_receipt(self, id):
        return self._p2p_get("Purchase Receipt", id)

    def list_invoices(self, *, supplier_id=None, po=None, since=None):
        f = []
        if supplier_id:
            f.append(["supplier", "=", supplier_id])
        if po:
            f.append(["Purchase Invoice Item", "purchase_order", "=", po])
        if since:
            f.append(["bill_date", ">=", since.isoformat()])
        return self._p2p_list("Purchase Invoice", f, "posting_date asc, name asc")

    def get_invoice(self, id):
        return self._p2p_get("Purchase Invoice", id)

    def list_payments(self, *, supplier_id=None, invoice=None, since=None):
        f = [["party_type", "=", "Supplier"], ["payment_type", "=", "Pay"]]
        if supplier_id:
            f.append(["party", "=", supplier_id])
        if invoice:
            f.append(["Payment Entry Reference", "reference_name", "=", invoice])
        if since:
            f.append(["posting_date", ">=", since.isoformat()])
        return self._p2p_list("Payment Entry", f, "posting_date asc, name asc")

    def get_payment(self, id):
        return self._p2p_get("Payment Entry", id)

    def outstanding(self, invoice_id):
        """ERPNext's outstanding amount on a submitted invoice (payments and debit notes
        against it reduce it); nothing on a draft, a cancelled invoice or a debit note."""
        rows = self.find("Purchase Invoice", [["name", "=", invoice_id]],
                         ["docstatus", "is_return", "outstanding_amount"], None, 1)
        if not rows:
            raise ErpError(f"No Purchase Invoice {invoice_id}")
        r = rows[0]
        if r["docstatus"] != 1 or r.get("is_return"):
            return Decimal(0)
        return _dec(r["outstanding_amount"])

    # purchase to pay: writes -----------------------------------------------------------
    # Documents are built with ERPNext's own mappers where there's a source document, so the
    # links between them (order line -> receipt line -> invoice line) are ERPNext's. Every
    # purchase document built here sets ignore_pricing_rule, so a pricing rule can't change
    # a rate after the checks saw it.
    def _parse(self, doctype: str, d: dict):
        table, _, _, _, parse = _P2P_DOCS[doctype]
        return parse(d, d.get(table) or [])

    def create_request(self, cost_centre, lines, notes):
        """A draft Material Request of type Purchase. ERPNext's request has no notes field;
        the notes are checked and shown in the preview only."""
        when = (date.today() + timedelta(days=14)).isoformat()
        doc = self.insert({
            "doctype": "Material Request", "material_request_type": "Purchase",
            "company": self.company, "transaction_date": date.today().isoformat(),
            "schedule_date": when,
            "items": [{"item_code": ln.item_code, "qty": float(ln.qty), "schedule_date": when,
                       "cost_center": cost_centre} for ln in lines]})
        return self._parse("Material Request", doc)

    def create_rfq(self, request_id, suppliers):
        draft = self.map_doc("Material Request", request_id, "Request for Quotation")
        draft["suppliers"] = [{"supplier": s} for s in suppliers]
        draft["subject"] = draft.get("subject") or f"Quotation for {request_id}"
        draft["message_for_supplier"] = draft.get("message_for_supplier") or \
            f"Please quote for the items of {request_id}."
        return self.insert(draft)["name"]

    def create_quote(self, supplier_id, lines, rfq, quoted_on):
        """A draft Supplier Quotation: from a (submitted) request for quotation when there
        is one, keeping the lines' items at their quantities and rates."""
        _one_per_key([(ln.item_code,) for ln in lines])
        wanted = {(ln.item_code,): {"qty": float(ln.qty), "rate": float(ln.rate)}
                  for ln in lines}
        if rfq:
            draft = self.map_doc("Request for Quotation", rfq, "Supplier Quotation")
            draft["items"] = _keep_rows(draft.get("items") or [], wanted,
                                        lambda r: (r["item_code"],), rfq)
        else:
            draft = {"doctype": "Supplier Quotation", "company": self.company,
                     "items": [{"item_code": k[0], **v} for k, v in wanted.items()]}
        draft.update(supplier=supplier_id, transaction_date=quoted_on.isoformat(),
                     ignore_pricing_rule=1)
        return self._parse("Supplier Quotation", self.insert(draft))

    def create_receipt(self, po, lines, *, is_return=False, return_against=None):
        """A receipt mapped from the order (a return from the receipt), with the lines'
        quantities, then submitted: there's nothing to approve in a receipt."""
        _one_per_key([(ln.po, ln.item_code) for ln in lines])
        if is_return:
            if not return_against:
                raise ErpError("A return names the receipt it returns goods from")
            draft = self.map_doc("Purchase Receipt", return_against, "Purchase Receipt")
        else:
            draft = self.map_doc("Purchase Order", po, "Purchase Receipt")
        sign = -1 if is_return else 1
        wanted = {(ln.po, ln.item_code): {"qty": sign * float(ln.qty),
                                          "received_qty": sign * float(ln.qty),
                                          "rejected_qty": 0} for ln in lines}
        rows = _keep_rows(draft.get("items") or [], wanted,
                          lambda r: (r.get("purchase_order"), r["item_code"]),
                          return_against or po)
        for r in rows:
            r["stock_qty"] = r["qty"] * float(r.get("conversion_factor") or 1)
        draft.update(items=rows, ignore_pricing_rule=1)
        doc = self.insert(draft)
        return self._parse("Purchase Receipt", self.submit("Purchase Receipt", doc["name"]))

    def create_invoice(self, supplier_id, bill_no, bill_date, lines, remarks):
        """A draft invoice: lines with a receipt are mapped from that receipt, the rest from
        their order, merged into one invoice with the lines' quantities and rates."""
        if not lines:
            raise ErpError("An invoice needs at least one line")

        def key(r):
            return (r.get("purchase_receipt") or None, r.get("purchase_order"), r["item_code"])
        _one_per_key([(ln.receipt, ln.po, ln.item_code) for ln in lines])
        sources: dict[tuple[str, str], dict[tuple, dict]] = {}
        for ln in lines:
            src = ("Purchase Receipt", ln.receipt) if ln.receipt else ("Purchase Order", ln.po)
            sources.setdefault(src, {})[(ln.receipt, ln.po, ln.item_code)] = {
                "qty": float(ln.qty), "rate": float(ln.rate), "discount_percentage": 0,
                "discount_amount": 0}
        doc, rows = None, []
        for (dt, name), wanted in sources.items():
            draft = self.map_doc(dt, name, "Purchase Invoice")
            rows += _keep_rows(draft.get("items") or [], wanted, key, name)
            doc = doc or draft
        doc.update(items=rows, supplier=supplier_id, bill_no=bill_no,
                   bill_date=bill_date.isoformat(), remarks=remarks, ignore_pricing_rule=1,
                   update_stock=0, is_paid=0)
        return self._parse("Purchase Invoice", self.insert(doc))

    def set_invoice_hold(self, id, hold, comment):
        """ERPNext's own hold and release (on_hold, hold_comment) on the invoice."""
        if hold:
            self.call(RUN_DOC_METHOD, dt="Purchase Invoice", dn=id, method="block_invoice",
                      args={"hold_comment": comment})
        else:
            self.call(RUN_DOC_METHOD, dt="Purchase Invoice", dn=id, method="unblock_invoice")
        inv = self.get_invoice(id)
        if inv is None:
            raise ErpError(f"No Purchase Invoice {id}")
        return inv

    def create_debit_note(self, invoice_id, lines, remarks):
        """A draft debit note mapped from the invoice, returning the lines' quantities at
        their rates. It reduces the original invoice's outstanding amount, so what is owed
        on the invoice is one number."""
        _one_per_key([(ln.po, ln.item_code) for ln in lines])
        draft = self.map_doc("Purchase Invoice", invoice_id, "Purchase Invoice")
        wanted = {(ln.po, ln.item_code): {"qty": -float(ln.qty), "rate": float(ln.rate),
                                          "discount_percentage": 0, "discount_amount": 0}
                  for ln in lines}
        draft["items"] = _keep_rows(draft.get("items") or [], wanted,
                                    lambda r: (r.get("purchase_order"), r["item_code"]),
                                    invoice_id)
        draft.update(remarks=remarks, update_outstanding_for_self=0, ignore_pricing_rule=1,
                     update_stock=0, is_paid=0)
        return self._parse("Purchase Invoice", self.insert(draft))

    def create_payment(self, supplier_id, allocations, paid_from):
        """A draft Payment Entry (Pay, to the supplier) from ERPNext's own payment for each
        invoice, merged into one, allocating exactly the given amounts. Paid from the given
        ledger account; no bank account record, and no bank fields are sent."""
        if not allocations:
            raise ErpError("A payment pays at least one invoice")
        doc, refs = None, []
        for a in allocations:
            draft = self.map_doc("Purchase Invoice", a.invoice, "Payment Entry")
            mine = [r for r in draft.get("references") or []
                    if (r.get("reference_doctype"), r.get("reference_name"))
                    == ("Purchase Invoice", a.invoice)]
            if not mine:
                raise ErpError(f"ERPNext's payment for {a.invoice} doesn't reference it")
            refs.append({**mine[0], "allocated_amount": float(a.amount)})
            doc = doc or draft
        total = float(sum((a.amount for a in allocations), Decimal(0)))
        doc.update(references=refs, payment_type="Pay", party_type="Supplier",
                   party=supplier_id, paid_from=paid_from, paid_amount=total,
                   received_amount=total, reference_date=date.today().isoformat(),
                   reference_no=", ".join(a.invoice for a in allocations)[:140])
        for k in [k for k, v in doc.items() if _BANK_KEY.search(k) and v in (None, "")]:
            del doc[k]  # e.g. bank, bank_account_no, party_bank_account: all empty
        return self._parse("Payment Entry", self.insert(doc))

    def workflow(self, doctype, id, action, comment=""):
        """Apply a workflow transition. A purchase order's rejection reason is saved on the
        order first (as set_status); other comments are kept in erp-guard's audit trail."""
        if doctype == "Purchase Order" and action == REJECT and comment:
            self._put(id, {"erp_guard_reason": comment})
        self.apply_workflow(doctype, id, action)

    def submit_doc(self, doctype, id):
        """Submit a document type without an approval workflow (a direct submit would skip
        a workflow's approval, so those are refused)."""
        if self.get_meta(doctype).get("__workflow_docs"):
            raise ErpError(f"{doctype} has an approval workflow; it is submitted by "
                           "approving it")
        self.submit(doctype, id)

    def cancel_doc(self, doctype, id, reason):
        """Cancel a submitted document (ERPNext keeps it, cancelled). A purchase order goes
        through cancel_order, which also handles drafts and stores the reason; for other
        types the reason is in erp-guard's audit trail."""
        if doctype == "Purchase Order":
            self.cancel_order(id, reason)
            return
        d = self._require(doctype, id)
        if d.get("docstatus") != 1:
            raise ErpError(f"{doctype} {id} isn't submitted; only submitted documents are "
                           "cancelled")
        self.cancel(doctype, id)

    def update_draft(self, doctype, id, fields):
        """Only the fields models.EDITABLE allows. Changed payment allocations also set the
        paid amount to their sum, as create_payment does."""
        check_editable(doctype, fields)
        d = self._require(doctype, id)
        if d.get("docstatus") != 0:
            raise ErpError(f"{doctype} {id} isn't a draft")
        doc = dict(d)
        for k, v in _plain(fields).items():
            if isinstance(v, list):
                have = d.get(k) or []
                if len(v) != len(have):
                    raise ErpError(f"{k}: {len(v)} rows given, the draft has {len(have)}")
                doc[k] = [{**r, **p} for r, p in zip(have, v, strict=True)]
            else:
                doc[k] = v
        if doctype == "Payment Entry" and "references" in fields:
            total = float(sum((_dec(r["allocated_amount"]) for r in doc["references"]),
                              Decimal(0)))
            doc.update(paid_amount=total, received_amount=total)
        self.save(doc)

    def close_order(self, po, reason):
        """ERPNext's own close (update_status Closed), with the reason saved on the order."""
        self._current(po)
        if reason:
            self._put(po, {"erp_guard_reason": reason})
        self.call(PO_UPDATE_STATUS, status="Closed", name=po)
        return self.get_order(po)

    def create_change_request(self, target_doctype, target_name, fields):
        """An ERP Guard Change Request, submitted for approval by a second person. Bank
        details are refused here, as on any write."""
        self.c._check_bank(fields, target_doctype)
        self._require(target_doctype, target_name)
        doc = self.insert({"doctype": CHANGE_REQUEST, "target_doctype": target_doctype,
                           "target_name": target_name,
                           "fields_json": json.dumps(_plain(fields), sort_keys=True),
                           "requested_by": self.whoami()})
        self.apply_workflow(CHANGE_REQUEST, doc["name"], SUBMIT_FOR_APPROVAL)
        return doc["name"]

    def apply_change_request(self, id, approve):
        """Approve: the change is saved on the target document first; only then does the
        workflow approve (submit) the request, which is marked Applied. If the target can't
        be saved, the request stays pending and the error is raised. Reject: the workflow
        rejects it and it is marked Rejected. Either way it records who decided."""
        cr = self._require(CHANGE_REQUEST, id)
        if (cr.get("docstatus"), cr.get("workflow_state")) != (0, "Pending Approval"):
            raise ErpError(f"Change request {id} isn't pending")
        if approve:
            target = self._require(cr["target_doctype"], cr["target_name"])
            self.save({**target, **json.loads(cr["fields_json"])})
        self.apply_workflow(CHANGE_REQUEST, id, APPROVE if approve else REJECT)
        self.c.request("PUT", _res(CHANGE_REQUEST, id), body={
            "status": "Applied" if approve else "Rejected", "decided_by": self.whoami()})
