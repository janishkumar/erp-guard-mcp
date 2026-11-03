"""Purchase requests and supplier quotes: the pack's checks, raise_purchase_request,
request_quotes and enter_supplier_quote, the request's workflow and the submission of
requests for quotation and quotes as the generic tools reach them, and the $5,000 request
and $25,000 quote rules on purchase orders."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import DIR, MGR, REQ, REQ2, TODAY, approved_request, as_user, po_args, quoted
from conftest import run as run_action
from fake_erpnext import FakeErpNext, connector

from erp_guard import render
from erp_guard.checks import BLOCK, PASS, WARN
from erp_guard.connectors.memory import line
from erp_guard.models import PurchaseOrder, Status
from erp_guard.ops import Op, Write
from erp_guard.packs.p2p import APPROVE, REJECT, REVISE, SUBMIT_FOR_APPROVAL
from erp_guard.packs.p2p import checks as P
from erp_guard.packs.p2p.models import DocState, Quote, Request, RequestLine
from erp_guard.service import InvalidArgs, Service

D = Decimal
MR, RFQ, SQ = "Material Request", "Request for Quotation", "Supplier Quotation"


def _checks(res, name):
    return [r for r in res if r.check == name]


def _level(res, name):
    return [r.level for r in _checks(res, name)]


def _blocks(res):
    return [r.message for r in res if r.level == BLOCK]


def run(svc, action, args):
    p, out = run_action(svc, action, args)
    assert out.outcome == "written", (out.message, _blocks(p.checks))
    return out.target


def _write(erp, policy, user, op, doctype, name, action=None, reason=""):
    svc = as_user(erp, policy, user)
    pack = svc.pack_for(doctype)
    w = Write(op, doctype, name, action=action, reason=reason)
    return svc, pack, w, pack.evaluate_write(svc, w)[1]


def _step(erp, policy, user, op, doctype, name, action=None, reason=""):
    svc, pack, w, res = _write(erp, policy, user, op, doctype, name, action, reason)
    assert not _blocks(res), _blocks(res)
    pack.apply_write(svc, w)
    return res


def _wf(erp, policy, user, ref, action, comment=""):
    return _write(erp, policy, user, Op.WORKFLOW, MR, ref, action, comment)[3]


def _raised(erp, policy, lines=(("SALT-TON", 5),), cc="Fleet", user=REQ, notes=None) -> str:
    args = {"cost_centre": cc, "lines": [{"item_code": i, "qty": q} for i, q in lines],
            "notes": notes}
    return run(as_user(erp, policy, user), "raise_purchase_request", args)


def _approved(erp, policy, lines=(("SALT-TON", 5),), approver=MGR, **kw) -> str:
    rid = _raised(erp, policy, lines, **kw)
    _step(erp, policy, kw.get("user", REQ), Op.WORKFLOW, MR, rid, SUBMIT_FOR_APPROVAL)
    _step(erp, policy, approver, Op.WORKFLOW, MR, rid, APPROVE)
    return rid


def _order(id, total, request=None, status=Status.DRAFT, supplier="SUP-HERC", cc="Fleet",
           days=0, item="SALT-TON", qty=1, **kw) -> PurchaseOrder:
    return PurchaseOrder(id, supplier, cc, REQ, status, TODAY - timedelta(days=days),
                         (line(item, qty, D(total) / qty),), request=request, **kw)


# --- the brief's cases -----------------------------------------------------------------

def test_po_under_5k_no_request_ok(svc):
    p = svc.preview("create_purchase_order", po_args(rate=800))
    assert not p.blocked and p.code
    assert _level(p.checks, "request required") == [PASS]
    assert _level(p.checks, "request split") == [PASS]
    assert _level(p.checks, "competitive quotes") == [PASS]


def test_po_8k_without_request_blocked(erp, svc):
    p = svc.preview("create_purchase_order", po_args(qty=8, rate=1000))
    assert p.blocked and p.code is None
    msg = _checks(p.checks, "request required")[0].message
    assert _level(p.checks, "request required") == [BLOCK] and "purchase request" in msg
    rid = approved_request(erp, qty=8)
    p = svc.preview("create_purchase_order", po_args(qty=8, rate=1000, request=rid))
    assert not p.blocked, _blocks(p.checks)
    # Exactly $5,000 needs one too; a draft or pending request doesn't do.
    assert svc.preview("create_purchase_order", po_args(qty=5, rate=1000)).blocked
    erp.requests[rid] = replace(erp.requests[rid], state=DocState.PENDING)
    p = svc.preview("create_purchase_order", po_args(qty=8, rate=1000, request=rid))
    assert _level(p.checks, "request required") == [BLOCK]
    assert _level(p.checks, "request approved") == [BLOCK]


def test_two_3k_requestless_pos_blocked(erp, svc, policy):
    run(svc, "create_purchase_order", po_args(qty=3, rate=1000))
    p = svc.preview("create_purchase_order", po_args(qty=3, rate=1000))
    assert p.blocked and _level(p.checks, "request split") == [BLOCK]
    assert "purchase request" in _checks(p.checks, "request split")[0].message
    assert _level(p.checks, "split_purchase") == [PASS]  # v1's tiers aren't the issue
    # Another requester is caught too; another supplier or cost centre isn't related.
    other = as_user(erp, policy, REQ2)
    assert other.preview("create_purchase_order", po_args(qty=3, rate=1000)).blocked
    assert not svc.preview("create_purchase_order",
                           po_args(supplier="SUP-CANON", qty=3, rate=1000)).blocked
    assert not svc.preview("create_purchase_order", po_args(cc="IT", qty=3, rate=1000)).blocked
    # Made from a request, the second order is fine.
    rid = approved_request(erp, qty=3)
    assert not svc.preview("create_purchase_order",
                           po_args(qty=3, rate=1000, request=rid)).blocked


def test_po_above_request_qty_blocked(erp, svc):
    rid = approved_request(erp, qty=8)
    p = svc.preview("create_purchase_order", po_args(qty=9, rate=1000, request=rid))
    assert _level(p.checks, "request approved") == [BLOCK] and p.code is None
    assert "8 approved" in _checks(p.checks, "request approved")[0].message
    run(svc, "create_purchase_order", po_args(qty=5, rate=1000, request=rid))
    p = svc.preview("create_purchase_order", po_args(qty=4, rate=1000, request=rid))
    assert _level(p.checks, "request approved") == [BLOCK]
    assert "3 of 8" in _checks(p.checks, "request approved")[0].message
    assert not svc.preview("create_purchase_order",
                           po_args(qty=3, rate=1000, request=rid)).blocked


def test_30k_one_quote_blocked(erp, svc):
    rid = approved_request(erp, qty=30)
    quoted(erp, suppliers=("SUP-HERC",))
    p = svc.preview("create_purchase_order", po_args(qty=30, rate=1000, request=rid))
    assert p.blocked and _level(p.checks, "competitive quotes") == [BLOCK]
    assert "Found 1" in _checks(p.checks, "competitive quotes")[0].message


def test_30k_three_quotes_ok(erp, svc):
    rid = approved_request(erp, qty=30)
    args = po_args(qty=30, rate=1000, request=rid)
    quoted(erp, suppliers=("SUP-HERC", "SUP-CANON"))
    quoted(erp, suppliers=("SUP-MGR",), on=TODAY - timedelta(days=91))  # too old
    assert _level(svc.preview("create_purchase_order", args).checks,
                  "competitive quotes") == [BLOCK]
    quoted(erp, suppliers=("SUP-MGR",), on=TODAY - timedelta(days=90))
    p = svc.preview("create_purchase_order", args)
    assert not p.blocked, _blocks(p.checks)
    assert _level(p.checks, "competitive quotes") == [PASS]
    out = svc.execute("create_purchase_order", args, p.code)
    assert out.outcome == "written" and erp.get_order(out.target).request == rid


def test_quotes_must_cover_every_item_and_include_the_supplier(erp, svc):
    rid = approved_request(erp, qty=30)
    args = po_args(qty=30, rate=1000, request=rid)
    quoted(erp, suppliers=("SUP-CANON", "SUP-MGR", "SUP-HOLD"))
    res = svc.preview("create_purchase_order", args).checks
    assert _level(res, "competitive quotes") == [BLOCK]
    assert "SUP-HERC" in _checks(res, "competitive quotes")[0].message  # it didn't quote
    # Draft quotes and quotes for other items don't count.
    erp.quotes["SQ-D"] = Quote("SQ-D", "SUP-HERC", None, DocState.DRAFT,
                               (line("SALT-TON", 1, 900),), TODAY)
    erp.quotes["SQ-T"] = Quote("SQ-T", "SUP-HERC", None, DocState.SUBMITTED,
                               (line("TONER-K", 1, 80),), TODAY)
    assert svc.preview("create_purchase_order", args).blocked
    erp.quotes["SQ-D"] = replace(erp.quotes["SQ-D"], state=DocState.SUBMITTED)
    assert not svc.preview("create_purchase_order", args).blocked


def test_sole_source_manager_blocked(erp, svc, policy):
    rid = approved_request(erp, qty=30)
    args = po_args(qty=30, rate=1000, request=rid,
                   sole_source_reason="Only certified supplier for this salt grade.")
    p = svc.preview("create_purchase_order", args)
    assert not p.blocked, _blocks(p.checks)
    assert _level(p.checks, "competitive quotes") == [PASS]
    po = run(svc, "create_purchase_order", args)
    assert erp.get_order(po).sole_source_reason.startswith("Only certified")
    shown = render.order(erp.get_order(po))  # what get_purchase_order shows
    assert f"Made from purchase request {rid}." in shown and "Sole-source reason" in shown
    run(svc, "submit_for_approval", {"po": po})
    p = as_user(erp, policy, MGR).preview("approve_purchase_order", {"po": po})
    assert _level(p.checks, "sole source") == [BLOCK] and p.code is None
    assert _level(p.checks, "approval_authority") == [PASS]  # $30K is within the limit
    assert erp.get_order(po).status == Status.PENDING


def test_sole_source_director_ok(erp, svc, policy):
    rid = approved_request(erp, qty=30)
    po = run(svc, "create_purchase_order", po_args(
        qty=30, rate=1000, request=rid, sole_source_reason="Only certified supplier."))
    run(svc, "submit_for_approval", {"po": po})
    run(as_user(erp, policy, DIR), "approve_purchase_order", {"po": po})
    assert erp.get_order(po).status == Status.APPROVED


def test_po_rate_above_quote_blocked(erp, svc):
    [q] = quoted(erp, rate=950, suppliers=("SUP-HERC",))
    p = svc.preview("create_purchase_order", po_args(rate=1000, quotations=[q]))
    assert _level(p.checks, "quote price") == [BLOCK] and p.code is None
    assert "950" in _checks(p.checks, "quote price")[0].message
    po = run(svc, "create_purchase_order", po_args(rate=950, quotations=[q]))
    assert erp.get_order(po).quotations == (q,)
    # Another supplier's quote, a draft one, a missing one, an item not quoted.
    [other] = quoted(erp, rate=2000, suppliers=("SUP-CANON",))
    erp.quotes["SQ-D"] = Quote("SQ-D", "SUP-HERC", None, DocState.DRAFT,
                               (line("SALT-TON", 1, 2000),), TODAY)
    for qs, why in (([other], "SUP-CANON"), (["SQ-D"], "Draft"), (["SQ-NOPE"], "No supplier"),
                    ([q], "TONER-K")):
        args = po_args(rate=500, quotations=qs)
        if why == "TONER-K":
            args["lines"].append({"item_code": "TONER-K", "qty": 1, "rate": 80})
        res = svc.preview("create_purchase_order", args).checks
        assert _level(res, "quote price") == [BLOCK], why
        assert why in _checks(res, "quote price")[0].message, why


def test_request_approval_tier(erp, policy):
    rid = _raised(erp, policy, (("LAPTOP-14", 50),), cc="IT")  # 50 x $1,200 = $60,000
    _step(erp, policy, REQ, Op.WORKFLOW, MR, rid, SUBMIT_FOR_APPROVAL)
    res = _wf(erp, policy, MGR, rid, APPROVE)
    assert _level(res, "request approval tier") == [BLOCK]
    assert "60,000" in _checks(res, "request approval tier")[0].message
    assert "director" in _checks(res, "request approval tier")[0].message
    own = _wf(erp, policy, REQ, rid, APPROVE)  # the requester: not an approver, and raised it
    assert _level(own, "request approval tier") == [BLOCK]
    assert erp.get_request(rid).state == DocState.PENDING
    _step(erp, policy, DIR, Op.WORKFLOW, MR, rid, APPROVE)
    assert erp.get_request(rid).state == DocState.APPROVED


# --- the pure checks -------------------------------------------------------------------

def test_request_split_pure(policy):
    mine = _order("(new)", 3000)
    recent = [_order("PO-A", 1000), _order("PO-B", 1500, request="MR-1"),
              _order("PO-C", 900, status=Status.CANCELLED), _order("PO-D", 700, days=15),
              _order("PO-E", 600, supplier="SUP-CANON"), _order("PO-F", 999, cc="IT")]
    r = P.request_split(mine, recent, policy, TODAY)
    assert r.level == PASS and r.evidence["related_orders"] == ["PO-A"]  # $4,000
    r = P.request_split(mine, [*recent, _order("PO-G", 1000, status=Status.APPROVED)],
                        policy, TODAY)
    assert r.level == BLOCK and r.evidence["related_orders"] == ["PO-A", "PO-G"]
    assert P.request_split(replace(mine, request="MR-1"), recent + [_order("PO-H", 4000)],
                           policy, TODAY).level == PASS
    assert P.request_split(_order("(new)", 5000), recent, policy, TODAY).level == PASS


def test_request_quantity_pure():
    req = Request("MR-1", REQ2, "Fleet", DocState.APPROVED,
                  (RequestLine("SALT-TON", D(10)), RequestLine("TONER-K", D(4))), TODAY)
    others = [_order("PO-A", 6000, "MR-1", qty=6), _order("PO-B", 2000, "MR-1", qty=2,
                                                           status=Status.CANCELLED),
              _order("PO-C", 3000, "MR-2", qty=3)]
    ok = _order("(new)", 4000, "MR-1", qty=4)
    assert P.request_quantity(ok, req, others).level == PASS
    over = P.request_quantity(_order("(new)", 5000, "MR-1", qty=5), req, others)
    assert over.level == BLOCK and "4 of 10" in over.message
    # The order itself (an amendment) doesn't count against itself.
    assert P.request_quantity(_order("PO-A", 10000, "MR-1", qty=10), req, others).level == PASS
    for bad, why in ((None, "No purchase request"),
                     (replace(req, state=DocState.REJECTED), "Rejected"),
                     (replace(req, cost_centre="IT"), "cost centre IT")):
        r = P.request_quantity(ok, bad, others)
        assert r.level == BLOCK and why in r.message, why
    laptop = _order("(new)", 1200, "MR-1", item="LAPTOP-14")
    assert "not on MR-1" in P.request_quantity(laptop, req, others).message
    assert P.request_quantity(_order("(new)", 1), None, others).level == PASS  # no request


def test_request_authority_pure(policy):
    req = Request("MR-1", REQ, "Fleet", DocState.PENDING,
                  (RequestLine("SALT-TON", D(10)), RequestLine("LAPTOP-14", D(2))), TODAY)
    prices = {"SALT-TON": D(100), "LAPTOP-14": D(1200)}
    assert P.request_authority(MGR, req, prices, policy).level == PASS  # $3,400
    assert P.request_authority(REQ, req, prices, policy).level == BLOCK
    assert P.request_authority(REQ2, req, prices, policy).level == BLOCK  # no authority
    mine = replace(req, requester=MGR)
    assert "raised" in P.request_authority(MGR, mine, prices, policy).message
    unpriced = P.request_authority(MGR, req, {"SALT-TON": D(100)}, policy)
    assert unpriced.level == WARN and "LAPTOP-14" in unpriced.message


def test_sole_source_approver_pure(policy):
    po = _order("PO-1", 30000, sole_source_reason="only one maker")
    assert P.sole_source_approver(MGR, po, policy).level == BLOCK
    assert P.sole_source_approver(DIR, po, policy).level == PASS
    assert P.sole_source_approver(REQ, po, policy).level == BLOCK
    assert P.sole_source_approver(MGR, replace(po, sole_source_reason=""), policy).level \
        == PASS


# --- raising a request and its workflow ------------------------------------------------

def test_raise_purchase_request(erp, policy):
    svc = as_user(erp, policy, REQ)
    args = {"cost_centre": "IT", "lines": [{"item_code": "LAPTOP-14", "qty": 5}],
            "notes": "new starters"}
    p = svc.preview("raise_purchase_request", args)
    assert not p.blocked and p.code
    assert "$6,000.00" in p.summary and "manager" in p.summary
    rid = svc.execute("raise_purchase_request", args, p.code).target
    req = erp.get_request(rid)
    assert (req.requester, req.cost_centre, req.state, req.lines) == (
        REQ, "IT", DocState.DRAFT, (RequestLine("LAPTOP-14", D(5)),))
    bad = {"cost_centre": "Nowhere", "lines": [{"item_code": "NOPE", "qty": 1}],
           "notes": "Wire to IBAN GB29 NWBK 6016 1331 9268 19"}
    res = svc.preview("raise_purchase_request", bad).checks
    assert {r.check for r in res if r.level == BLOCK} >= {"cost centre", "items_known",
                                                          "no_bank_details"}
    for lines in ([{"item_code": "TONER-K", "qty": 0}],
                  [{"item_code": "TONER-K", "qty": 1}, {"item_code": "TONER-K", "qty": 2}],
                  [{"item_code": "TONER-K", "qty": 1, "rate": 80}], []):
        with pytest.raises(InvalidArgs):
            svc.preview("raise_purchase_request", {"cost_centre": "IT", "lines": lines})


def test_request_workflow_rules(erp, policy):
    rid = _raised(erp, policy)
    assert _level(_wf(erp, policy, REQ2, rid, SUBMIT_FOR_APPROVAL), "ownership") == [BLOCK]
    assert _level(_wf(erp, policy, MGR, rid, APPROVE), "request state") == [BLOCK]
    _step(erp, policy, REQ, Op.WORKFLOW, MR, rid, SUBMIT_FOR_APPROVAL)
    assert erp.get_request(rid).state == DocState.PENDING
    assert _wf(erp, policy, MGR, rid, REJECT) and _blocks(_wf(erp, policy, MGR, rid, REJECT))
    assert _blocks(_wf(erp, policy, REQ2, rid, REJECT, "no"))  # not an approver
    _step(erp, policy, MGR, Op.WORKFLOW, MR, rid, REJECT, "split the PPE out")
    assert erp.get_request(rid).state == DocState.REJECTED
    assert _level(_wf(erp, policy, REQ2, rid, REVISE), "ownership") == [BLOCK]
    _step(erp, policy, REQ, Op.WORKFLOW, MR, rid, REVISE)
    assert erp.get_request(rid).state == DocState.DRAFT
    res = _wf(erp, policy, REQ, rid, "Withdraw")
    assert _blocks(res) and "Submit for Approval" in _blocks(res)[0]
    res = _wf(erp, policy, REQ, "MR-NOPE", SUBMIT_FOR_APPROVAL)
    assert "No purchase request" in _blocks(res)[0]
    # Approval re-runs the request's own checks: an item disabled since is caught.
    _step(erp, policy, REQ, Op.WORKFLOW, MR, rid, SUBMIT_FOR_APPROVAL)
    erp.items.discard("SALT-TON")
    assert _level(_wf(erp, policy, MGR, rid, APPROVE), "items_known") == [BLOCK]


def test_unpriced_request_warns_but_can_be_approved(erp, policy):
    rid = _raised(erp, policy, (("CONSULT-HR", 40),))  # no purchase history
    _step(erp, policy, REQ, Op.WORKFLOW, MR, rid, SUBMIT_FOR_APPROVAL)
    res = _step(erp, policy, MGR, Op.WORKFLOW, MR, rid, APPROVE)
    assert _level(res, "request approval tier") == [WARN]
    assert erp.get_request(rid).state == DocState.APPROVED


def test_request_tools_need_a_p2p_connector(policy):
    from erp_guard.connectors.sap import SapS4
    svc = Service(object.__new__(SapS4), policy)
    svc._actor = REQ
    p = svc.preview("raise_purchase_request", {"cost_centre": "IT", "lines": [
        {"item_code": "TG11", "qty": 1}]})
    assert _level(p.checks, "connector") == [BLOCK]


# --- quotes -----------------------------------------------------------------------------

def test_request_quotes_and_submit(erp, policy):
    svc = as_user(erp, policy, REQ)
    draft = _raised(erp, policy)
    res = svc.preview("request_quotes", {"request": draft, "suppliers": ["SUP-HERC"]}).checks
    assert _level(res, "request state") == [BLOCK]
    rid = _approved(erp, policy)
    res = svc.preview("request_quotes", {"request": rid,
                                         "suppliers": ["SUP-HERC", "SUP-HOLD"]}).checks
    assert _level(res, "supplier_usable") == [PASS, BLOCK]
    with pytest.raises(InvalidArgs):
        svc.preview("request_quotes", {"request": rid, "suppliers": ["SUP-HERC", "SUP-HERC"]})
    with pytest.raises(InvalidArgs):
        svc.preview("request_quotes", {"request": rid, "suppliers": []})
    rfq = run(svc, "request_quotes", {"request": rid,
                                      "suppliers": ["SUP-HERC", "SUP-CANON", "SUP-MGR"]})
    got = erp.get_rfq(rfq)
    assert (got.request, got.suppliers, got.state, got.created_by) == (
        rid, ("SUP-HERC", "SUP-CANON", "SUP-MGR"), DocState.DRAFT, REQ)
    res = _write(erp, policy, REQ2, Op.SUBMIT, RFQ, rfq)[3]
    assert _level(res, "ownership") == [BLOCK]
    _step(erp, policy, REQ, Op.SUBMIT, RFQ, rfq)
    assert erp.get_rfq(rfq).state == DocState.SUBMITTED
    assert _blocks(_write(erp, policy, REQ, Op.SUBMIT, RFQ, rfq)[3])  # not a draft now
    assert "No request for quotation" in _blocks(
        _write(erp, policy, REQ, Op.SUBMIT, RFQ, "RFQ-NOPE")[3])[0]


def _rfq(erp, policy) -> tuple[str, str]:
    rid = _approved(erp, policy, (("SALT-TON", 30),))
    svc = as_user(erp, policy, REQ)
    rfq = run(svc, "request_quotes", {"request": rid,
                                      "suppliers": ["SUP-HERC", "SUP-CANON", "SUP-MGR"]})
    _step(erp, policy, REQ, Op.SUBMIT, RFQ, rfq)
    return rid, rfq


def _quote_args(supplier="SUP-HERC", rfq=None, rate=950, qty=30, item="SALT-TON",
                quoted_on=None):
    return {"supplier": supplier, "rfq": rfq, "quoted_on": quoted_on or TODAY.isoformat(),
            "lines": [{"item_code": item, "qty": qty, "rate": rate}]}


def test_enter_supplier_quote_and_submit(erp, policy):
    rid, rfq = _rfq(erp, policy)
    svc = as_user(erp, policy, REQ)
    for args, check in ((_quote_args(rfq=rfq, quoted_on=(TODAY + timedelta(days=1))
                                     .isoformat()), "quote date"),
                        (_quote_args("SUP-HOLD", rfq), "supplier_usable"),
                        (_quote_args("SUP-HOLD"), "supplier_usable"),
                        (_quote_args(rfq=rfq, item="TONER-K"), "request for quotation"),
                        (_quote_args(rfq="RFQ-NOPE"), "request for quotation"),
                        (_quote_args(item="NOPE"), "items_known")):
        res = svc.preview("enter_supplier_quote", args).checks
        assert _level(res, check)[-1:] == [BLOCK], (args, _blocks(res))
    erp.add_supplier(replace(erp.get_supplier("SUP-HERC"), id="SUP-NEW", name="NEW LLC"))
    res = svc.preview("enter_supplier_quote", _quote_args("SUP-NEW", rfq)).checks
    assert "wasn't asked" in _checks(res, "request for quotation")[0].message
    with pytest.raises(InvalidArgs):
        svc.preview("enter_supplier_quote", _quote_args(quoted_on="last week"))
    ids = []
    for s in ("SUP-HERC", "SUP-CANON", "SUP-MGR"):
        q = run(svc, "enter_supplier_quote", _quote_args(s, rfq))
        assert _level(_write(erp, policy, REQ2, Op.SUBMIT, SQ, q)[3], "ownership") == [BLOCK]
        _step(erp, policy, REQ, Op.SUBMIT, SQ, q)
        ids.append(q)
    q = erp.get_quote(ids[0])
    assert (q.supplier_id, q.rfq, q.state, q.entered_by, q.quoted_on) == (
        "SUP-HERC", rfq, DocState.SUBMITTED, REQ, TODAY)
    # A quote without a request for quotation is fine too.
    run(svc, "enter_supplier_quote", _quote_args("SUP-CANON", rate=900))
    # The full step: a $28,500 order from the request and the winning quote.
    args = po_args(qty=30, rate=950, request=rid, quotations=[ids[0]])
    po = run(svc, "create_purchase_order", args)
    assert (erp.get_order(po).request, erp.get_order(po).quotations) == (rid, (ids[0],))


def test_submit_and_amend_rerun_the_request_checks(erp, svc, policy):
    rid = approved_request(erp, qty=8)
    po = run(svc, "create_purchase_order", po_args(qty=8, rate=1000, request=rid))
    erp.requests[rid] = replace(erp.requests[rid], state=DocState.CANCELLED)
    p = svc.preview("submit_for_approval", {"po": po})
    assert _level(p.checks, "request required") == [BLOCK]
    erp.requests[rid] = replace(erp.requests[rid], state=DocState.APPROVED)
    assert not svc.preview("submit_for_approval", {"po": po}).blocked
    # A small order amended up to $8,000 needs the request passed; omitted, it has none.
    small = run(svc, "create_purchase_order", po_args(supplier="SUP-CANON", qty=1,
                                                      rate=1000))
    lines = [{"item_code": "SALT-TON", "qty": 8, "rate": 1000}]
    p = svc.preview("amend_purchase_order", {"po": small, "lines": lines})
    assert _level(p.checks, "request required") == [BLOCK]
    rid2 = approved_request(erp, qty=8)
    run(svc, "amend_purchase_order", {"po": small, "lines": lines, "request": rid2})
    assert erp.get_order(small).request == rid2
    run(svc, "amend_purchase_order", {"po": po, "lines": lines[:1], "request": rid})
    assert erp.get_order(po).request == rid  # it doesn't count against itself


def test_sole_source_reason_is_free_text_checked(svc, erp):
    rid = approved_request(erp, qty=30)
    p = svc.preview("create_purchase_order", po_args(
        qty=30, rate=1000, request=rid, sole_source_reason="Routing 071000013 account 1234567"))
    assert _level(p.checks, "no_bank_details") == [BLOCK]


# --- routing through the generic tools ---------------------------------------------------

@pytest.mark.parametrize(("op", "doctype", "tool"), [
    (Op.CREATE, MR, "raise_purchase_request"), (Op.SUBMIT, MR, "workflow_action"),
    (Op.CREATE, RFQ, "request_quotes"), (Op.CREATE, SQ, "enter_supplier_quote")])
def test_other_operations_name_the_tool(erp, policy, op, doctype, tool):
    res = _write(erp, policy, REQ, op, doctype, "X-1")[3]
    assert _level(res, "pack") == [BLOCK] and tool in res[0].message


def test_workflow_and_submit_through_the_fake_erpnext(policy):
    """Over ERPNext (the fake): the request's workflow and the quote steps reach the pack's
    checks through workflow_action and submit_document, and the order is built from
    ERPNext's mappers with the links back."""
    fake = FakeErpNext(REQ)
    fake.workflows.add(MR)
    erp = connector(fake)
    svc = Service(erp, policy, today=lambda: TODAY)
    fake.add({"doctype": MR, "name": "MAT-MR-1", "owner": REQ, "docstatus": 0,
              "workflow_state": "Pending Approval", "transaction_date": TODAY.isoformat(),
              "company": "Lakeside Demo Utilities",
              "items": [{"item_code": "LAPTOP-14", "qty": 5, "cost_center": "IT - LDU"}]})
    fake.add({"doctype": "Item", "name": "LAPTOP-14", "last_purchase_rate": 1200})
    fake.add({"doctype": "Cost Center", "name": "IT - LDU", "erp_guard_annual_budget": 1})
    args = {"doctype": MR, "name": "MAT-MR-1", "action": APPROVE}
    p = svc.preview("workflow_action", args)
    assert _level(p.checks, "request approval tier") == [BLOCK]  # the requester
    fake.user = MGR
    mgr = Service(connector(fake), policy, today=lambda: TODAY)
    p = mgr.preview("workflow_action", args)
    assert not p.blocked, _blocks(p.checks)
    assert mgr.execute("workflow_action", args, p.code).outcome == "written"
    assert fake.docs[(MR, "MAT-MR-1")]["docstatus"] == 1

    fake.add({"doctype": RFQ, "name": "RFQ-1", "owner": REQ, "docstatus": 0,
              "items": [{"item_code": "LAPTOP-14", "qty": 5, "material_request": "MAT-MR-1"}],
              "suppliers": [{"supplier": "SUP-A"}]})
    fake.add({"doctype": "Supplier", "name": "SUP-A", "supplier_name": "A LLC"})
    sub = {"doctype": RFQ, "name": "RFQ-1"}
    assert _level(mgr.preview("submit_document", sub).checks, "ownership") == [BLOCK]
    fake.user = REQ
    p = svc.preview("submit_document", sub)
    assert not p.blocked, _blocks(p.checks)
    assert svc.execute("submit_document", sub, p.code).outcome == "written"
    assert fake.docs[(RFQ, "RFQ-1")]["docstatus"] == 1
    assert erp.get_rfq("RFQ-1").suppliers == ("SUP-A",)


