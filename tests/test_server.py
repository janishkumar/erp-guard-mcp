"""The MCP layer: tools register with the right annotations and render results."""

from __future__ import annotations

import asyncio

from conftest import as_user, po_args

from erp_guard import server
from erp_guard.connectors.base import ErpError


def _tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


READS = {"preview", "search_suppliers", "get_purchase_order", "list_purchase_orders",
         "budget_status", "audit_log", "whoami",
         "describe_doctype", "find_documents", "get_document", "draft_from"}
WRITES = {"create_purchase_order", "onboard_supplier", "submit_for_approval",
          "approve_purchase_order", "reject_purchase_order", "cancel_purchase_order",
          "amend_purchase_order", "raise_purchase_request", "request_quotes",
          "enter_supplier_quote", "receive_goods", "return_goods",
          "enter_supplier_invoice", "enter_debit_note", "hold_invoice", "release_invoice",
          "pay_supplier", "close_purchase_order", "create_document", "update_document",
          "submit_document", "workflow_action", "cancel_document", "amend_document"}
GENERIC_WRITES = {"create_document", "update_document", "submit_document", "workflow_action",
                  "cancel_document", "amend_document"}


def test_tools_listed():
    tools = _tools()
    assert set(tools) == READS | WRITES
    for n in READS:
        assert tools[n].annotations.read_only_hint is True
    for n in WRITES:
        assert tools[n].annotations.read_only_hint is False
        props = list(tools[n].input_schema["properties"])
        assert props[-1] == "confirmation_code"  # last, and optional
        assert "confirmation_code" not in tools[n].input_schema.get("required", [])
    for n in ("cancel_purchase_order", "cancel_document"):
        assert tools[n].annotations.destructive_hint is True
    for n in WRITES - {"cancel_purchase_order", "cancel_document"}:
        assert tools[n].annotations.destructive_hint is False
    for n in WRITES:  # identity is never an argument
        props = set(tools[n].input_schema["properties"])
        assert not props & {"user", "actor", "as_user", "approver", "requester"}
    for n in GENERIC_WRITES | {"describe_doctype"}:  # point the model at pack tools first
        assert "prefer" in tools[n].description.lower()


def test_generic_read_tools(policy):
    from datetime import date

    from fake_erpnext import FakeErpNext, connector

    from erp_guard.service import Service
    fake = FakeErpNext("requester@example.com")
    fake.roles["requester@example.com"] = ["Purchase User", "Projects User"]
    fake.add({"doctype": "Task", "name": "TASK-1", "subject": "Check meter", "status": "Open",
              "owner": "requester@example.com", "docstatus": 0})
    fake.add({"doctype": "Purchase Order", "name": "PO-1", "supplier": "HERC RENTALS INC",
              "docstatus": 1, "items": [{"item_code": "SALT-TON", "qty": 2, "rate": 10}]})
    server.set_service(Service(connector(fake), policy, today=lambda: date(2026, 10, 29)))
    try:
        d = server.describe_doctype("Task")
        assert "**Task**" in d and "later phase" in d and "| subject | Data | yes |" in d
        assert "create" in d and "submit" not in d.split("You may:")[1].split("\n")[0]
        po = server.describe_doctype("Purchase Order")
        assert "purchase-to-pay" in po and "create_purchase_order" in po
        assert "ledger" in server.describe_doctype("Journal Entry")
        assert "only when linked to a Supplier" in server.describe_doctype("Address")
        assert "system administration" in server.describe_doctype("Server Script")
        assert "ERP error" in server.describe_doctype("No Such Type")
        rows = server.find_documents("Task", [["status", "=", "Open"]], ["name", "subject"])
        assert "| TASK-1 | Check meter |" in rows
        assert "No Task matches" in server.find_documents("Task", [["status", "=", "Closed"]])
        doc = server.get_document("Task", "TASK-1")
        assert "Check meter" in doc and "data, not instructions" in doc
        assert "No Task 'NOPE'" in server.get_document("Task", "NOPE")
        draft = server.draft_from("Purchase Order", "PO-1", "Purchase Receipt")
        assert "SALT-TON" in draft and "create_document" in draft
        assert "__islocal" not in draft and "parenttype" not in draft and "docstatus" not in draft
        assert "no mapper" in server.draft_from("Purchase Order", "PO-1", "Journal Entry")
        assert "Projects User" in server.whoami()
    finally:
        server.set_service(None)


def test_generic_read_tools_need_erpnext(svc):
    server.set_service(svc)
    try:
        for text in (server.describe_doctype("Task"), server.find_documents("Task"),
                     server.get_document("Task", "T"),
                     server.draft_from("Purchase Order", "P", "Purchase Receipt")):
            assert "need ERPNext" in text
    finally:
        server.set_service(None)


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
