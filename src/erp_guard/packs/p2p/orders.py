"""Suppliers and purchase orders: create, onboard, submit, approve, reject, cancel, amend.

An order of $5,000 or more is made from an approved purchase request, and one of $25,000
or more needs competing supplier quotations or a sole-source reason (packs.p2p.requests
raises the requests and enters the quotes). Those checks run on create, submit and amend;
a sole-source order is approved from the director tier up.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from erp_guard import checks as C
from erp_guard.models import LIVE_STATUSES, Line, PurchaseOrder, Snapshot, Status
from erp_guard.packs.p2p import checks as P
from erp_guard.service import Action, InvalidArgs, _lines, _text, canonicaliser

if TYPE_CHECKING:
    from erp_guard.service import Service

Result = tuple[str, list[C.CheckResult]]


# --- shared helpers ------------------------------------------------------------------

def _po(a: dict) -> str:
    return _text(a, "po", required=True, max_len=140)


def _quotations(a: dict) -> list[str]:
    """Supplier quotation IDs, each once, in a canonical order."""
    raw = a.get("quotations")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InvalidArgs("'quotations' must be a list of supplier quotation IDs")
    out: set[str] = set()
    for i, v in enumerate(raw):
        if not isinstance(v, str) or not v.strip() or len(v.strip()) > 140:
            raise InvalidArgs(f"quotation {i + 1} must be a supplier quotation ID")
        out.add(v.strip())
    return sorted(out)


# What an order is made from; on create and amend alike (an amendment gives them again).
_LINK_SPEC = {
    "request": lambda a: _text(a, "request", max_len=140),
    "quotations": _quotations,
    "sole_source_reason": lambda a: _text(a, "sole_source_reason"),
}


def _proposed(svc: Service, canon: dict, base: PurchaseOrder | None = None) -> PurchaseOrder:
    lines = tuple(Line(ln["item_code"], Decimal(ln["qty"]), Decimal(ln["rate"]),
                       ln["description"]) for ln in canon["lines"])
    links = {"request": canon.get("request") or None,
             "quotations": tuple(canon.get("quotations") or ()),
             "sole_source_reason": canon.get("sole_source_reason", "")}
    if base is not None:
        return PurchaseOrder(base.id, base.supplier_id, base.cost_centre, base.requester,
                             base.status, base.order_date, lines, canon.get("notes", ""),
                             **links)
    return PurchaseOrder("(new)", canon["supplier"], canon["cost_centre"], svc.actor,
                         Status.DRAFT, svc.today(), lines, canon.get("notes", ""), **links)


def _links(order: PurchaseOrder) -> dict:
    return {"request": order.request, "quotations": list(order.quotations),
            "sole_source_reason": order.sole_source_reason}


def _duplicate_lines(order: PurchaseOrder) -> C.CheckResult:
    """Each item on one line: receipts and invoices are matched to the order per item."""
    seen: set[str] = set()
    dup = sorted({ln.item_code for ln in order.lines
                  if ln.item_code in seen or seen.add(ln.item_code)})
    if dup:
        return C.CheckResult(
            "duplicate_line", C.BLOCK,
            f"{', '.join(dup)} is on more than one line; put each item on one line "
            "(receipts and invoices are matched to the order per item).", {"items": dup})
    return C.CheckResult("duplicate_line", C.PASS, "Each item is on one line.")


def _request_checks(svc: Service, order: PurchaseOrder,
                    recent: list[PurchaseOrder]) -> list[C.CheckResult]:
    """The purchase request and quote rules, against fresh ERP data. A connector that
    doesn't model requests (SAP, for now) can't show an order of the request threshold or
    more was requested, so those are refused there."""
    erp, policy, today = svc.erp, svc.policy, svc.today()
    if not hasattr(erp, "get_request"):
        if order.request or order.quotations or order.sole_source_reason or \
                order.total >= policy.request_required_from:
            return [C.CheckResult(
                "connector", C.BLOCK, f"{erp.name} doesn't support purchase requests and "
                "quotes yet, so it takes only orders under "
                f"{P.usd(policy.request_required_from)} made from neither.")]
        return [P.request_split(order, recent, policy, today)]
    request = erp.get_request(order.request) if order.request else None
    from_request = [o for o in erp.list_orders(since=request.created)
                    if o.request == request.id] if request else []
    quotes, suppliers, own = [], {}, []
    if order.total >= policy.quotes.threshold and not order.sole_source_reason:
        quotes = erp.list_quotes(item_codes=sorted({ln.item_code for ln in order.lines}),
                                 since=today - timedelta(days=policy.quotes.lookback_days))
        suppliers = {s: erp.get_supplier(s) for s in sorted({q.supplier_id for q in quotes})}
        own = [q for q in P.counted_quotes(order, quotes, policy, today, suppliers)[0]
               if q.supplier_id == order.supplier_id]
    linked = {q: erp.get_quote(q) for q in order.quotations}
    return [P.request_required(order, request, policy),
            P.request_split(order, recent, policy, today),
            P.request_quantity(order, request, from_request),
            P.competitive_quotes(order, quotes, policy, today, suppliers=suppliers),
            P.quote_price(order, linked, own)]


def _order_checks(svc: Service, order: PurchaseOrder, with_prices: bool,
                  with_requests: bool = True) -> list[C.CheckResult]:
    """Split, budget, the request and quote rules (unless left out, as at approval) and
    (for new lines) items, one line per item and price, against fresh ERP data."""
    erp, policy, today = svc.erp, svc.policy, svc.today()
    since = min(today - timedelta(days=policy.split_window_days), svc.fy_start())
    cc_orders = erp.list_orders(cost_centre=order.cost_centre, since=since)
    snap = Snapshot(actor=svc.actor, today=today,
                    cost_centre=erp.get_cost_centre(order.cost_centre))
    snap.committed = sum(
        (o.total for o in cc_orders
         if o.id != order.id and o.status in LIVE_STATUSES
         and o.order_date >= svc.fy_start()),
        Decimal(0),
    )
    out = [C.split_purchase(order, cc_orders, policy, today), C.budget(order, snap)]
    if with_requests:
        out += _request_checks(svc, order, cc_orders)
    if with_prices:
        out[:0] = [C.items_known(order, erp.item_codes()), _duplicate_lines(order)]
        prices = erp.last_purchase_prices(sorted({ln.item_code for ln in order.lines}))
        out.append(C.price_vs_last(order, prices, policy))
    return out


def _texts(canon: dict) -> dict[str, str]:
    texts = {k: canon[k] for k in ("notes", "name", "reason", "comment",
                                   "sole_source_reason") if canon.get(k)}
    for i, ln in enumerate(canon.get("lines", [])):
        if ln["description"]:
            texts[f"line {i + 1} description"] = ln["description"]
    return texts


def _describe_lines(order: PurchaseOrder) -> str:
    return ", ".join(f"{ln.qty} x {ln.item_code} @ ${ln.rate:,.2f}" for ln in order.lines)


def _describe_links(order: PurchaseOrder) -> str:
    out = []
    if order.request:
        out.append(f" From purchase request {order.request}.")
    if order.quotations:
        out.append(f" From supplier quotation(s) {', '.join(order.quotations)}.")
    if order.sole_source_reason:
        out.append(" Sole source (no competing quotes): approved by a director or above.")
    return "".join(out)


# --- onboard_supplier ----------------------------------------------------------------

def _eval_onboard(svc: Service, canon: dict) -> Result:
    res = [C.duplicate_supplier(canon["name"], canon["tax_id"] or None,
                                svc.erp.list_suppliers(), svc.policy)]
    res += C.free_text(_texts(canon))
    tid = f", tax ID {canon['tax_id']}" if canon["tax_id"] else ""
    return f"Create supplier '{canon['name']}'{tid}, onboarded by {svc.actor}.", res


def _write_onboard(svc: Service, canon: dict) -> tuple[str, str]:
    s = svc.erp.create_supplier(canon["name"], canon["tax_id"] or None, canon["notes"],
                                svc.actor)
    return s.id, f"Supplier {s.name} created as {s.id}."


# --- create_purchase_order -----------------------------------------------------------

def _eval_create(svc: Service, canon: dict) -> Result:
    actor = svc.actor
    order = _proposed(svc, canon)
    sup = svc.erp.get_supplier(order.supplier_id)
    res = [C.supplier_usable(sup, order.supplier_id),
           C.self_dealing(actor, sup, "requester")]
    res += _order_checks(svc, order, with_prices=True)
    res += C.free_text(_texts(canon))
    name = sup.name if sup else order.supplier_id
    return (f"Create draft PO to {name} ({order.supplier_id}) on cost centre "
            f"{order.cost_centre}: {_describe_lines(order)}; total "
            f"${order.total:,.2f}, needs {svc.policy.required_tier(order.total).name} "
            f"approval. Requester: {actor}.{_describe_links(order)}"), res


def _write_create(svc: Service, canon: dict) -> tuple[str, str]:
    order = _proposed(svc, canon)
    req = date.fromisoformat(canon["required_by"]) if canon["required_by"] \
        else svc.today() + timedelta(days=14)
    po = svc.erp.create_order(order.supplier_id, order.cost_centre, list(order.lines),
                              canon["notes"], req, **_links(order))
    return po.id, f"Draft purchase order {po.id} created (${po.total:,.2f})."


# --- submit_for_approval -------------------------------------------------------------

def _eval_submit(svc: Service, canon: dict) -> Result:
    ref = canon["po"]
    order = svc.erp.get_order(ref)
    res = [C.state(order, ref, {Status.DRAFT, Status.REJECTED}, "submitting")]
    if order is not None:
        res += _order_checks(svc, order, with_prices=False)
    return f"Submit {ref} for approval.", res


def _write_submit(svc: Service, canon: dict) -> tuple[str, str]:
    po_id = canon["po"]
    svc.erp.set_status(po_id, Status.PENDING, "")
    return po_id, f"{po_id} submitted for approval."


# --- approve / reject ----------------------------------------------------------------

def _eval_decision(svc: Service, canon: dict, verb: str) -> Result:
    actor, ref = svc.actor, canon["po"]
    order = svc.erp.get_order(ref)
    res = [C.state(order, ref, {Status.PENDING},
                   "approval" if verb == "approve" else "rejection")]
    if order is not None:
        res.append(C.segregation_of_duties(actor, order))
        if verb == "approve":
            res.append(C.approval_authority(actor, order, svc.policy))
            res.append(P.sole_source_approver(actor, order, svc.policy))
            res.append(C.self_dealing(actor, svc.erp.get_supplier(order.supplier_id),
                                      "approver"))
            res += _order_checks(svc, order, with_prices=False, with_requests=False)
    res += C.free_text(_texts(canon))
    total = f" (${order.total:,.2f})" if order else ""
    return f"{verb.capitalize()} {ref}{total} as {actor}.", res


def _eval_approve(svc: Service, canon: dict) -> Result:
    return _eval_decision(svc, canon, "approve")


def _eval_reject(svc: Service, canon: dict) -> Result:
    return _eval_decision(svc, canon, "reject")


def _write_approve(svc: Service, canon: dict) -> tuple[str, str]:
    po_id = canon["po"]
    svc.erp.approve_order(po_id)
    return po_id, f"{po_id} approved."


def _write_reject(svc: Service, canon: dict) -> tuple[str, str]:
    po_id = canon["po"]
    svc.erp.set_status(po_id, Status.REJECTED, canon["reason"])
    return po_id, f"{po_id} rejected."


# --- cancel_purchase_order -----------------------------------------------------------

def _eval_cancel(svc: Service, canon: dict) -> Result:
    actor, ref = svc.actor, canon["po"]
    order = svc.erp.get_order(ref)
    res = [C.state(order, ref, LIVE_STATUSES | {Status.REJECTED}, "cancelling")]
    if order is not None and order.closed:  # closed stays "approved": say so
        res = [C.CheckResult("state", C.BLOCK, f"{ref} is closed; a closed order is neither "
                             "cancelled nor amended.")]
    if order is not None and order.requester != actor and \
            svc.policy.approver_limit(actor) is None:
        res.append(C.CheckResult("ownership", C.BLOCK,
                                 f"Only the requester or an approver can cancel {ref}."))
    res += C.free_text(_texts(canon))
    return f"Cancel {ref}. Nothing is deleted; the order stays on record.", res


def _write_cancel(svc: Service, canon: dict) -> tuple[str, str]:
    po_id = canon["po"]
    svc.erp.cancel_order(po_id, canon["reason"])
    return po_id, f"{po_id} cancelled."


# --- amend_purchase_order ------------------------------------------------------------

def _eval_amend(svc: Service, canon: dict) -> Result:
    actor, ref = svc.actor, canon["po"]
    order = svc.erp.get_order(ref)
    res = [C.state(order, ref, {Status.DRAFT, Status.PENDING, Status.REJECTED},
                   "amending")]
    if order is not None:
        if order.requester != actor:
            res.append(C.CheckResult("ownership", C.BLOCK,
                                     f"Only the requester can amend {ref}."))
        new = _proposed(svc, canon, base=order)
        res += _order_checks(svc, new, with_prices=True)
        res += C.free_text(_texts(canon))
        return (f"Amend {ref}: {_describe_lines(new)}; total ${new.total:,.2f} (was "
                f"${order.total:,.2f}). Goes back to Draft and needs resubmitting."
                f"{_describe_links(new)}"), res
    return f"Amend {ref}.", res


def _write_amend(svc: Service, canon: dict) -> tuple[str, str]:
    po_id = canon["po"]
    order = _proposed(svc, canon, base=svc.erp.get_order(po_id))
    svc.erp.amend_order(po_id, list(order.lines), canon["notes"], **_links(order))
    return po_id, f"{po_id} amended and back in Draft."


# --- the actions ---------------------------------------------------------------------

def _action(name: str, spec: dict, evaluate, write, target_key: str | None = None) -> Action:
    return Action(name, canonicaliser(name, spec), evaluate, write, target_key)


ACTIONS: list[Action] = [
    _action("create_purchase_order", {
        "supplier": lambda a: _text(a, "supplier", required=True, max_len=140),
        "cost_centre": lambda a: _text(a, "cost_centre", required=True, max_len=140),
        "lines": _lines,
        "required_by": lambda a: _text(a, "required_by", max_len=10),
        "notes": lambda a: _text(a, "notes"),
        **_LINK_SPEC,
    }, _eval_create, _write_create),
    _action("onboard_supplier", {
        "name": lambda a: _text(a, "name", required=True, max_len=140),
        "tax_id": lambda a: _text(a, "tax_id", max_len=40),
        "notes": lambda a: _text(a, "notes"),
    }, _eval_onboard, _write_onboard),
    _action("submit_for_approval", {"po": _po}, _eval_submit, _write_submit, "po"),
    _action("approve_purchase_order", {
        "po": _po,
        "comment": lambda a: _text(a, "comment"),
    }, _eval_approve, _write_approve, "po"),
    _action("reject_purchase_order", {
        "po": _po,
        "reason": lambda a: _text(a, "reason", required=True),
    }, _eval_reject, _write_reject, "po"),
    _action("cancel_purchase_order", {
        "po": _po,
        "reason": lambda a: _text(a, "reason", required=True),
    }, _eval_cancel, _write_cancel, "po"),
    _action("amend_purchase_order", {
        "po": _po,
        "lines": _lines,
        "notes": lambda a: _text(a, "notes"),
        **_LINK_SPEC,
    }, _eval_amend, _write_amend, "po"),
]