def test_erpnext_order_rows_come_from_the_mappers():
    fake = FakeErpNext(REQ)
    erp = connector(fake, writable=lambda d: d == "Purchase Order")
    fake.mapped["erpnext.stock.doctype.material_request.material_request.make_purchase_order"] \
        = {"doctype": "Purchase Order", "items": [
            {"doctype": "Purchase Order Item", "item_code": "LAPTOP-14", "qty": 5,
             "conversion_factor": 1, "material_request": "MAT-MR-1",
             "material_request_item": "mri-1", "warehouse": "Stores - LDU"}]}
    fake.mapped["erpnext.buying.doctype.supplier_quotation.supplier_quotation."
                "make_purchase_order"] = {"doctype": "Purchase Order", "items": [
                    {"doctype": "Purchase Order Item", "item_code": "LAPTOP-14", "qty": 5,
                     "conversion_factor": 1, "supplier_quotation": "SQ-1",
                     "supplier_quotation_item": "sqi-1", "discount_percentage": 5}]}
    o = erp.create_order("SUP-A", "IT - LDU", [line("LAPTOP-14", 4, 1100)], "",
                         TODAY + timedelta(days=14), request="MAT-MR-1", quotations=["SQ-1"],
                         sole_source_reason="")
    body = fake.docs[("Purchase Order", o.id)]
    [row] = body["items"]
    assert (row["material_request"], row["material_request_item"], row["supplier_quotation"],
            row["supplier_quotation_item"]) == ("MAT-MR-1", "mri-1", "SQ-1", "sqi-1")
    assert (row["qty"], row["rate"], row["stock_qty"], row["discount_percentage"],
            row["cost_center"]) == (4, 1100, 4, 0, "IT - LDU")
    assert (o.request, o.quotations) == ("MAT-MR-1", ("SQ-1",))
    assert "erp_guard_sole_source_reason" not in body
    o = erp.create_order("SUP-A", "IT - LDU", [line("LAPTOP-14", 4, 1100)], "",
                         TODAY, request="MAT-MR-1", sole_source_reason="only maker")
    body = fake.docs[("Purchase Order", o.id)]
    assert body["items"][0]["warehouse"] == "Stores - LDU"
    assert (o.sole_source_reason, o.quotations) == ("only maker", ())
    with pytest.raises(Exception, match="Not on"):
        erp.create_order("SUP-A", "IT - LDU", [line("TONER-K", 1, 80)], "", TODAY,
                         request="MAT-MR-1")
