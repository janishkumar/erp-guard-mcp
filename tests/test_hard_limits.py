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


def _client(seen):
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"data": {}, "message": "ok"})
    return FrappeClient("http://erp.local", "k", "s", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("verb,path,body", [
    ("DELETE", "/api/resource/Supplier/ACME", None),
    ("PATCH", "/api/resource/Supplier/ACME", {}),
    ("GET", "/api/resource/Payment Entry", None),
    ("POST", "/api/resource/Bank Account", {"account_name": "x"}),
    ("POST", "/api/resource/Payment Request", {}),
    ("GET", "/api/method/frappe.client.delete", None),
    ("POST", "/api/method/frappe.client.cancel", {"doctype": "Supplier", "name": "x"}),
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
