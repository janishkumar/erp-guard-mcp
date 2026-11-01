"""The ERPNext connector's purchase-order lifecycle on the seeded approval workflow, over
the fake ERPNext (the live tests run the same against v16.52.0)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from conftest import MGR, REQ
from fake_erpnext import FakeErpNext, connector

from erp_guard.connectors.base import ErpError
from erp_guard.connectors.erpnext import _order_status
from erp_guard.models import Line, Status

PO = "PUR-ORD-2026-00009"
APPLY = "frappe.model.workflow.apply_workflow"


@pytest.mark.parametrize("doc,status", [
    ({"docstatus": 1}, Status.APPROVED),                       # history: before the workflow
    ({"docstatus": 1, "workflow_state": "Approved"}, Status.APPROVED),
    ({"docstatus": 2, "workflow_state": "Approved"}, Status.CANCELLED),  # plain cancel
    ({"docstatus": 0}, Status.DRAFT),
    ({"docstatus": 0, "workflow_state": "Draft"}, Status.DRAFT),
    ({"docstatus": 0, "workflow_state": "Pending Approval"}, Status.PENDING),
    ({"docstatus": 0, "workflow_state": "Rejected"}, Status.REJECTED),
    ({"docstatus": 0, "workflow_state": "Pending Approval", "erp_guard_cancelled": 1},
     Status.CANCELLED),
    ({"docstatus": 0, "workflow_state": "Draft", "erp_guard_cancelled": 0}, Status.DRAFT),
])
def test_status_from_docstatus_workflow_state_and_flag(doc, status):
    assert _order_status(doc) == status


@pytest.fixture
def fake():
    f = FakeErpNext(REQ)
    f.add({"doctype": "Purchase Order", "name": PO, "docstatus": 0, "owner": REQ,
           "workflow_state": "Draft", "supplier": "HERC RENTALS INC",
           "erp_guard_cost_centre": "Fleet - LDU", "transaction_date": "2026-10-20",
           "schedule_date": "2026-11-03",
           "items": [{"item_code": "SALT-TON", "qty": 1, "rate": 900}]})
    return f


def _doc(fake) -> dict:
    return fake.docs[("Purchase Order", PO)]


def _actions(fake) -> list[str]:
    return [c["action"] for c in fake.method_calls(APPLY)]


def test_create_sends_no_status_field(fake):
    o = connector(fake).create_order("HERC RENTALS INC", "Fleet - LDU",
                                     [Line("SALT-TON", Decimal(1), Decimal(900))], "",
                                     date(2026, 11, 3))
    assert o.status == Status.DRAFT
    body = next(r for r in fake.calls if r.method == "POST" and "resource/Purchase" in str(r.url))
    assert b"erp_guard_status" not in body.content


def test_submit_reject_resubmit_approve(fake):
    erp = connector(fake)
    assert erp.set_status(PO, Status.PENDING, "").status == Status.PENDING
    assert erp.set_status(PO, Status.REJECTED, "too much").status == Status.REJECTED
    assert _doc(fake)["erp_guard_reason"] == "too much"  # saved before the transition
    # Resubmitting a rejected order revises it back to Draft first.
    assert erp.set_status(PO, Status.PENDING, "").status == Status.PENDING
    fake.user = MGR
    o = erp.approve_order(PO)
    assert (o.status, o.approver) == (Status.APPROVED, MGR)
    assert _actions(fake) == ["Submit for Approval", "Reject", "Revise", "Submit for Approval",
                              "Approve"]


def test_other_statuses_are_not_set_directly(fake):
    with pytest.raises(ErpError, match="directly"):
        connector(fake).set_status(PO, Status.APPROVED, "")
    assert not fake.method_calls(APPLY)


def test_cancel_draft_sets_the_flag_and_cancel_approved_cancels(fake):
    erp = connector(fake)
    erp.set_status(PO, Status.PENDING, "")
    o = erp.cancel_order(PO, "not needed")
    d = _doc(fake)
    assert o.status == Status.CANCELLED
    assert (d["docstatus"], d["workflow_state"], d["erp_guard_cancelled"],
            d["erp_guard_reason"]) == (0, "Pending Approval", 1, "not needed")
    assert not fake.method_calls("frappe.client.cancel")

    d.update({"docstatus": 1, "workflow_state": "Approved", "erp_guard_cancelled": 0})
    assert erp.cancel_order(PO, "done").status == Status.CANCELLED
    assert fake.method_calls("frappe.client.cancel") == [
        {"doctype": "Purchase Order", "name": PO}]
    assert _doc(fake)["docstatus"] == 2


def test_amend_draft_rejected_and_pending(fake):
    erp = connector(fake)
    new = [Line("SALT-TON", Decimal(2), Decimal(900))]
    o = erp.amend_order(PO, new, "two")
    assert (o.status, o.total) == (Status.DRAFT, Decimal(1800)) and _actions(fake) == []
    _doc(fake)["workflow_state"] = "Rejected"
    o = erp.amend_order(PO, new, "two")
    assert o.status == Status.DRAFT and _actions(fake) == ["Revise"]
    _doc(fake)["workflow_state"] = "Pending Approval"
    o = erp.amend_order(PO, [Line("SALT-TON", Decimal(3), Decimal(900))], "three")
    assert (o.status, o.total) == (Status.DRAFT, Decimal(2700))
    assert _actions(fake) == ["Revise", "Withdraw"]
    # Withdrawn before the lines are saved: the PUT comes after the transition.
    paths = [(r.method, r.url.path) for r in fake.calls if r.method in ("POST", "PUT")]
    assert paths[-2:] == [("POST", "/api/method/" + APPLY),
                          ("PUT", f"/api/resource/Purchase Order/{PO}")]


def test_missing_order(fake):
    with pytest.raises(ErpError, match="No purchase order"):
        connector(fake).cancel_order("PUR-ORD-404", "x")
