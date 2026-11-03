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


def _forbidden(names, allowed=frozenset()) -> list[str]:
    return [n for n in names if n not in allowed and any(f in n.lower() for f in FORBIDDEN)]


def _public(cls) -> set[str]:
    return {n for n, _ in inspect.getmembers(cls) if not n.startswith("_")}


def test_connector_protocol_has_no_forbidden_methods():
    """v1's protocol names no payment at all. Purchase to pay adds exactly the payment
    methods in PAYMENT_METHODS (draft a payment against invoices, read payments); nothing
    names bank details or deletion, in the protocols or their implementations."""
    assert not _forbidden(_public(base.ErpConnector))
    assert {"create_payment", "get_payment", "list_payments"} == base.PAYMENT_METHODS
    assert _public(base.P2PConnector) >= base.PAYMENT_METHODS
    assert not _forbidden(_public(base.P2PConnector), base.PAYMENT_METHODS)
    for impl in (MemoryErp, ErpNext):
        public = [n for n, _ in inspect.getmembers(impl, inspect.isfunction)
                  if not n.startswith("_")]
        assert not _forbidden(public, base.PAYMENT_METHODS), impl


def test_no_tool_can_pay_bank_or_delete():
    """Payments are in scope only through pay_supplier (a draft paying approved invoices,
    through the pack's checks and the payment workflow); no tool names bank details or
    deletion."""
    tools = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert len(tools) == 31
    assert _forbidden(tools) == ["pay_supplier"]


def test_payment_type_party_and_paid_to_are_never_the_callers():
    """A payment is always a Pay to a Supplier, into the supplier's payable account as
    ERPNext maps it from the invoice: no internal transfer, no other party type, no
    caller-chosen paid_to, through any layer."""
    from fake_erpnext import FakeErpNext, connector

    from erp_guard.packs.p2p.models import Allocation
    from erp_guard.service import InvalidArgs, Service
    # The connector methods take no such argument...
    for impl in (base.P2PConnector, MemoryErp, ErpNext):
        params = set(inspect.signature(impl.create_payment).parameters) - {"self"}
        assert params == {"supplier_id", "allocations", "paid_from", "remarks"}, impl
    # ...and whatever ERPNext's mapper drafts, the entry sent is a Pay to the Supplier,
    # paid_to as mapped.
    bank, pi = "Demo Operating Bank - LDU", "ACC-PINV-1"
    fake = FakeErpNext("treasury@example.com")
    fake.mapped["get_payment_entry"] = {
        "doctype": "Payment Entry", "payment_type": "Internal Transfer",
        "party_type": "Employee", "party": "HR-EMP-1", "paid_from": bank,
        "posting_date": "2026-11-02",
        "paid_to": "Creditors - LDU", "references": [
            {"doctype": "Payment Entry Reference", "reference_doctype": "Purchase Invoice",
             "reference_name": pi, "allocated_amount": 10.0}]}
    erp = connector(fake, writable=lambda d: d == "Payment Entry",
                    allowed_bank_values={bank})
    erp.create_payment("TRANE U.S. INC", [Allocation(pi, 10)], bank)
    sent = next(b["doc"] for b in fake.method_calls("frappe.client.insert"))
    assert (sent["payment_type"], sent["party_type"], sent["party"], sent["paid_to"]) == (
        "Pay", "Supplier", "TRANE U.S. INC", "Creditors - LDU")
    # pay_supplier refuses them as arguments, and no connector edits them on a draft.
    from erp_guard.policy import load_policy
    svc = Service(MemoryErp(user="treasury@example.com"), load_policy())
    for extra in ({"payment_type": "Internal Transfer"}, {"paid_to": "Cash - LDU"},
                  {"party_type": "Employee"}, {"paid_from": "Cash - LDU"}):
        with pytest.raises(InvalidArgs, match="Unknown argument"):
            svc.preview("pay_supplier", {"supplier": "S", "invoices": [], **extra})
    for field in ("payment_type", "paid_to", "party_type", "party", "paid_from"):
        for c in (MemoryErp(), erp):
            with pytest.raises(base.ErpError, match=field):
                c.update_draft("Payment Entry", "PE-1", {field: "x"})


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


@pytest.mark.parametrize("path,body,refused", [
    ("/api/resource/Payment Entry", {"payment_type": "Pay", "paid_from": "Petty Cash - LDU"},
     "['paid_from']"),
    ("/api/resource/Payment Entry", {"bank_account": "Other Bank - LDU"}, "['bank_account']"),
    ("/api/resource/Payment Entry", {"paid_from": BANK, "party_bank_account": "SUP-1 - Chase"},
     "['party_bank_account']"),
    ("/api/method/frappe.client.insert", {"doc": {"doctype": "Payment Entry",
                                                  "paid_from": "Petty Cash - LDU"}},
     "['paid_from']"),
    ("/api/method/frappe.client.save", {"doc": {"doctype": "Payment Entry", "paid_from": BANK,
                                                "bank_account_no": "123456789"}},
     "['bank_account_no']"),
    # the bank account is only allowed on a payment entry, nowhere else
    ("/api/resource/Supplier", {"supplier_name": "x", "default_bank_account": BANK},
     "['default_bank_account']"),
    ("/api/method/frappe.client.insert", {"doc": {"doctype": "Task", "subject": "x",
                                                  "bank_account": BANK}}, "['bank_account']"),
])
def test_bank_details_refused_except_the_policy_account(path, body, refused):
    seen = []
    c = _client(seen, writable=lambda d: True, allowed_bank_values={BANK})
    with pytest.raises(HardLimitError) as exc:
        c.request("POST", path, body=body)
    assert str(exc.value) == f"bank details are never written: {refused}"
    assert not seen


