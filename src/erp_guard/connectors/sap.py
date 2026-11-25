"""SAP S/4HANA Cloud connector.

- Purchase orders: the OData V4 Purchase Order API, service group API_PURCHASEORDER_2
  (communication scenario SAP_COM_0053). The V2 API_PURCHASEORDER_PROCESS_SRV is the
  deprecated predecessor and isn't used.
- Suppliers: API_BUSINESS_PARTNER, entity A_Supplier (OData V2, read only here).

Reads run live against the api.sap.com sandbox when SAP_API_KEY is set (the sandbox is
read-only and authenticates with an `APIKey` header). Writes follow SAP's published
schema (CSRF token fetch, deep insert of header + items + account assignment, If-Match
on PATCH) and are tested against fixtures only: **not run against a live tenant**.

What S/4HANA does differently from ERPNext, and how this connector handles it:
- Approval is SAP's flexible workflow (the approver's My Inbox), not part of
  API_PURCHASEORDER_2. approve/reject raise an error that says so; submit is a no-op
  because saving the order starts the workflow.
- Cancelling sets the item deletion indicator (PurchasingDocumentDeletionCode = "L"),
  as ME22N does. DELETE is never sent.
- There's no custom table for the audit trail in a standard tenant, so audit records
  go to a local JSONL file (SAP_AUDIT_FILE).
- Cost-centre budgets don't live in this API; they come from SAP_BUDGETS_FILE (JSON).
- Supplier onboarding (business partner creation) isn't implemented for SAP yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from erp_guard.connectors.base import ErpError
from erp_guard.connectors.erpnext import HardLimitError, _walk_keys
from erp_guard.models import AuditRecord, CostCentre, Line, PurchaseOrder, Status, Supplier

SANDBOX = "https://sandbox.api.sap.com/s4hanacloud"
PO_SERVICE = "/sap/opu/odata4/sap/api_purchaseorder_2/srvd_a2x/sap/purchaseorder/0001"
BP_SERVICE = "/sap/opu/odata/sap/API_BUSINESS_PARTNER"

_ALLOWED = [  # (verb, path regex relative to the base URL)
    ("GET", rf"{re.escape(PO_SERVICE)}/(\$metadata)?"),
    ("GET", rf"{re.escape(PO_SERVICE)}/PurchaseOrder(\('[^']*'\))?(/_PurchaseOrderItem)?"),
    ("GET", rf"{re.escape(BP_SERVICE)}/A_Supplier(\('[^']*'\))?"),
    ("POST", rf"{re.escape(PO_SERVICE)}/PurchaseOrder"),
    ("PATCH", rf"{re.escape(PO_SERVICE)}/PurchaseOrderItem\('[^']*','[^']*'\)"),
]
_BANK_KEY = re.compile(r"bank|iban|swift|payment|payee|AlternativePayee", re.IGNORECASE)

# EKKO-PROCSTAT (domain MEPROCSTATE): 01 version in process, 02 active, 03 in release,
# 04 partially released, 05 release completed, 08 rejected.
_PROCSTAT = {"01": Status.DRAFT, "03": Status.PENDING, "04": Status.PENDING,
             "05": Status.APPROVED, "08": Status.REJECTED}


class ODataClient:
    def __init__(self, base_url: str, headers: dict[str, str],
                 transport: httpx.BaseTransport | None = None, timeout: float = 30.0):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers,
                                 transport=transport, timeout=timeout)
        self._csrf: str | None = None

    def _guard(self, verb: str, path: str, body) -> None:
        if not any(v == verb and re.fullmatch(rx, path) for v, rx in _ALLOWED):
            raise HardLimitError(f"{verb} {path} is not allowed")
        if body is not None:
            bad = sorted({k for k in _walk_keys(body) if _BANK_KEY.search(str(k))})
            if bad:
                raise HardLimitError(f"bank or payment fields are never written: {bad}")

    def request(self, verb: str, path: str, params=None, body=None, headers=None):
        self._guard(verb, path, body)
        hdrs = dict(headers or {})
        if verb != "GET":
            hdrs["x-csrf-token"] = self._token()
        try:
            r = self.http.request(verb, path, params=params, json=body, headers=hdrs)
        except httpx.HTTPError as exc:
            raise ErpError(f"SAP unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise ErpError(f"SAP {r.status_code} on {verb} {path}: {r.text[:400]}")
        return r

    def _token(self) -> str:
        if self._csrf is None:
            r = self.request("GET", f"{PO_SERVICE}/", headers={"x-csrf-token": "fetch"})
            self._csrf = r.headers.get("x-csrf-token")
            if not self._csrf:
                raise ErpError("SAP returned no CSRF token")
        return self._csrf


def _key(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class SapS4:
    name = "sap-s4hana"

    def __init__(self, client: ODataClient, user: str, *, company_code: str = "1010",
                 purchasing_org: str = "1010", purchasing_group: str = "001",
                 plant: str = "1010", gl_account: str = "61008000", currency: str = "USD",
                 budgets: dict[str, Decimal] | None = None, audit_file: Path | None = None):
        self.c, self.user = client, user
        self.org = dict(CompanyCode=company_code, PurchasingOrganization=purchasing_org,
                        PurchasingGroup=purchasing_group)
        self.plant, self.gl_account, self.currency = plant, gl_account, currency
        self.budgets = budgets or {}
        self.audit_file = audit_file or Path("audit/sap-audit.jsonl")

    @classmethod
    def from_env(cls) -> SapS4:
        key = os.environ.get("SAP_API_KEY")
        url = os.environ.get("SAP_BASE_URL", SANDBOX)
        if url == SANDBOX and not key:
            raise SystemExit("Set SAP_API_KEY (free api.sap.com account) for sandbox reads.")
        headers = {"Accept": "application/json"}
        if key:
            headers["APIKey"] = key
        budgets = {}
        if os.environ.get("SAP_BUDGETS_FILE"):
            raw = json.loads(Path(os.environ["SAP_BUDGETS_FILE"]).read_text())
            budgets = {k: Decimal(str(v)) for k, v in raw.items()}
        return cls(ODataClient(url, headers), os.environ.get("SAP_USER", "SANDBOX_USER"),
                   budgets=budgets,
                   audit_file=Path(os.environ.get("SAP_AUDIT_FILE", "audit/sap-audit.jsonl")))

    # mapping ---------------------------------------------------------------------------
    @staticmethod
    def _order(d: dict) -> PurchaseOrder:
        items = [i for i in d.get("_PurchaseOrderItem", [])]
        live = [i for i in items if not i.get("PurchasingDocumentDeletionCode")]
        if d.get("PurchaseOrderDeletionCode") or (items and not live):
            status = Status.CANCELLED
        elif d.get("PurchasingProcessingStatus") == "02":
            status = Status.PENDING if d.get("ReleaseIsNotCompleted") else Status.APPROVED
        else:
            status = _PROCSTAT.get(d.get("PurchasingProcessingStatus") or "", Status.DRAFT)
        cc = ""
        for i in live or items:
            for a in i.get("_PurOrdAccountAssignment", []) or []:
                cc = cc or (a.get("CostCenter") or "")
        lines = tuple(Line(i.get("Material") or i.get("PurchaseOrderItemText") or "",
                           Decimal(str(i.get("OrderQuantity") or 0)),
                           Decimal(str(i.get("NetPriceAmount") or 0)),
                           i.get("PurchaseOrderItemText") or "") for i in live)
        return PurchaseOrder(d["PurchaseOrder"], d.get("Supplier") or "", cc,
                             d.get("CreatedByUser") or "", status,
                             date.fromisoformat(str(d.get("PurchaseOrderDate"))[:10]), lines)

    _EXPAND = "_PurchaseOrderItem($expand=_PurOrdAccountAssignment)"

    # reads -----------------------------------------------------------------------------
    def whoami(self) -> str:
        # The sandbox key carries no user; in a tenant this is the communication user.
        return self.user

    def list_suppliers(self) -> list[Supplier]:
        r = self.c.request("GET", f"{BP_SERVICE}/A_Supplier", params={
            "$select": "Supplier,SupplierName,TaxNumber1,PostingIsBlocked,PurchasingIsBlocked,"
                       "DeletionIndicator", "$top": "5000", "$format": "json"})
        rows = r.json().get("d", {}).get("results", [])
        return [Supplier(x["Supplier"], x.get("SupplierName") or x["Supplier"],
                         x.get("TaxNumber1") or None, bool(x.get("DeletionIndicator")),
                         bool(x.get("PostingIsBlocked") or x.get("PurchasingIsBlocked")))
                for x in rows]

    def get_supplier(self, supplier_id: str) -> Supplier | None:
        return next((s for s in self.list_suppliers() if s.id == supplier_id), None)

    def get_order(self, order_id: str) -> PurchaseOrder | None:
        try:
            r = self.c.request("GET", f"{PO_SERVICE}/PurchaseOrder({_key(order_id)})",
                               params={"$expand": self._EXPAND})
        except ErpError as exc:
            if " 404 " in str(exc):
                return None
            raise
        return self._order(r.json())

    def doc_version(self, doctype: str, name: str) -> str:
        """A hash of the order as read (header and items, with their ETags); "" if there is
        no such order. Only purchase orders are written through this connector."""
        if doctype != "Purchase Order":
            raise ErpError(f"The SAP connector doesn't write {doctype}")
        try:
            r = self.c.request("GET", f"{PO_SERVICE}/PurchaseOrder({_key(name)})",
                               params={"$expand": self._EXPAND})
        except ErpError as exc:
            if " 404 " in str(exc):
                return ""
            raise
        blob = json.dumps(r.json(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def list_orders(self, *, supplier_id=None, cost_centre=None, since=None):
        flt = []
        if supplier_id:
            flt.append(f"Supplier eq {_key(supplier_id)}")
        if since:
            flt.append(f"PurchaseOrderDate ge {since.isoformat()}")
        params = {"$expand": self._EXPAND, "$top": "1000", "$orderby": "PurchaseOrderDate desc"}
        if flt:
            params["$filter"] = " and ".join(flt)
        r = self.c.request("GET", f"{PO_SERVICE}/PurchaseOrder", params=params)
        orders = [self._order(d) for d in r.json().get("value", [])]
        return [o for o in orders if cost_centre is None or o.cost_centre == cost_centre]

    def get_cost_centre(self, cost_centre: str) -> CostCentre | None:
        b = self.budgets.get(cost_centre)
        return CostCentre(cost_centre, b) if b is not None else None

    def item_codes(self) -> set[str]:
        return {ln.item_code for o in self.list_orders() for ln in o.lines}

    def last_purchase_prices(self, item_codes):
        out: dict[str, Decimal] = {}
        approved = sorted((o for o in self.list_orders() if o.status == Status.APPROVED),
                          key=lambda o: (o.order_date, o.id))
        for o in approved:
            for ln in o.lines:
                if ln.item_code in item_codes:
                    out[ln.item_code] = ln.rate
        return out

    def read_audit(self, target=None, limit=50):
        if not self.audit_file.exists():
            return []
        rows = [json.loads(x) for x in self.audit_file.read_text().splitlines() if x.strip()]
        rows = [r for r in rows if target is None or r.get("target") == target]
        return [AuditRecord(**r) for r in reversed(rows)][:limit]

    # writes ----------------------------------------------------------------------------
    def create_supplier(self, name, tax_id, notes, onboarded_by):
        raise ErpError("Supplier onboarding (business partner creation) isn't implemented "
                       "for SAP yet.")

    def _item(self, n: int, ln: Line, cost_centre: str) -> dict:
        return {"PurchaseOrderItem": f"{n * 10:05d}", "Material": ln.item_code,
                "PurchaseOrderItemText": (ln.description or ln.item_code)[:40],
                "OrderQuantity": str(ln.qty), "PurchaseOrderQuantityUnit": "EA",
                "NetPriceAmount": str(ln.rate), "DocumentCurrency": self.currency,
                "Plant": self.plant, "AccountAssignmentCategory": "K",
                "_PurOrdAccountAssignment": [{"AccountAssignmentNumber": "01",
                                              "CostCenter": cost_centre,
                                              "GLAccount": self.gl_account}]}

    @staticmethod
    def _no_links(request, quotations, sole_source_reason) -> None:
        if request or quotations or sole_source_reason:
            raise ErpError("The SAP connector doesn't support purchase requests, quotations "
                           "or sole-source reasons yet.")

    def create_order(self, supplier_id, cost_centre, lines, notes, required_by, *,
                     request=None, quotations=(), sole_source_reason=""):
        self._no_links(request, quotations, sole_source_reason)
        body = {"PurchaseOrderType": "NB", **self.org, "Supplier": supplier_id,
                "DocumentCurrency": self.currency,
                "PurchaseOrderDate": date.today().isoformat(),
                "_PurchaseOrderItem": [self._item(i + 1, ln, cost_centre)
                                       for i, ln in enumerate(lines)]}
        if notes:
            body["_PurchaseOrderNote"] = [{"TextObjectType": "F01", "Language": "EN",
                                           "PlainLongText": notes[:1000]}]
        r = self.c.request("POST", f"{PO_SERVICE}/PurchaseOrder", body=body)
        return self._order(r.json())

    def set_status(self, order_id, status, reason):
        if status == Status.PENDING:
            # Saving the order already started SAP's flexible workflow.
            return self.get_order(order_id)
        raise ErpError(f"Setting '{status.value}' in S/4HANA runs through flexible workflow "
                       "(the approver's My Inbox), not API_PURCHASEORDER_2.")

    def approve_order(self, order_id):
        raise ErpError("Approval in S/4HANA runs through flexible workflow (My Inbox), not "
                       "API_PURCHASEORDER_2; erp-guard's SAP connector doesn't approve.")

    def _patch_item(self, order_id: str, item: dict, changes: dict) -> None:
        keys = f"{_key(order_id)},{_key(item['PurchaseOrderItem'])}"
        path = f"{PO_SERVICE}/PurchaseOrderItem({keys})"
        etag = item.get("@odata.etag") or "*"
        self.c.request("PATCH", path, body=changes, headers={"If-Match": etag})

    def _raw_items(self, order_id: str) -> list[dict]:
        r = self.c.request("GET", f"{PO_SERVICE}/PurchaseOrder({_key(order_id)})",
                           params={"$expand": self._EXPAND})
        return r.json().get("_PurchaseOrderItem", [])

    def cancel_order(self, order_id, reason):
        for item in self._raw_items(order_id):
            if not item.get("PurchasingDocumentDeletionCode"):
                self._patch_item(order_id, item, {"PurchasingDocumentDeletionCode": "L"})
        return self.get_order(order_id)

    def amend_order(self, order_id, lines, notes, *, request=None, quotations=(),
                    sole_source_reason=""):
        self._no_links(request, quotations, sole_source_reason)
        items = [i for i in self._raw_items(order_id)
                 if not i.get("PurchasingDocumentDeletionCode")]
        if len(items) != len(lines):
            raise ErpError("The SAP connector amends existing lines in place; adding or "
                           "removing lines isn't supported yet.")
        for item, ln in zip(items, lines, strict=True):
            if item.get("Material") != ln.item_code:
                raise ErpError("Changing an item's material isn't supported; cancel and "
                               "raise a new order.")
            self._patch_item(order_id, item, {"OrderQuantity": str(ln.qty),
                                              "NetPriceAmount": str(ln.rate)})
        return self.get_order(order_id)

    def write_audit(self, record):
        self.audit_file.parent.mkdir(parents=True, exist_ok=True)
        rid = f"AUD-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
        row = {**record.__dict__, "id": rid,
               "timestamp": datetime.now(UTC).isoformat(timespec="seconds")}
        with self.audit_file.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return rid

