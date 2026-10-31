"""ERPNext connector: talks to ERPNext over its REST API (token auth, one API key per ERP user).

The HTTP layer only speaks to an allowlist of doctypes and methods, and never sends
DELETE. That's a second wall behind the connector protocol: even a bug in this module
can't reach payments, bank accounts or deletion.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from decimal import Decimal
from urllib.parse import quote

import httpx

from erp_guard.connectors.base import ErpError
from erp_guard.models import AuditRecord, CostCentre, Line, PurchaseOrder, Status, Supplier

AUDIT_DOCTYPE = "ERP Guard Audit"

RESOURCES = frozenset({"Supplier", "Purchase Order", "Item", "Cost Center", AUDIT_DOCTYPE})
METHODS = frozenset({"frappe.auth.get_logged_user", "frappe.client.cancel",
                     "frappe.client.get_list"})
HTTP_VERBS = frozenset({"GET", "POST", "PUT"})
_BANK_KEY = re.compile(r"bank|iban|swift|bic|routing|account_no|payment", re.IGNORECASE)

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


class FrappeClient:
    def __init__(self, base_url: str, api_key: str, api_secret: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 30.0):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
            headers={"Authorization": f"token {api_key}:{api_secret}",
                     "Accept": "application/json"},
        )

    def _guard(self, verb: str, path: str, body) -> None:
        if verb not in HTTP_VERBS:
            raise HardLimitError(f"{verb} is never sent")
        m = re.fullmatch(r"/api/(resource|method)/([^/?]+)(/[^?]*)?", path)
        if not m:
            raise HardLimitError(f"path not allowed: {path}")
        kind, name = m.group(1), httpx.URL("/" + m.group(2)).path[1:]
        name = name.replace("%20", " ")
        if kind == "resource" and name not in RESOURCES:
            raise HardLimitError(f"doctype not allowed: {name}")
        if kind == "method" and name not in METHODS:
            raise HardLimitError(f"method not allowed: {name}")
        if kind == "method" and name == "frappe.client.cancel" and body \
                and body.get("doctype") != "Purchase Order":
            raise HardLimitError("only purchase orders can be cancelled")
        if body is not None:
            bad = sorted({k for k in _walk_keys(body) if _BANK_KEY.search(str(k))})
            if bad:
                raise HardLimitError(f"bank or payment fields are never written: {bad}")

    def request(self, verb: str, path: str, params: dict | None = None, body=None):
        self._guard(verb, path, body)
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

    @classmethod
    def from_env(cls, user_tag: str | None = None) -> ErpNext:
        """Credentials from the environment. `user_tag` (e.g. "MANAGER") picks the
        per-user keys that `make seed` writes, instead of ERPNEXT_API_KEY/SECRET."""
        kname, sname = (("ERPNEXT_API_KEY", "ERPNEXT_API_SECRET") if user_tag is None else
                        (f"ERPNEXT_KEY_{user_tag}", f"ERPNEXT_SECRET_{user_tag}"))
        missing = [k for k in (kname, sname) if not os.environ.get(k)]
        if missing:
            raise SystemExit(f"Set {', '.join(missing)} (make seed writes them to .env).")
        client = FrappeClient(os.environ.get("ERPNEXT_URL", "http://localhost:8080"),
                              os.environ[kname], os.environ[sname])
        return cls(client, os.environ.get("ERPNEXT_COMPANY", "Lakeside Demo Utilities"),
                   os.environ.get("ERPNEXT_SUPPLIER_GROUP", "Demo Suppliers"))

    # reads -----------------------------------------------------------------------------
    def whoami(self) -> str:
        return self.c.request("GET", "/api/method/frappe.auth.get_logged_user")["message"]

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
