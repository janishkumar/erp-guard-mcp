"""No path to payments, bank details or deletion, at any layer."""

from __future__ import annotations

import asyncio
import inspect
import json

import httpx
import pytest

from erp_guard import server
from erp_guard.connectors import base
from erp_guard.connectors.erpnext import ErpNext, FrappeClient, HardLimitError
from erp_guard.connectors.memory import MemoryErp

FORBIDDEN = ("pay", "bank", "delete", "remove", "iban", "transfer")


def test_connector_protocol_has_no_forbidden_methods():
    names = [n for n, _ in inspect.getmembers(base.ErpConnector) if not n.startswith("_")]
    assert not [n for n in names if any(f in n.lower() for f in FORBIDDEN)]
    for impl in (MemoryErp, ErpNext):
        public = [n for n, _ in inspect.getmembers(impl, inspect.isfunction)
                  if not n.startswith("_")]
        assert not [n for n in public if any(f in n.lower() for f in FORBIDDEN)], impl


def test_no_tool_can_pay_bank_or_delete():
    tools = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert len(tools) == 14
    assert not [n for n in tools if any(f in n.lower() for f in FORBIDDEN)]


def _client(seen, **kw):
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"data": {}, "message": "ok"})
    return FrappeClient("http://erp.local", "k", "s", transport=httpx.MockTransport(handler),
                        **kw)


@pytest.mark.parametrize("verb,path,body", [
    ("DELETE", "/api/resource/Supplier/ACME", None),
    ("PATCH", "/api/resource/Supplier/ACME", {}),
    ("GET", "/api/resource/Payment Entry", None),
    ("POST", "/api/resource/Bank Account", {"account_name": "x"}),
    ("POST", "/api/resource/Payment Request", {}),
    ("GET", "/api/method/frappe.client.delete", None),
    ("POST", "/api/method/frappe.client.cancel", {"doctype": "Payment Request", "name": "x"}),
    ("PUT", "/api/resource/Supplier/ACME", {"default_bank_account": "x"}),
    ("POST", "/api/resource/Supplier", {"supplier_name": "x",
                                        "accounts": [{"iban": "GB29NWBK60161331926819"}]}),
    ("GET", "/files/private/secret.pdf", None),
])
def test_erpnext_client_refuses(verb, path, body):
    seen = []
    with pytest.raises(HardLimitError):
        _client(seen).request(verb, path, body=body)
    assert not seen  # nothing left the process


def test_erpnext_client_allows_the_needed_calls():
    seen = []
    c = _client(seen)
    c.request("GET", "/api/resource/Purchase%20Order", params={"fields": json.dumps(["name"])})
    c.request("POST", "/api/method/frappe.client.cancel",
              body={"doctype": "Purchase Order", "name": "PO-1"})
    assert len(seen) == 2


@pytest.mark.parametrize("doctype", ["Supplier", "Purchase Order", "Task", "User", "GL Entry"])
def test_delete_refused_any_doctype(doctype):
    seen = []
    c = _client(seen, writable=lambda d: True)
    with pytest.raises(HardLimitError):
        c.request("DELETE", f"/api/resource/{doctype}/X-1")
    for method in ("frappe.client.delete", "frappe.desk.reportview.delete_items"):
        with pytest.raises(HardLimitError):
            c.request("POST", f"/api/method/{method}", body={"doctype": doctype, "name": "X-1"})
    assert not seen


def test_write_unwritable_doctype_refused():
    seen = []
    c = _client(seen, writable=lambda d: False)
    for verb in ("POST", "PUT"):
        with pytest.raises(HardLimitError, match="not writable"):
            c.request(verb, "/api/resource/Task", body={"subject": "x"})
    with pytest.raises(HardLimitError):
        c.request("POST", "/api/method/frappe.client.cancel", body={"doctype": "Task", "name": "T"})
    with pytest.raises(HardLimitError):
        c.request("POST", "/api/method/erpnext.buying.doctype.purchase_order.purchase_order."
                          "update_status", body={"status": "Closed", "name": "PO-1"})
    assert not seen


@pytest.mark.parametrize("method", ["frappe.client.delete", "frappe.desk.reportview.delete_items",
                                    "frappe.client.set_value", "frappe.client.insert_many",
                                    "run_doc_method", "frappe.client.rename_doc"])
def test_unlisted_method_refused(method):
    seen = []
    with pytest.raises(HardLimitError, match="method not allowed"):
        _client(seen, writable=lambda d: True).request(
            "POST", f"/api/method/{method}", body={"doctype": "Task", "name": "T"})
    assert not seen


