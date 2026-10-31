"""The action registry: what the service can write, and how arguments canonicalise."""

from __future__ import annotations

import asyncio

import pytest
from conftest import REQ, po_args

from erp_guard import server, service
from erp_guard.ops import Op, Write
from erp_guard.packs.base import Pack
from erp_guard.service import (
    Action,
    InvalidArgs,
    Service,
    canonical_args,
    default_actions,
    payload_hash,
)

V1 = {"create_purchase_order", "onboard_supplier", "submit_for_approval",
      "approve_purchase_order", "reject_purchase_order", "cancel_purchase_order",
      "amend_purchase_order"}


def test_v1_actions_registered(erp, policy):
    svc = Service(erp, policy)
    assert set(svc.actions) >= V1
    assert all(isinstance(a, Action) and svc.actions[a.name] is a
               for a in svc.actions.values())
    assert {a.name for a in default_actions()} == set(svc.actions)


def test_write_actions_follow_the_registry():
    names = service.WRITE_ACTIONS
    assert names == tuple(a.name for a in default_actions())
    with pytest.raises(AttributeError):
        service.NO_SUCH_THING  # noqa: B018


def test_unknown_action_refused(svc):
    with pytest.raises(InvalidArgs) as exc:
        svc.preview("drop_tables", {})
    msg = str(exc.value)
    assert "drop_tables" in msg
    for name in V1:
        assert name in msg


def test_unknown_action_write_refused_and_audited(erp, svc):
    out = svc.execute("drop_tables", {}, "ABCD2345")
    assert out.outcome == "refused" and "drop_tables" in out.message
    assert erp.read_audit(None, 5)[0].outcome == "refused"


def test_canonical_numbers_equal():
    hashes = {
        payload_hash("create_purchase_order", REQ,
                     canonical_args("create_purchase_order", po_args(qty=q)))
        for q in (5, "5", 5.0, "5.00")
    }
    assert len(hashes) == 1


def test_register_rejects_duplicate_names(svc):
    with pytest.raises(ValueError):
        svc.register(svc.actions["onboard_supplier"])


def test_registered_action_is_used(svc):
    seen = []

    def evaluate(s, canon):
        seen.append(("evaluate", canon["x"]))
        return "Do x.", []

    def write(s, canon):
        seen.append(("write", canon["x"]))
        return "X-1", "x done."

    svc.register(Action("do_x", lambda a: {"x": a["x"]}, evaluate, write, target_key="x"))
    p = svc.preview("do_x", {"x": "X-1"})
    out = svc.execute("do_x", {"x": "X-1"}, p.code)
    assert out.outcome == "written" and out.target == "X-1"
    assert seen == [("evaluate", "X-1"), ("evaluate", "X-1"), ("write", "X-1")]


def test_every_write_tool_is_a_registered_action(svc):
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())
             if not t.annotations.read_only_hint}
    assert tools <= set(svc.actions)


def test_write_op():
    w = Write(Op.SUBMIT, "Purchase Order", "PO-1")
    assert w.op == "submit" and w.fields == {} and w.action is None and w.reason == ""
    assert {o.value for o in Op} == {"create", "update", "submit", "workflow", "cancel",
                                      "amend"}


def test_pack_protocol_shape():
    assert {"owns", "evaluate_write", "apply_write"} <= set(dir(Pack))
