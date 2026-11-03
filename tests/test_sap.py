"""SAP connector against fixtures built from SAP's published schema (see fixtures/sap)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from erp_guard.connectors.base import ErpError
from erp_guard.connectors.erpnext import HardLimitError
from erp_guard.connectors.sap import BP_SERVICE, PO_SERVICE, ODataClient, SapS4
from erp_guard.models import Line, Status
from erp_guard.policy import load_policy
from erp_guard.service import Service

FIX = Path(__file__).parent / "fixtures" / "sap"


def load(name):
    return json.loads((FIX / name).read_text())


class FakeSap:
    """Records requests and answers from the fixtures."""

    def __init__(self):
        self.calls: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = req.url.path
        if req.headers.get("x-csrf-token") == "fetch":
            return httpx.Response(200, headers={"x-csrf-token": "tok-123"}, json={})
        if req.method == "GET" and path.endswith("/A_Supplier"):
            return httpx.Response(200, json=load("suppliers.json"))
        if req.method == "GET" and path.endswith("/PurchaseOrder"):
            return httpx.Response(200, json=load("purchase_orders.json"))
        if req.method == "GET" and "/PurchaseOrder(" in path:
            po = path.split("'")[1]
            for d in load("purchase_orders.json")["value"] + [load("create_response.json")]:
                if d["PurchaseOrder"] == po:
                    return httpx.Response(200, json=d)
            return httpx.Response(404, json={"error": {"message": "not found"}})
        if req.method == "POST" and path.endswith("/PurchaseOrder"):
            assert req.headers["x-csrf-token"] == "tok-123"
            return httpx.Response(201, json=load("create_response.json"))
        if req.method == "PATCH":
            assert req.headers["x-csrf-token"] == "tok-123" and req.headers["If-Match"]
            return httpx.Response(204)
        return httpx.Response(500, json={"unexpected": str(req.url)})


@pytest.fixture
def sap(tmp_path):
    fake = FakeSap()
    client = ODataClient("https://sap.example", {"APIKey": "test"},
                         transport=httpx.MockTransport(fake))
    s = SapS4(client, "CB9980000010", budgets={"10101101": Decimal("10000")},
              audit_file=tmp_path / "audit.jsonl")
    s.fake = fake
    return s


def test_reads_map_sap_fields(sap):
    sups = {s.id: s for s in sap.list_suppliers()}
    assert sups["17300002"].name == "HERC RENTALS INC" and sups["17300003"].on_hold
    orders = {o.id: o for o in sap.list_orders()}
    assert orders["4500000101"].status == Status.APPROVED
    assert orders["4500000102"].status == Status.PENDING
    assert orders["4500000103"].status == Status.APPROVED
    assert orders["4500000104"].status == Status.CANCELLED  # all items deletion-flagged
    assert orders["4500000103"].total == Decimal("6600")
    assert orders["4500000101"].cost_centre == "10101101"
    assert sap.last_purchase_prices(["TG11"]) == {"TG11": Decimal("45.5")}
    assert sap.list_orders(cost_centre="10101102")[0].supplier_id == "17300002"


def test_create_is_a_deep_insert_with_csrf(sap):
    o = sap.create_order("17300001", "10101101", [Line("TG11", Decimal(4), Decimal(46))],
                         "note", None)
    assert o.id == "4500000201"
    post = [r for r in sap.fake.calls if r.method == "POST"][0]
    body = json.loads(post.content)
    assert body["PurchaseOrderType"] == "NB" and body["Supplier"] == "17300001"
    item = body["_PurchaseOrderItem"][0]
    assert item["AccountAssignmentCategory"] == "K"
    assert item["_PurOrdAccountAssignment"][0]["CostCenter"] == "10101101"
    assert post.url.path == PO_SERVICE + "/PurchaseOrder"


def test_cancel_sets_deletion_indicator_never_delete(sap):
    sap.cancel_order("4500000103", "no longer needed")
    patches = [r for r in sap.fake.calls if r.method == "PATCH"]
    assert len(patches) == 2
    assert all(json.loads(p.content) == {"PurchasingDocumentDeletionCode": "L"} for p in patches)
    assert not [r for r in sap.fake.calls if r.method == "DELETE"]


def test_amend_patches_items_with_etag(sap):
    sap.amend_order("4500000101", [Line("TG11", Decimal(25), Decimal(45.5))], "")
    p = [r for r in sap.fake.calls if r.method == "PATCH"][0]
    assert p.headers["If-Match"] == 'W/"SADL-4500000101-10"'
    assert json.loads(p.content)["OrderQuantity"] == "25"


def test_approval_is_flexible_workflow_not_this_api(sap):
    with pytest.raises(ErpError, match="flexible workflow"):
        sap.approve_order("4500000102")
    with pytest.raises(ErpError):
        sap.set_status("4500000102", Status.REJECTED, "x")
    assert sap.set_status("4500000102", Status.PENDING, "").id == "4500000102"


def test_hard_limits(sap):
    with pytest.raises(HardLimitError):
        sap.c.request("DELETE", f"{PO_SERVICE}/PurchaseOrder('4500000101')")
    with pytest.raises(HardLimitError):
        sap.c.request("GET", f"{BP_SERVICE}/A_SupplierBank")
    with pytest.raises(HardLimitError):
        sap.c.request("POST", f"{PO_SERVICE}/PurchaseOrder", body={"PaymentTerms": "0001"})
    with pytest.raises(HardLimitError):
        sap.c.request("POST", f"{BP_SERVICE}/A_Supplier", body={"SupplierName": "x"})


def test_guardrails_run_on_sap_too(sap):
    svc = Service(sap, load_policy())
    args = {"supplier": "17300001", "cost_centre": "10101101",
            "lines": [{"item_code": "TG11", "qty": 400, "rate": 46}]}
    p = svc.preview("create_purchase_order", args)
    assert {r.check: r.level for r in p.checks}["budget"] == "block"
    # SAP doesn't model purchase requests yet, so $5,000 or more can't show one.
    assert {r.check: r.level for r in p.checks}["connector"] == "block"
    small = {**args, "lines": [{"item_code": "TG11", "qty": 4, "rate": 46}]}
    p = svc.preview("create_purchase_order", small)
    out = svc.execute("create_purchase_order", small, p.code)
    assert out.outcome == "written" and out.target == "4500000201"
    assert sap.read_audit(limit=1)[0].outcome == "written"


def test_from_env_needs_key_for_sandbox(monkeypatch):
    monkeypatch.delenv("SAP_API_KEY", raising=False)
    monkeypatch.delenv("SAP_BASE_URL", raising=False)
    with pytest.raises(SystemExit):
        SapS4.from_env()
