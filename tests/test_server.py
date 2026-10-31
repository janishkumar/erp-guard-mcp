"""The MCP layer: tools register with the right annotations and render results."""

from __future__ import annotations

import asyncio

from conftest import as_user, po_args

from po_guard import server
from po_guard.connectors.base import ErpError


def _tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_tool_set_and_annotations():
    tools = _tools()
    reads = {"preview", "search_suppliers", "get_purchase_order", "list_purchase_orders",
             "budget_status", "audit_log", "whoami"}
    writes = {"create_purchase_order", "onboard_supplier", "submit_for_approval",
              "approve_purchase_order", "reject_purchase_order", "cancel_purchase_order",
              "amend_purchase_order"}
    assert set(tools) == reads | writes
    for n in reads:
        assert tools[n].annotations.read_only_hint is True
    for n in writes:
        assert tools[n].annotations.read_only_hint is False
    assert tools["cancel_purchase_order"].annotations.destructive_hint is True
    for n in writes:  # identity is never an argument
        props = set(tools[n].input_schema["properties"])
        assert not props & {"user", "actor", "as_user", "approver", "requester"}


def test_preview_then_write_through_the_tools(svc, erp, policy):
    server.set_service(svc)
    try:
        text = server.preview("create_purchase_order", po_args(rate=2000))
        assert "Confirmation code: `" in text and "nothing has been written" in text
        code = text.split("Confirmation code: `")[1].split("`")[0]
        out = server.create_purchase_order(**po_args(rate=2000), confirmation_code=code)
        assert out.startswith("**Done:**")
        po = erp.list_orders(supplier_id="SUP-HERC")[0].id
        assert "Draft" in server.get_purchase_order(po)
        assert po in server.list_purchase_orders(supplier="SUP-HERC")
        assert "remaining" in server.budget_status("Fleet")
        assert "HERC" in server.search_suppliers("herc rental")
        assert "written" in server.audit_log(po)
        assert "cannot approve" in server.whoami()
        assert "No purchase order" in server.get_purchase_order("NOPE")
        assert server.create_purchase_order(**po_args(), confirmation_code=None) \
            .startswith("**Refused:**")
        assert "Invalid arguments" in server.preview("delete_supplier", {})
        server.set_service(as_user(erp, policy, "manager@example.com"))
        assert "can approve up to $50,000" in server.whoami()
    finally:
        server.set_service(None)


def test_blocked_preview_says_so(svc):
    server.set_service(svc)
    try:
        text = server.preview("create_purchase_order", po_args(supplier="SUP-HOLD"))
        assert "No confirmation code" in text and "BLOCK" in text
    finally:
        server.set_service(None)


def test_erp_errors_are_reported_not_raised(svc, monkeypatch):
    server.set_service(svc)

    def boom(*a, **k):
        raise ErpError("down")
    monkeypatch.setattr(svc, "preview", boom)
    try:
        assert server.preview("create_purchase_order", po_args()) == "ERP error: down"
    finally:
        server.set_service(None)
