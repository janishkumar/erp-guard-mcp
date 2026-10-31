"""ERPNext generic document operations against metadata recorded from v16.52.0
(scripts/record_erpnext_fixtures.py)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from erp_guard.connectors.base import ErpError
from erp_guard.connectors.erpnext import MAPPERS, ErpNext, FrappeClient, HardLimitError

FIX = Path(__file__).parent / "fixtures" / "erpnext"
PI = "erpnext.buying.doctype.purchase_order.purchase_order"

CANCELLED_PO = {
    "doctype": "Purchase Order", "name": "PUR-ORD-2026-00007", "docstatus": 2,
    "status": "Cancelled", "workflow_state": "Cancelled", "owner": "requester@example.com",
    "creation": "2026-10-01 10:00:00", "modified": "2026-10-02 10:00:00",
    "modified_by": "director@example.com", "supplier": "HERC RENTALS INC", "_user_tags": None,
    "items": [{"doctype": "Purchase Order Item", "name": "row1", "parent": "PUR-ORD-2026-00007",
               "parenttype": "Purchase Order", "parentfield": "items", "idx": 1, "docstatus": 2,
               "item_code": "LAPTOP-14", "qty": 2, "rate": 1200}],
}


class FakeErpNext:
    """Records requests; answers metadata from the recorded fixtures."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.docs = {("Purchase Order", CANCELLED_PO["name"]): CANCELLED_PO,
                     ("Task", "TASK-1"): {"doctype": "Task", "name": "TASK-1", "docstatus": 0,
                                          "subject": "Check meter", "modified": "2026-10-03"}}

    def body(self, i: int = -1) -> dict:
        return json.loads(self.calls[i].content)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        method = req.url.path.removeprefix("/api/method/")
        if method == "frappe.desk.form.load.getdoctype":
            f = FIX / "meta" / f"{req.url.params['doctype']}.json"
            return httpx.Response(200, content=f.read_bytes()) if f.exists() else \
                httpx.Response(404, json={"exc_type": "DoesNotExistError"})
        if method == "frappe.client.get_list" and json.loads(req.content)["doctype"] == "DocType":
            return httpx.Response(200, content=(FIX / "doctypes.json").read_bytes())
        if method == "frappe.client.get":
            d = self.docs.get((req.url.params["doctype"], req.url.params["name"]))
            return httpx.Response(200, json={"message": d}) if d else \
                httpx.Response(404, json={"exc_type": "DoesNotExistError"})
        if method in MAPPERS.values():
            return httpx.Response(200, json={"message": {"doctype": "Purchase Receipt",
                                                         "docstatus": 0, "items": []}})
        if method == "frappe.model.workflow.get_transitions":
            return httpx.Response(200, json={"message": [
                {"action": "Approve", "next_state": "Approved"},
                {"action": "Reject", "next_state": "Rejected"}]})
        if method in ("frappe.client.insert", "frappe.client.save", "frappe.client.submit"):
            return httpx.Response(200, json={"message": {**self.body()["doc"], "name": "NEW-1"}})
        return httpx.Response(200, json={"message": {"ok": method}})


@pytest.fixture
def erp():
    fake = FakeErpNext()
    client = FrappeClient("http://erp.local", "k", "s", transport=httpx.MockTransport(fake),
                          writable=lambda d: d in {"Purchase Order", "Task", "Purchase Invoice"})
    e = ErpNext(client, "Lakeside Demo Utilities")
    e.fake = fake
    return e


def test_get_meta_from_fixture(erp):
    meta = erp.get_meta("Purchase Invoice")
    assert meta["name"] == "Purchase Invoice" and meta["is_submittable"] == 1
    fields = {f["fieldname"]: f for f in meta["fields"]}
    assert fields["supplier"]["fieldtype"] == "Link" and fields["supplier"]["options"] == "Supplier"
    assert fields["items"]["options"] == "Purchase Invoice Item"
    assert "Purchase Invoice Item" in meta["child_doctypes"]
    assert erp.get_meta("Task")["is_submittable"] == 0
    erp.get_meta("Purchase Invoice")
    assert len(erp.fake.calls) == 2  # cached per instance
    assert erp.fake.calls[0].method == "GET"
    assert {f["fieldname"] for f in erp.get_meta("Supplier")["fields"]} >= {
        "erp_guard_onboarded_by", "erp_guard_notes"}  # the seed's custom fields


def test_get_meta_unknown_doctype(erp):
    with pytest.raises(ErpError, match="404"):
        erp.get_meta("No Such Type")


def test_list_doctypes_from_fixture(erp):
    rows = erp.list_doctypes()
    by_name = {r["name"]: r for r in rows}
    assert set(rows[0]) == {"name", "module", "issingle", "istable", "is_submittable"}
    assert not [r for r in rows if r["istable"]]
    assert by_name["Payment Entry"]["is_submittable"] == 1
    assert by_name["Payment Entry"]["module"] == "Accounts"
    assert by_name["Buying Settings"]["issingle"] == 1
    body = erp.fake.body()
    assert body["doctype"] == "DocType" and body["filters"] == [["istable", "=", 0]]