@pytest.mark.parametrize("falsy", [None, ""])
def test_bank_check_keeps_path_doctype_when_body_doctype_falsy(falsy):
    c = _client([], allowed_bank_values={BANK})
    with pytest.raises(HardLimitError) as exc:
        c._check_bank({"doctype": falsy, "paid_from": "Other Bank - LDU"}, "Payment Entry")
    assert str(exc.value) == "bank details are never written: ['paid_from']"


# ERPNext writes the path's doctype whatever the body says, so a body naming any other
# doctype (None and "" included) could dodge the payment entry rule; it's refused.
@pytest.mark.parametrize("claimed", ["Task", None, ""])
def test_resource_body_doctype_other_than_path_refused(claimed):
    seen = []
    c = _client(seen, writable=lambda d: True, allowed_bank_values={BANK})
    with pytest.raises(HardLimitError) as exc:
        c.request("POST", "/api/resource/Payment Entry",
                  body={"doctype": claimed, "paid_from": "Other Bank - LDU"})
    assert str(exc.value) == f"body doctype {claimed!r} != path Payment Entry"
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


def test_resource_body_doctype_must_match_path():
    seen = []
    c = _client(seen, writable=lambda d: True, allowed_bank_values={BANK})
    for verb in ("POST", "PUT"):
        with pytest.raises(HardLimitError, match="body doctype 'Task' != path Payment Entry"):
            c.request(verb, "/api/resource/Payment Entry/PE-1", body={"doctype": "Task"})
    assert not seen
    c.request("POST", "/api/resource/Payment Entry", body={"doctype": "Payment Entry",
                                                          "paid_from": BANK})
    assert len(seen) == 1


@pytest.mark.parametrize("verb,path,params", [
    ("PUT", "/api/resource/Supplier/ACME", {"default_bank_account": "x"}),
    ("POST", "/api/resource/Payment Entry", {"paid_from": "Petty Cash - LDU"}),
    ("POST", "/api/method/frappe.client.insert",
     {"doc": json.dumps({"doctype": "User", "email": "x@example.com"})}),
    ("POST", "/api/method/frappe.client.cancel", {"doctype": "Payment Request", "name": "x"}),
])
def test_write_arguments_in_query_string_refused(verb, path, params):
    seen = []
    c = _client(seen, writable=lambda d: d != "User" and d != "Payment Request",
                allowed_bank_values={BANK})
    with pytest.raises(HardLimitError, match="body only"):
        c.request(verb, path, params=params)
    assert not seen


def test_audit_doctype_is_create_only():
    """The audit trail is append-only, whatever the write predicate says."""
    seen = []
    audit = "/api/resource/ERP%20Guard%20Audit"
    for writable in (lambda d: True, lambda d: d == "Some Row"):
        c = _client(seen, writable=writable)
        for verb, path, body in [
            ("PUT", f"{audit}/AUD-1", {"outcome": "written"}),
            ("POST", f"{audit}/AUD-1", {"outcome": "written"}),
        ]:
            with pytest.raises(HardLimitError, match="ERP Guard Audit"):
                c.request(verb, path, body=body)
        for method, body in [
            ("frappe.client.save", {"doc": {"doctype": "ERP Guard Audit", "name": "AUD-1"}}),
            ("frappe.client.submit", {"doc": {"doctype": "ERP Guard Audit", "name": "AUD-1"}}),
            ("frappe.client.cancel", {"doctype": "ERP Guard Audit", "name": "AUD-1"}),
            ("frappe.model.workflow.apply_workflow",
             {"doc": {"doctype": "ERP Guard Audit", "name": "AUD-1"}, "action": "x"}),
            ("frappe.client.insert", {"doc": {"doctype": "Some Row", "parent": "AUD-1",
                                              "parenttype": "ERP Guard Audit"}}),
        ]:
            with pytest.raises(HardLimitError, match="ERP Guard Audit"):
                c.request("POST", f"/api/method/{method}", body=body)
    assert not seen
    c = _client(seen, writable=lambda d: False)
    c.request("POST", audit, body={"action": "x", "outcome": "refused"})
    c.request("POST", "/api/method/frappe.client.insert",
              body={"doc": {"doctype": "ERP Guard Audit", "action": "x"}})
    assert len(seen) == 2


def test_child_rows_never_written_on_their_own():
    """A child row written by itself skips its parent's checks: refused whatever the write
    predicate says, through every write path."""
    seen = []
    c = _client(seen, writable=lambda d: True)
    row = {"doctype": "Purchase Order Item", "name": "row1", "parent": "PO-1",
           "parenttype": "Purchase Order", "parentfield": "items", "qty": 5}
    for method in ("frappe.client.save", "frappe.client.insert", "frappe.client.submit"):
        with pytest.raises(HardLimitError, match="through their parent"):
            c.request("POST", f"/api/method/{method}", body={"doc": row})
    for verb, path in (("PUT", "/api/resource/Purchase%20Order%20Item/row1"),
                       ("POST", "/api/resource/Purchase%20Order%20Item")):
        with pytest.raises(HardLimitError, match="through their parent"):
            c.request(verb, path, body={"parent": "PO-1", "qty": 5})
    assert not seen
