"""Integration tests against the local ERPNext from `make up && make seed`.

Skipped unless it's reachable. Each test cancels what it creates, so budgets and the
split window stay usable across runs (cancelled orders stay on record; nothing is deleted).
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from erp_guard.connectors.erpnext import ErpNext, HardLimitError
from erp_guard.env import load_env
from erp_guard.generic.actions import meta_of
from erp_guard.models import Status
from erp_guard.policy import load_policy
from erp_guard.service import Service

load_env()
URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")


def _up() -> bool:
    try:
        return httpx.get(f"{URL}/api/method/ping", timeout=3).status_code == 200 \
            and bool(os.environ.get("ERPNEXT_KEY_REQUESTER"))
    except httpx.HTTPError:
        return False


pytestmark = [pytest.mark.live, pytest.mark.skipif(not _up(), reason="local ERPNext not up")]
CC = "Facilities - LDU"


def svc(tag: str) -> Service:
    return Service(ErpNext.from_env(tag), load_policy())


def run(s: Service, action: str, args: dict):
    p = s.preview(action, args)
    return p, s.execute(action, args, p.code)


@pytest.fixture
def created():
    ids: list[str] = []
    yield ids
    req = ErpNext.from_env("REQUESTER")
    for po in ids:
        o = req.get_order(po)
        if o and o.status not in (Status.CANCELLED,):
            (ErpNext.from_env("DIRECTOR") if o.status == Status.APPROVED else req) \
                .cancel_order(po, "test cleanup")


def _po(qty=2, rate=640.0, item="HVAC-SVC", supplier="TRANE U.S. INC", **kw):
    return {"supplier": supplier, "cost_centre": CC,
            "lines": [{"item_code": item, "qty": qty, "rate": rate}], **kw}


def test_identity_comes_from_the_key():
    assert svc("REQUESTER").actor == "requester@example.com"
    assert svc("MANAGER").actor == "manager@example.com"


def test_full_lifecycle_with_audit(created):
    req, mgr = svc("REQUESTER"), svc("MANAGER")
    _, out = run(req, "create_purchase_order", _po(notes=f"live {uuid.uuid4().hex[:6]}"))
    assert out.outcome == "written", out.message
    po = out.target
    created.append(po)
    assert run(req, "submit_for_approval", {"po": po})[1].outcome == "written"
    p = req.preview("approve_purchase_order", {"po": po})
    assert p.blocked  # requester can't approve own order
    _, ok = run(mgr, "approve_purchase_order", {"po": po})
    assert ok.outcome == "written", ok.message
    o = req.erp.get_order(po)
    assert o.status == Status.APPROVED and o.approver == "manager@example.com"
    trail = req.erp.read_audit(po, limit=10)
    assert [a.outcome for a in trail][:3] == ["written"] * 3
    _, c = run(req, "cancel_purchase_order", {"po": po, "reason": "test done"})
    assert c.outcome == "written" and req.erp.get_order(po).status == Status.CANCELLED


def test_reject_amend_resubmit(created):
    req, mgr = svc("REQUESTER"), svc("MANAGER")
    _, out = run(req, "create_purchase_order", _po())
    created.append(out.target)
    run(req, "submit_for_approval", {"po": out.target})
    _, rej = run(mgr, "reject_purchase_order", {"po": out.target, "reason": "qty too high"})
    assert rej.outcome == "written"
    assert req.erp.get_order(out.target).status == Status.REJECTED
    _, am = run(req, "amend_purchase_order", {"po": out.target, "lines": [
        {"item_code": "HVAC-SVC", "qty": 1, "rate": 640}]})
    assert am.outcome == "written", am.message
    o = req.erp.get_order(out.target)
    assert o.status == Status.DRAFT and o.total == 640


def test_write_without_code_is_refused_and_audited_in_erp(created):
    req = svc("REQUESTER")
    before = len(req.erp.list_orders(cost_centre=CC))
    out = req.execute("create_purchase_order", _po(), None)
    assert out.outcome == "refused"
    assert len(req.erp.list_orders(cost_centre=CC)) == before
    assert req.erp.read_audit(limit=1)[0].outcome == "refused"


def test_duplicate_supplier_against_real_master():
    p = svc("REQUESTER").preview("onboard_supplier", {"name": "Herc Rentals, Inc"})
    assert p.blocked


def test_hard_limits_hold_against_the_real_client():
    erp = ErpNext.from_env("REQUESTER")
    # Payment Entry is pack-owned, so the client lets it through to the pack's checks; a
    # ledger-posting type with no pack, and the system's own types, it never writes.
    for doctype in ("Journal Entry", "Bank Account", "User"):
        with pytest.raises(HardLimitError):
            erp.c.request("POST", f"/api/resource/{doctype}", body={})
        with pytest.raises(HardLimitError):
            erp.c.request("GET", f"/api/resource/{doctype}")
    with pytest.raises(HardLimitError):
        erp.c.request("PUT", "/api/resource/ERP Guard Audit/x", body={"outcome": "written"})
    with pytest.raises(HardLimitError):
        erp.c.request("DELETE", "/api/resource/Supplier/TRANE U.S. INC")
    with pytest.raises(HardLimitError):
        erp.c.request("PUT", "/api/resource/Supplier/TRANE U.S. INC",
                      body={"default_bank_account": "x"})


def test_live_generic_task_create_and_update():
    """A later-phase type through the generic tools: create, update, audit in ERPNext. Tasks
    can't be cancelled, so the test closes its Task by setting it to Cancelled."""
    req = svc("REQUESTER")
    subject = f"live generic {uuid.uuid4().hex[:6]}"
    p, out = run(req, "create_document", {"doctype": "Task", "fields": {"subject": subject}})
    assert out.outcome == "written", (out.message, [r.message for r in p.checks])
    task = out.target
    assert req.erp.get_doc("Task", task)["subject"] == subject
    p, upd = run(req, "update_document", {"doctype": "Task", "name": task,
                                          "fields": {"subject": subject + " (b)"}})
    assert f"subject: '{subject}' → '{subject} (b)'" in p.summary
    assert upd.outcome == "written", upd.message
    _, done = run(req, "update_document", {"doctype": "Task", "name": task,
                                           "fields": {"status": "Cancelled"}})
    assert done.outcome == "written", done.message
    doc = req.erp.get_doc("Task", task)
    assert doc["subject"] == subject + " (b)" and doc["status"] == "Cancelled"
    trail = req.erp.read_audit(task, limit=10)
    assert [a.action for a in trail] == ["update_document", "update_document",
                                         "create_document"]
    assert {a.outcome for a in trail} == {"written"}


def test_live_generic_scope_and_workflow_detection():
    req = svc("REQUESTER")
    meta, _ = meta_of(req, "Task")
    assert meta.has_workflow is False  # getdoctype's __workflow_docs, empty with no workflow
    w = req.erp.c.writable
    assert w("Task") and w("Purchase Order") and not w("Journal Entry")
    assert not w("Cost Center") and not w("Server Script")
    p = req.preview("create_document", {"doctype": "Journal Entry", "fields": {}})
    assert p.blocked and "ledger" in p.checks[0].message
    out = req.execute("create_document", {"doctype": "Server Script", "fields": {}}, None)
    assert out.outcome == "refused"
    assert "Projects User" in req.erp.roles()
