"""Master data owned by the purchase-to-pay pack, over a fake ERPNext: creates with a
duplicate-name warning, check-relevant changes as change requests a second person decides,
and the open-document block."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import MGR, REQ, REQ2, TODAY
from fake_erpnext import FakeErpNext, connector

from erp_guard.checks import BLOCK, PASS, WARN
from erp_guard.connectors.base import ErpError
from erp_guard.service import Service

CO = "Lakeside Demo Utilities"
CR = "ERP Guard Change Request"
CONTROLLER = "controller@example.com"
AP = "ap@example.com"


@pytest.fixture
def fake():
    f = FakeErpNext(REQ)
    f.add({"doctype": "Item Group", "name": "Demo Purchases"})
    f.add({"doctype": "UOM", "name": "Nos"})
    f.add({"doctype": "Country", "name": "United States"})
    for code, name in (("SALT-TON", "Road salt, per ton"), ("TONER-K", "Black toner cartridge")):
        f.add({"doctype": "Item", "name": code, "item_code": code, "item_name": name,
               "item_group": "Demo Purchases", "stock_uom": "Nos", "is_stock_item": 1,
               "disabled": 0, "description": name})
    for sup in ("HERC RENTALS INC", "TRANE U.S. INC", "MORTON SALT INC"):
        f.add({"doctype": "Supplier", "name": sup, "supplier_name": sup, "disabled": 0,
               "on_hold": 0, "erp_guard_onboarded_by": None})
    # A live order for road salt from HERC; an approved, unpaid invoice from MORTON.
    f.add({"doctype": "Purchase Order", "name": "PO-1", "docstatus": 0, "owner": REQ2,
           "workflow_state": "Pending Approval", "supplier": "HERC RENTALS INC",
           "company": CO, "transaction_date": (TODAY - timedelta(days=3)).isoformat(),
           "items": [{"item_code": "SALT-TON", "qty": 1, "rate": 78}]})
    f.add({"doctype": "Purchase Invoice", "name": "PI-1", "docstatus": 1, "owner": AP,
           "workflow_state": "Approved", "supplier": "MORTON SALT INC", "company": CO,
           "bill_no": "B-1", "bill_date": TODAY.isoformat(),
           "posting_date": TODAY.isoformat(), "outstanding_amount": 420,
           "items": [{"item_code": "PAPER-A4", "qty": 10, "rate": 42}]})
    for name, sup in (("TRANE-Billing", "TRANE U.S. INC"), ("MORTON-Billing", "MORTON SALT INC"),
                      ("Depot-Office", None)):
        f.add({"doctype": "Address", "name": name, "address_title": name,
               "address_type": "Billing", "address_line1": "1 Main St", "city": "Chicago",
               "country": "United States",
               "links": [{"doctype": "Dynamic Link", "name": f"dl-{name}", "idx": 1,
                          "link_doctype": "Supplier", "link_name": sup}] if sup else []})
    return f


def as_user(fake: FakeErpNext, policy, user: str) -> Service:
    fake.user = user
    return Service(connector(fake), policy, today=lambda: TODAY)


@pytest.fixture
def svc(fake, policy):
    return as_user(fake, policy, REQ)


def by_check(p, name):
    return [r for r in p.checks if r.check == name]


def update(doctype, name, **fields):
    return {"doctype": doctype, "name": name, "fields": fields}


def decide(cr, action, comment=None):
    return {"doctype": CR, "name": cr, "action": action, "comment": comment}


def make_cr(svc, fake, doctype="Item", name="TONER-K", **fields):
    args = update(doctype, name, **(fields or {"is_stock_item": 0}))
    p = svc.preview("update_document", args)
    out = svc.execute("update_document", args, p.code)
    assert out.outcome == "written", out.message
    return out.target


def test_flip_item_to_non_stock_with_open_po_blocked(svc, fake):
    p = svc.preview("update_document", update("Item", "SALT-TON", is_stock_item=0))
    assert p.blocked and p.code is None
    block = by_check(p, "open documents")[0]
    assert block.level == BLOCK and "purchase order PO-1" in block.message
    assert not fake.of(CR)
    assert fake.docs[("Item", "SALT-TON")]["is_stock_item"] == 1


def test_item_change_creates_change_request(svc, fake):
    args = update("Item", "TONER-K", is_stock_item=0)
    p = svc.preview("update_document", args)
    assert p.code, [r.message for r in p.checks if r.level == BLOCK]
    assert "is_stock_item: 1 → '0'" in p.summary and "change request" in p.summary
    assert by_check(p, "open documents")[0].level == PASS
    assert by_check(p, "second person")[0].level == PASS
    out = svc.execute("update_document", args, p.code)
    assert out.outcome == "written", out.message
    cr = fake.docs[(CR, out.target)]
    assert (cr["target_doctype"], cr["target_name"], cr["requested_by"],
            cr["workflow_state"]) == ("Item", "TONER-K", REQ, "Pending Approval")
    assert json.loads(cr["fields_json"]) == {"is_stock_item": "0"}
    assert fake.docs[("Item", "TONER-K")]["is_stock_item"] == 1  # nothing applied yet
    assert fake.audit()[-1]["target"] == out.target


def test_item_change_outside_check_fields_applies_directly(svc, fake):
    args = update("Item", "SALT-TON", description="Rock salt, bulk")
    p = svc.preview("update_document", args)
    assert p.code and by_check(p, "master data")[0].level == PASS
    out = svc.execute("update_document", args, p.code)
    assert out.outcome == "written" and out.target == "SALT-TON"
    assert fake.docs[("Item", "SALT-TON")]["description"] == "Rock salt, bulk"
    assert not fake.of(CR)


def test_change_request_self_approve_blocked(svc, fake, policy):
    cr = make_cr(svc, fake)
    p = svc.preview("workflow_action", decide(cr, "Approve"))
    assert p.blocked and p.code is None
    assert by_check(p, "second person")[0].level == BLOCK
    # Someone else, but without a purchase approval limit, can't decide it either.
    other = as_user(fake, policy, REQ2)
    p = other.preview("workflow_action", decide(cr, "Approve"))
    block = by_check(p, "approver")[0]
    assert block.level == BLOCK and "purchase approvers" in block.message
    assert fake.docs[(CR, cr)]["workflow_state"] == "Pending Approval"
    # Nor can the person who made the request, whoever it names as requester.
    fake.docs[(CR, cr)]["requested_by"] = REQ2
    p = as_user(fake, policy, REQ).preview("workflow_action", decide(cr, "Approve"))
    assert p.blocked and by_check(p, "second person")[0].level == BLOCK


def test_change_request_changed_after_submission_blocked(svc, fake, policy):
    cr = make_cr(svc, fake)
    fake.docs[(CR, cr)].update(modified_by=REQ, fields_json='{"is_stock_item": "0"}')
    assert as_user(fake, policy, MGR).preview("workflow_action", decide(cr, "Approve")).code
    fake.docs[(CR, cr)].update(modified_by=REQ2, fields_json='{"disabled": "1"}')
    p = as_user(fake, policy, MGR).preview("workflow_action", decide(cr, "Approve"))
    block = by_check(p, "second person")[0]
    assert p.blocked and block.level == BLOCK and "changed by" in block.message


def test_change_request_approval_rechecks_fields(svc, fake, policy):
    cr = make_cr(svc, fake, "Supplier", "TRANE U.S. INC", on_hold=1)
    mgr = as_user(fake, policy, MGR)
    for fields, check in (('{"erp_guard_onboarded_by": "manager@example.com"}', "fixed fields"),
                          ('{"supplier_primary_address": "NOPE"}', "links"),
                          ('{"default_bank_account": "x"}', "bank details")):
        fake.docs[(CR, cr)]["fields_json"] = fields
        p = mgr.preview("workflow_action", decide(cr, "Approve"))
        assert p.blocked and by_check(p, check)[0].level == BLOCK, check


def test_change_request_approve_applies(svc, fake, policy):
    cr = make_cr(svc, fake)
    mgr = as_user(fake, policy, MGR)
    p = mgr.preview("workflow_action", decide(cr, "Approve"))
    assert p.code, [r.message for r in p.checks if r.level == BLOCK]
    assert "TONER-K: is_stock_item: 1 → '0'" in p.summary
    out = mgr.execute("workflow_action", decide(cr, "Approve"), p.code)
    assert out.outcome == "written", out.message
    assert fake.docs[("Item", "TONER-K")]["is_stock_item"] == "0"
    doc = fake.docs[(CR, cr)]
    assert (doc["workflow_state"], doc["status"], doc["decided_by"]) == (
        "Approved", "Applied", MGR)
    p = mgr.preview("workflow_action", decide(cr, "Approve"))  # decided once only
    assert p.blocked and by_check(p, "change request")[0].level == BLOCK


def test_change_request_approval_rechecks_open_documents(svc, fake, policy):
    cr = make_cr(svc, fake)
    fake.add({"doctype": "Purchase Order", "name": "PO-2", "docstatus": 1, "owner": REQ2,
              "workflow_state": "Approved", "supplier": "TRANE U.S. INC", "company": CO,
              "transaction_date": TODAY.isoformat(),
              "items": [{"item_code": "TONER-K", "qty": 2, "rate": 88}]})
    p = as_user(fake, policy, MGR).preview("workflow_action", decide(cr, "Approve"))
    assert p.blocked and "purchase order PO-2" in by_check(p, "open documents")[0].message
    fake.docs[("Purchase Order", "PO-2")]["status"] = "Closed"  # a closed order isn't open
    p = as_user(fake, policy, MGR).preview("workflow_action", decide(cr, "Approve"))
    assert p.code


def test_change_request_reject_needs_comment(svc, fake, policy):
    cr = make_cr(svc, fake)
    args = decide(cr, "Reject", "Toner is stocked in the stores")
    # The controller approves invoices, not purchases: no say on master data.
    p = as_user(fake, policy, CONTROLLER).preview("workflow_action", args)
    assert p.blocked and by_check(p, "approver")[0].level == BLOCK
    mgr = as_user(fake, policy, MGR)
    p = mgr.preview("workflow_action", decide(cr, "Reject"))
    assert p.blocked and by_check(p, "reason")[0].level == BLOCK
    p = mgr.preview("workflow_action", args)
    assert p.code and "Nothing changes" in p.summary
    out = mgr.execute("workflow_action", args, p.code)
    assert out.outcome == "written", out.message
    doc = fake.docs[(CR, cr)]
    assert (doc["workflow_state"], doc["status"], doc["decided_by"], doc["reason"]) == (
        "Rejected", "Rejected", MGR, "Toner is stocked in the stores")
    assert fake.docs[("Item", "TONER-K")]["is_stock_item"] == 1


def test_change_request_other_actions_and_targets_refused(svc, fake, policy):
    cr = make_cr(svc, fake)
    mgr = as_user(fake, policy, MGR)
    for action in ("Submit for Approval", "Revise", "Withdraw"):
        p = mgr.preview("workflow_action", decide(cr, action))
        assert p.blocked and by_check(p, "pack")[0].level == BLOCK, action
    for op, args in (("create_document", {"doctype": CR, "fields": {
            "target_doctype": "Item", "target_name": "TONER-K", "fields_json": "{}"}}),
            ("update_document", update(CR, cr, fields_json='{"disabled": 1}')),
            ("cancel_document", {"doctype": CR, "name": cr, "reason": "x"})):
        p = mgr.preview(op, args)
        assert p.blocked and by_check(p, "pack")[0].level == BLOCK, op
    # A change request made outside erp-guard for a document the pack doesn't change this
    # way is never applied through it.
    fake.add({"doctype": CR, "name": "ECR-X", "docstatus": 0, "owner": REQ2,
              "workflow_state": "Pending Approval", "target_doctype": "Purchase Order",
              "target_name": "PO-1", "fields_json": '{"supplier": "TRANE U.S. INC"}',
              "requested_by": REQ2})
    p = mgr.preview("workflow_action", decide("ECR-X", "Approve"))
    assert p.blocked and "isn't master data" in by_check(p, "change request")[0].message


def test_supplier_address_change_needs_second_person(svc, fake, policy):
    # Linked to a supplier with nothing open: a change request.
    args = update("Address", "TRANE-Billing", address_line1="99 Elsewhere Rd")
    p = svc.preview("update_document", args)
    assert p.code and by_check(p, "second person")[0].level == PASS
    out = svc.execute("update_document", args, p.code)
    assert out.outcome == "written" and out.target in {d["name"] for d in fake.of(CR)}
    assert fake.docs[("Address", "TRANE-Billing")]["address_line1"] == "1 Main St"
    p = svc.preview("workflow_action", decide(out.target, "Approve"))
    assert p.blocked and by_check(p, "second person")[0].level == BLOCK
    mgr = as_user(fake, policy, MGR)
    p = mgr.preview("workflow_action", decide(out.target, "Approve"))
    assert mgr.execute("workflow_action", decide(out.target, "Approve"),
                       p.code).outcome == "written"
    assert fake.docs[("Address", "TRANE-Billing")]["address_line1"] == "99 Elsewhere Rd"

    # Linked to a supplier with an unpaid invoice: blocked.
    svc = as_user(fake, policy, REQ)
    p = svc.preview("update_document", update("Address", "MORTON-Billing",
                                              address_line1="99 Elsewhere Rd"))
    assert p.blocked and "unpaid invoice PI-1" in by_check(p, "open documents")[0].message
    fake.docs[("Purchase Invoice", "PI-1")]["outstanding_amount"] = 0  # paid in full
    p = svc.preview("update_document", update("Address", "MORTON-Billing",
                                              address_line1="99 Elsewhere Rd"))
    assert p.code

    # Pointing a supplier at another address is the same change.
    p = svc.preview("update_document", update("Supplier", "HERC RENTALS INC",
                                              supplier_primary_address="TRANE-Billing"))
    assert p.blocked and "purchase order PO-1" in by_check(p, "open documents")[0].message


def test_address_not_linked_to_a_supplier_is_generic(svc, fake):
    args = update("Address", "Depot-Office", address_line1="2 Depot Rd")
    p = svc.preview("update_document", args)
    assert p.code
    assert by_check(p, "scope")[0].evidence == {"scope": "generic"}
    assert svc.execute("update_document", args, p.code).outcome == "written"
    assert fake.docs[("Address", "Depot-Office")]["address_line1"] == "2 Depot Rd"
    assert not fake.of(CR)
    # Linking it to a supplier makes it the pack's: a change request.
    link = [{"link_doctype": "Supplier", "link_name": "TRANE U.S. INC"}]
    p = svc.preview("update_document", update("Address", "Depot-Office", links=link))
    assert p.code and by_check(p, "scope")[0].evidence == {"scope": "pack"}
    assert by_check(p, "second person")[0].level == PASS
    # Creating one: linked to a supplier, the pack's (a change request); not linked,
    # generic.
    new = {"address_title": "New", "address_type": "Billing", "address_line1": "3 Rd",
           "city": "Chicago", "country": "United States"}
    p = svc.preview("create_document", {"doctype": "Address", "fields": {**new, "links": link}})
    assert p.code and by_check(p, "scope")[0].evidence == {"scope": "pack"}
    p = svc.preview("create_document", {"doctype": "Address", "fields": new})
    assert p.code and by_check(p, "scope")[0].evidence == {"scope": "generic"}


def test_supplier_changes(svc, fake):
    p = svc.preview("update_document", update("Supplier", "TRANE U.S. INC",
                                              erp_guard_onboarded_by=REQ))
    assert p.blocked and by_check(p, "fixed fields")[0].level == BLOCK
    p = svc.preview("update_document", update("Supplier", "TRANE U.S. INC", on_hold=1))
    assert p.code and "change request" in p.summary
    p = svc.preview("update_document", update("Supplier", "TRANE U.S. INC",
                                              erp_guard_notes="Prefers email"))
    assert p.code and by_check(p, "master data")[0].level == PASS
    p = svc.preview("update_document", update("Supplier", "TRANE U.S. INC",
                                              default_bank_account="DE89 3704 0044 0532 0130 00"))
    assert p.blocked and by_check(p, "bank details")[0].level == BLOCK


def test_generic_supplier_create_refused(svc, fake):
    p = svc.preview("create_document", {"doctype": "Supplier", "fields": {
        "supplier_name": "NEW SUPPLIER LLC", "supplier_group": "Demo Suppliers"}})
    assert p.blocked and p.code is None
    refusal = by_check(p, "pack")[0]
    assert refusal.level == BLOCK and "onboard_supplier" in refusal.message
    assert not fake.method_calls("frappe.client.insert")


def test_item_duplicate_name_warns(svc, fake):
    item = {"item_code": "SALT-ROAD", "item_name": "Road Salt (per ton)",
            "item_group": "Demo Purchases", "stock_uom": "Nos", "is_stock_item": 1}
    p = svc.preview("create_document", {"doctype": "Item", "fields": item})
    warn = by_check(p, "duplicate name")[0]
    assert warn.level == WARN and "SALT-TON" in warn.message
    assert p.code  # a warning, not a block
    out = svc.execute("create_document", {"doctype": "Item", "fields": item}, p.code)
    assert out.outcome == "written", out.message
    assert fake.docs[("Item", out.target)]["item_name"] == "Road Salt (per ton)"

    other = {**item, "item_code": "VALVE-GATE-8", "item_name": "8-inch gate valve"}
    p = svc.preview("create_document", {"doctype": "Item", "fields": other})
    assert by_check(p, "duplicate name")[0].level == PASS
    # The generic checks apply in full: an item needs its group and unit.
    p = svc.preview("create_document", {"doctype": "Item", "fields": {
        "item_code": "X-1", "item_name": "Something else"}})
    assert p.blocked
    assert {r.message for r in by_check(p, "known fields")} == {
        "Item.item_group is required", "Item.stock_uom is required"}


def test_item_price_duplicate_warns(svc, fake):
    fake.add({"doctype": "Price List", "name": "Standard Buying"})
    fake.add({"doctype": "Item Price", "name": "IP-1", "item_code": "SALT-TON",
              "price_list": "Standard Buying", "price_list_rate": 78, "uom": "Nos"})
    price = {"item_code": "SALT-TON", "price_list": "Standard Buying", "uom": "Nos",
             "price_list_rate": 80}
    p = svc.preview("create_document", {"doctype": "Item Price", "fields": price})
    assert by_check(p, "duplicate name")[0].level == WARN and p.code
    # Its rate is read like the item: blocked while the item is on an open order.
    p = svc.preview("update_document", update("Item Price", "IP-1", price_list_rate=90))
    assert p.blocked and "PO-1" in by_check(p, "open documents")[0].message


def _no_payments(svc: Service) -> None:
    def refused(**_):
        raise ErpError("ERPNext 403: Insufficient Permission for Payment Entry")
    svc.erp.list_payments = refused


def test_unreadable_documents_warn_on_request_and_block_on_approval(svc, fake, policy):
    _no_payments(svc)  # a requester can't read payments in ERPNext
    args = update("Address", "TRANE-Billing", address_line1="99 Elsewhere Rd")
    p = svc.preview("update_document", args)
    warn = by_check(p, "open documents")[0]
    assert warn.level == WARN and "payments" in warn.message and p.code
    cr = svc.execute("update_document", args, p.code).target
    mgr = as_user(fake, policy, MGR)
    _no_payments(mgr)
    p = mgr.preview("workflow_action", decide(cr, "Approve"))
    block = by_check(p, "open documents")[0]
    assert p.blocked and block.level == BLOCK and "can't read payments" in block.message
    assert as_user(fake, policy, MGR).preview("workflow_action", decide(cr, "Approve")).code


def new_address(sup="MORTON SALT INC", **kw):
    return {"doctype": "Address", "fields": {
        "address_title": "Remit", "address_type": "Billing", "address_line1": "77 New Rd",
        "city": "Chicago", "country": "United States", "is_primary_address": 1,
        "links": [{"link_doctype": "Supplier", "link_name": sup}], **kw}}


def test_new_supplier_address_is_a_change_request(svc, fake, policy):
    args = new_address()
    # MORTON has an unpaid invoice: a new primary address would redirect it.
    p = svc.preview("create_document", args)
    assert p.blocked and "unpaid invoice PI-1" in by_check(p, "open documents")[0].message
    fake.docs[("Purchase Invoice", "PI-1")]["outstanding_amount"] = 0
    p = svc.preview("create_document", args)
    assert p.code and "change request" in p.summary
    out = svc.execute("create_document", args, p.code)
    assert out.outcome == "written", out.message
    cr = fake.docs[(CR, out.target)]
    assert (cr["target_doctype"], cr["target_name"], cr["workflow_state"]) == (
        "Address", "", "Pending Approval")
    assert json.loads(cr["fields_json"])["address_line1"] == "77 New Rd"
    before = {d["name"] for d in fake.of("Address")}
    assert fake.method_calls("frappe.client.insert")[-1]["doc"]["doctype"] == CR

    mgr = as_user(fake, policy, MGR)
    assert svc.preview("workflow_action", decide(out.target, "Approve")).blocked  # own
    fake.docs[("Purchase Invoice", "PI-1")]["outstanding_amount"] = 420  # open again
    p = mgr.preview("workflow_action", decide(out.target, "Approve"))
    assert p.blocked and "unpaid invoice PI-1" in by_check(p, "open documents")[0].message
    fake.docs[("Purchase Invoice", "PI-1")]["outstanding_amount"] = 0  # paid
    p = mgr.preview("workflow_action", decide(out.target, "Approve"))
    assert p.code and "a new Address" in p.summary and "It is created" in p.summary
    done = mgr.execute("workflow_action", decide(out.target, "Approve"), p.code)
    assert done.outcome == "written", done.message
    made = {d["name"] for d in fake.of("Address")} - before
    assert len(made) == 1
    cr = fake.docs[(CR, out.target)]
    assert (cr["status"], cr["target_name"]) == ("Applied", made.pop())
    assert fake.docs[("Address", cr["target_name"])]["address_line1"] == "77 New Rd"


def test_new_supplier_address_refuses_bank_details(svc, fake):
    p = svc.preview("create_document", new_address("TRANE U.S. INC",
                                                   address_line2="IBAN DE89370400440532013000"))
    assert p.blocked and by_check(p, "bank details")[0].level == BLOCK


def test_duplicate_address_and_contact_warn(svc, fake):
    # The same street, city and postcode as the supplier's billing address, spelled
    # differently.
    p = svc.preview("create_document", new_address("TRANE U.S. INC", address_line1="1  MAIN st",
                                                   city="chicago "))
    warn = by_check(p, "duplicate address")[0]
    assert warn.level == WARN and "TRANE-Billing" in warn.message and p.code
    p = svc.preview("create_document", new_address("TRANE U.S. INC"))
    assert by_check(p, "duplicate address")[0].level == PASS
    fake.add({"doctype": "Contact", "name": "Pat Lee", "first_name": "Pat",
              "email_id": "pat.lee@example.com", "links": []})
    contact = {"doctype": "Contact", "fields": {
        "first_name": "Patricia", "email_id": "Pat.Lee@Example.com",
        "links": [{"link_doctype": "Supplier", "link_name": "TRANE U.S. INC"}]}}
    p = svc.preview("create_document", contact)
    warn = by_check(p, "duplicate contact")[0]
    assert warn.level == WARN and "Pat Lee" in warn.message and p.code
    contact["fields"]["email_id"] = "someone.else@example.com"
    assert by_check(svc.preview("create_document", contact), "duplicate contact")[0].level \
        == PASS


def test_item_price_item_and_unit_changes_need_a_second_person(svc, fake):
    fake.add({"doctype": "Price List", "name": "Standard Buying"})
    fake.add({"doctype": "UOM", "name": "Box"})
    fake.add({"doctype": "Item Price", "name": "IP-2", "item_code": "TONER-K",
              "price_list": "Standard Buying", "price_list_rate": 88, "uom": "Nos"})
    p = svc.preview("update_document", update("Item Price", "IP-2", uom="Box"))
    assert p.code and by_check(p, "second person")[0].level == PASS
    p = svc.preview("update_document", update("Item Price", "IP-2", note="x"))
    assert p.code and by_check(p, "master data")[0].level == PASS
