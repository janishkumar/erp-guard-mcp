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
from datetime import date
from decimal import Decimal
from urllib.parse import quote

import httpx

from erp_guard.checks import BANK_KEY as _BANK_KEY
from erp_guard.connectors.base import ErpError
from erp_guard.models import AuditRecord, CostCentre, Line, PurchaseOrder, Status, Supplier
from erp_guard.policy import Policy, load_policy

AUDIT_DOCTYPE = "ERP Guard Audit"

# v1's document types: read through /api/resource, and the default writable set.
RESOURCES = frozenset({"Supplier", "Purchase Order", "Item", "Cost Center", AUDIT_DOCTYPE})

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
                    "erp_guard_onboarded_by", "erp_guard_notes"]


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
                 writable: Callable[[str], bool] = lambda d: d in RESOURCES,
                 allowed_bank_values: Iterable[str] = ()):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
            headers={"Authorization": f"token {api_key}:{api_secret}",
                     "Accept": "application/json"},
        )
        self.writable = writable
        self.allowed_bank_values = frozenset(allowed_bank_values)

    def _require_writable(self, doctype) -> None:
        if not isinstance(doctype, str) or not doctype or not self.writable(doctype):
            raise HardLimitError(f"doctype not writable: {doctype}")

    def _write_targets(self, method: str, body) -> list:
        """The document types a write method touches, read from its arguments."""
        where = WRITE_METHODS[method]
        body = body if isinstance(body, dict) else {}
        if where == "doctype":
            return [body.get("doctype")]
        if where == "dt":
            if (body.get("dt"), body.get("method")) not in DOC_METHODS or not body.get("dn"):
                raise HardLimitError(f"document method not allowed: {body.get('method')}")
            if not isinstance(body.get("args"), dict | None):
                raise HardLimitError("document method args must be an object")
            return [body["dt"]]
        if where == "doc":
            doc = body.get("doc")
            if not isinstance(doc, dict):
                raise HardLimitError(f"{method} needs the document as an object")
            # A child row inserted on its own is written into its parent.
            return [doc.get("doctype")] + ([doc["parenttype"]] if doc.get("parenttype") else [])
        return [where]

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
                self._require_writable(name)
        elif name not in METHODS:
            raise HardLimitError(f"method not allowed: {name}")
        elif name in GET_ONLY_METHODS and verb != "GET":
            raise HardLimitError(f"{name} is read with GET only")
        elif name in WRITE_METHODS:
            if verb != "POST":
                raise HardLimitError(f"{name} is sent with POST only")
            for target in self._write_targets(name, body):
                self._require_writable(target)
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


_STATUS = {s.value: s for s in Status}


def _order_from_doc(d: dict) -> PurchaseOrder:
    if d.get("docstatus") == 1:
        status = Status.APPROVED
    elif d.get("docstatus") == 2:
        status = Status.CANCELLED
    else:
        status = _STATUS.get(d.get("erp_guard_status") or "Draft", Status.DRAFT)
    lines = tuple(
        Line(i["item_code"], Decimal(str(i["qty"])), Decimal(str(i["rate"])),
             i.get("description") or "")
        for i in d.get("items", [])
    )
    return PurchaseOrder(
        id=d["name"], supplier_id=d["supplier"],
        cost_centre=d.get("erp_guard_cost_centre") or "",
        requester=d["owner"], status=status,
        order_date=date.fromisoformat(str(d["transaction_date"])[:10]), lines=lines,
        notes=d.get("erp_guard_notes") or "", approver=d.get("erp_guard_approver") or None,
    )


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

    def get_meta(self, doctype: str) -> dict:
        """The DocType document (fields, permissions, is_submittable, ...) with its child
        tables' DocTypes under `child_doctypes`, by name."""
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
                        d.get("erp_guard_onboarded_by") or None, d.get("erp_guard_notes") or "")

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
             "erp_guard_status", "transaction_date", "erp_guard_approver", "erp_guard_notes"],
            filters, order_by="transaction_date desc")
        if not heads:
            return []
        items = self.c.request("POST", "/api/method/frappe.client.get_list", body={
            "doctype": "Purchase Order",
            "fields": ["name", "`tabPurchase Order Item`.item_code as item_code",
                       "`tabPurchase Order Item`.qty as qty",
                       "`tabPurchase Order Item`.rate as rate",
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
        body = {"supplier": supplier_id, "company": self.company,
                "transaction_date": date.today().isoformat(),
                "schedule_date": required_by.isoformat(),
                "erp_guard_cost_centre": cost_centre, "erp_guard_status": Status.DRAFT.value,
                "erp_guard_notes": notes, "items": self._items(lines, cost_centre, required_by)}
        return _order_from_doc(self.c.request("POST", _res("Purchase Order"), body=body)["data"])

    def _put(self, order_id: str, body: dict) -> PurchaseOrder:
        d = self.c.request("PUT", _res("Purchase Order", order_id), body=body)["data"]
        return _order_from_doc(d)

    def set_status(self, order_id, status, reason):
        return self._put(order_id, {"erp_guard_status": status.value,
                                    "erp_guard_reason": reason or ""})

    def approve_order(self, order_id):
        user = self.whoami()
        return self._put(order_id, {"erp_guard_status": Status.APPROVED.value,
                                    "erp_guard_approver": user, "docstatus": 1})

    def cancel_order(self, order_id, reason):
        current = self.c.get_doc("Purchase Order", order_id)
        if current is None:
            raise ErpError(f"No purchase order {order_id}")
        if current["docstatus"] == 1:
            self.c.request("POST", "/api/method/frappe.client.cancel",
                           body={"doctype": "Purchase Order", "name": order_id})
            return self.get_order(order_id)
        return self._put(order_id, {"erp_guard_status": Status.CANCELLED.value,
                                    "erp_guard_reason": reason})

    def amend_order(self, order_id, lines, notes):
        current = self.c.get_doc("Purchase Order", order_id)
        if current is None:
            raise ErpError(f"No purchase order {order_id}")
        schedule = date.fromisoformat(str(current["schedule_date"])[:10])
        return self._put(order_id, {
            "items": self._items(lines, current.get("erp_guard_cost_centre") or "", schedule),
            "erp_guard_notes": notes, "erp_guard_status": Status.DRAFT.value})

    def write_audit(self, record):
        body = {"action": record.action, "actor": record.actor, "outcome": record.outcome,
                "target": record.target or "", "payload_hash": record.payload_hash,
                "checks": json.dumps(record.checks), "detail": record.detail}
        return self.c.request("POST", _res(AUDIT_DOCTYPE), body=body)["data"]["name"]