def test_insert_checks_doctype_arg():
    seen = []
    c = _client(seen, writable=lambda d: d != "User")
    for method in ("frappe.client.insert", "frappe.client.save", "frappe.client.submit",
                   "frappe.model.workflow.apply_workflow"):
        with pytest.raises(HardLimitError, match="User"):
            c.request("POST", f"/api/method/{method}", body={"doc": {"doctype": "User"}})
    # a child row inserted on its own still names its parent, which must be writable too
    with pytest.raises(HardLimitError, match="User"):
        c.request("POST", "/api/method/frappe.client.insert", body={"doc": {
            "doctype": "Has Role", "parenttype": "User", "parent": "a@example.com",
            "role": "System Manager"}})
    # a document sent as a JSON string can't be inspected, so it's refused
    with pytest.raises(HardLimitError):
        c.request("POST", "/api/method/frappe.client.insert",
                  body={"doc": json.dumps({"doctype": "User"})})
    assert not seen
    c.request("POST", "/api/method/frappe.client.insert", body={"doc": {"doctype": "Task"}})
    assert len(seen) == 1


def test_write_methods_are_post_and_getdoctype_is_get():
    seen = []
    c = _client(seen, writable=lambda d: True)
    with pytest.raises(HardLimitError):
        c.request("GET", "/api/method/frappe.client.insert", params={"doc": "{}"})
    with pytest.raises(HardLimitError):
        c.request("POST", "/api/method/frappe.desk.form.load.getdoctype", body={"doctype": "Task"})
    c.request("GET", "/api/method/frappe.desk.form.load.getdoctype", params={"doctype": "Task"})
    assert len(seen) == 1


def test_doc_methods_only_the_allowlisted_ones():
    seen = []
    c = _client(seen, writable=lambda d: True)
    path = "/api/method/frappe.handler.run_doc_method"
    for body in ({"dt": "Purchase Invoice", "dn": "PI-1", "method": "make_bank_entry"},
                 {"dt": "Purchase Order", "dn": "PO-1", "method": "block_invoice"},
                 {"docs": json.dumps({"doctype": "Purchase Invoice"}), "method": "block_invoice"},
                 {"dt": "Purchase Invoice", "dn": "PI-1", "method": "block_invoice",
                  "args": json.dumps({"hold_comment": "x"})}):
        with pytest.raises(HardLimitError):
            c.request("POST", path, body=body)
    with pytest.raises(HardLimitError, match="not writable"):
        _client(seen, writable=lambda d: False).request("POST", path, body={
            "dt": "Purchase Invoice", "dn": "PI-1", "method": "unblock_invoice"})
    assert not seen
    c.request("POST", path, body={"dt": "Purchase Invoice", "dn": "PI-1", "method": "block_invoice",
                                  "args": {"hold_comment": "price query"}})
    assert len(seen) == 1


BANK = "Demo Operating Bank - LDU"


@pytest.mark.parametrize("path,body", [
    ("/api/resource/Payment Entry", {"payment_type": "Pay", "paid_from": "Petty Cash - LDU"}),
    ("/api/resource/Payment Entry", {"bank_account": "Other Bank - LDU"}),
    ("/api/resource/Payment Entry", {"paid_from": BANK, "party_bank_account": "SUP-1 - Chase"}),
    ("/api/method/frappe.client.insert", {"doc": {"doctype": "Payment Entry",
                                                  "paid_from": "Petty Cash - LDU"}}),
    ("/api/method/frappe.client.save", {"doc": {"doctype": "Payment Entry", "paid_from": BANK,
                                                "bank_account_no": "123456789"}}),
    # the bank account is only allowed on a payment entry, nowhere else
    ("/api/resource/Supplier", {"supplier_name": "x", "default_bank_account": BANK}),
    ("/api/method/frappe.client.insert", {"doc": {"doctype": "Task", "subject": "x",
                                                  "bank_account": BANK}}),
])
def test_bank_details_refused_except_the_policy_account(path, body):
    seen = []
    c = _client(seen, writable=lambda d: True, allowed_bank_values={BANK})
    with pytest.raises(HardLimitError, match="bank"):
        c.request("POST", path, body=body)
    assert not seen


def test_policy_bank_account_and_empty_bank_fields_pass():
    seen = []
    c = _client(seen, writable=lambda d: True, allowed_bank_values={BANK})
    c.request("POST", "/api/resource/Payment Entry", body={
        "payment_type": "Pay", "paid_from": BANK, "bank_account": BANK,
        "party_bank_account": None, "mode_of_payment": "Wire Transfer",
        "references": [{"reference_doctype": "Purchase Invoice", "allocated_amount": 10}]})
    c.request("POST", "/api/method/frappe.client.insert", body={"doc": {
        "doctype": "Purchase Invoice", "cash_bank_account": None,
        "payment_schedule": [{"payment_term": None, "payment_amount": 100.0}]}})
    assert len(seen) == 2