def test_get_doc_and_find(erp):
    assert erp.get_doc("Task", "TASK-1")["subject"] == "Check meter"
    assert erp.get_doc("Task", "TASK-404") is None
    erp.find("Payment Entry", [["docstatus", "=", 1]], ["name", "paid_amount"],
             "posting_date desc", 5)
    assert erp.fake.calls[-1].url.path == "/api/method/frappe.client.get_list"
    assert erp.fake.body() == {"doctype": "Payment Entry", "fields": ["name", "paid_amount"],
                               "filters": [["docstatus", "=", 1]],
                               "order_by": "posting_date desc", "limit_page_length": 5}


def test_map_doc_calls_allowlisted_mapper(erp):
    doc = erp.map_doc("Purchase Order", "PUR-ORD-2026-00001", "Purchase Receipt")
    assert doc["doctype"] == "Purchase Receipt" and doc["docstatus"] == 0
    req = erp.fake.calls[-1]
    assert req.method == "POST"
    assert req.url.path == f"/api/method/{PI}.make_purchase_receipt"
    assert erp.fake.body() == {"source_name": "PUR-ORD-2026-00001"}


def test_map_doc_payment_entry_args(erp):
    erp.map_doc("Purchase Invoice", "ACC-PINV-2026-00001", "Payment Entry")
    assert erp.fake.calls[-1].url.path.endswith("payment_entry.get_payment_entry")
    assert erp.fake.body() == {"dt": "Purchase Invoice", "dn": "ACC-PINV-2026-00001"}


def test_map_doc_unknown_pair_refused(erp):
    with pytest.raises(HardLimitError, match="no mapper"):
        erp.map_doc("Purchase Invoice", "PI-1", "Journal Entry")
    assert not erp.fake.calls


def test_apply_workflow_payload(erp):
    erp.apply_workflow("Purchase Order", "PUR-ORD-2026-00007", "Approve")
    assert erp.fake.calls[-1].url.path == "/api/method/frappe.model.workflow.apply_workflow"
    body = erp.fake.body()
    assert body["doc"] == {"doctype": "Purchase Order", "name": "PUR-ORD-2026-00007"}
    assert body["action"] == "Approve"
    assert erp.transitions("Purchase Order", "PUR-ORD-2026-00007") == ["Approve", "Reject"]


def test_insert_save_cancel(erp):
    assert erp.insert({"doctype": "Task", "subject": "x"})["name"] == "NEW-1"
    assert erp.fake.body() == {"doc": {"doctype": "Task", "subject": "x"}}
    erp.save({"doctype": "Task", "name": "TASK-1", "subject": "y"})
    assert erp.fake.calls[-1].url.path.endswith("frappe.client.save")
    erp.cancel("Purchase Order", "PUR-ORD-2026-00001")
    assert erp.fake.body() == {"doctype": "Purchase Order", "name": "PUR-ORD-2026-00001"}
    with pytest.raises(HardLimitError):
        erp.insert({"doctype": "User", "email": "x@example.com"})


def test_submit_sends_the_current_document(erp):
    erp.submit("Task", "TASK-1")
    assert erp.fake.calls[-1].url.path.endswith("frappe.client.submit")
    assert erp.fake.body()["doc"]["subject"] == "Check meter"  # not just doctype + name
    with pytest.raises(ErpError, match="No Task"):
        erp.submit("Task", "TASK-404")


def test_amend_copies_the_cancelled_document(erp):
    erp.amend("Purchase Order", "PUR-ORD-2026-00007", {"supplier": "CANON U.S.A., INC."})
    doc = erp.fake.body()["doc"]
    assert erp.fake.calls[-1].url.path.endswith("frappe.client.insert")
    assert doc["amended_from"] == "PUR-ORD-2026-00007" and doc["supplier"] == "CANON U.S.A., INC."
    for key in ("name", "docstatus", "status", "workflow_state", "owner", "creation", "modified",
                "_user_tags"):
        assert key not in doc
    assert doc["items"] == [{"doctype": "Purchase Order Item", "idx": 1,
                             "item_code": "LAPTOP-14", "qty": 2, "rate": 1200}]
    with pytest.raises(ErpError, match="cancelled"):
        erp.amend("Task", "TASK-1", {})


def test_call_uses_get_for_get_only_methods(erp):
    erp.call("frappe.desk.form.load.getdoctype", doctype="Task")
    assert erp.fake.calls[-1].method == "GET"
    erp.call("frappe.handler.run_doc_method", dt="Purchase Invoice", dn="PI-1",
             method="block_invoice", args={"hold_comment": "price query"})
    assert erp.fake.calls[-1].method == "POST"
    with pytest.raises(HardLimitError):
        erp.call("frappe.client.set_value", doctype="Task", name="TASK-1", fieldname="status")


def test_from_env_takes_the_bank_account_from_policy(monkeypatch):
    monkeypatch.setenv("ERPNEXT_API_KEY", "k")
    monkeypatch.setenv("ERPNEXT_API_SECRET", "s")
    e = ErpNext.from_env()
    assert e.c.allowed_bank_values == frozenset({"Demo Operating Bank - LDU"})
