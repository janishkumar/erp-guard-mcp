"""Purchase requests and supplier quotes: raise_purchase_request, request_quotes and
enter_supplier_quote, plus the request's workflow, the submission of requests for
quotation and supplier quotations, draft edits and cancellation as the generic tools reach
them (workflow_action, submit_document, update_document, cancel_document).

A purchase request (ERPNext: a Material Request of type Purchase) is raised as a draft,
submitted for approval by the person who raised it, and approved through the Material
Request workflow by a purchase approver who didn't raise it and whose limit covers its
estimated value (each item's last purchase price times quantity). Orders of $5,000 or more
are made from an approved request (packs.p2p.orders). Quotes are asked for from an
approved request with a request for quotation, and entered as supplier quotations; neither
has an approval, so each is submitted directly, by the person who entered it. Orders of
$25,000 or more need submitted quotes from three suppliers, or a sole-source reason.

A draft request or quote is edited only by the person who entered it, and only its
quantities (and a quote's rates); the checks it was entered with re-run. A submitted one
is cancelled by the person who entered it or a purchase approver, with a reason, and not
while a live order is made from it. A draft needs no voiding: it counts for nothing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from erp_guard import checks as C
from erp_guard.models import LIVE_STATUSES, Line
from erp_guard.ops import Write
from erp_guard.packs.p2p import APPROVE, REJECT, REVISE, SUBMIT_FOR_APPROVAL
from erp_guard.packs.p2p import checks as P
from erp_guard.packs.p2p.models import (
    POSTED,
    DocState,
    Quote,
    Request,
    RequestLine,
    Rfq,
    not_editable,
)
from erp_guard.service import Action, InvalidArgs, _decimal, _text, canonicaliser

if TYPE_CHECKING:
    from erp_guard.service import Service

Result = tuple[str, list[C.CheckResult]]
REQUEST, RFQ, QUOTE = "Material Request", "Request for Quotation", "Supplier Quotation"
WORKFLOW_ACTIONS = (SUBMIT_FOR_APPROVAL, APPROVE, REJECT, REVISE)


def _block(check: str, message: str, **evidence) -> C.CheckResult:
    return C.CheckResult(check, C.BLOCK, message, evidence)


def _pass(check: str, message: str) -> C.CheckResult:
    return C.CheckResult(check, C.PASS, message)


def _unsupported(svc: Service) -> list[C.CheckResult]:
    """Requests and quotes need a connector that models them (ERPNext, the in-memory
    one)."""
    if hasattr(svc.erp, "get_request"):
        return []
    return [_block("connector", f"{svc.erp.name} doesn't support purchase requests and "
                   "quotes yet.")]


def _free_text(texts: dict[str, str]) -> list[C.CheckResult]:
    return C.free_text({k: v for k, v in texts.items() if v})


def _describe(lines) -> str:
    return ", ".join(f"{ln.qty.normalize():f} x {ln.item_code}"
                     + (f" @ {P.usd(ln.rate)}" if hasattr(ln, "rate") else "")
                     for ln in lines)


# --- canonical arguments ---------------------------------------------------------------

def _positive(row: dict, key: str, i: int) -> str:
    v = _decimal(row.get(key), key)
    if v <= 0:
        raise InvalidArgs(f"line {i + 1}: '{key}' must be more than zero")
    return format(v, "f")


def _item_lines(fields: tuple[str, ...]):
    """[{"item_code", *fields}], each item once, every number more than zero."""
    def canon(args: dict) -> list[dict]:
        raw = args.get("lines")
        if not isinstance(raw, list) or not raw:
            raise InvalidArgs("'lines' must be a non-empty list")
        out, seen = [], set()
        for i, row in enumerate(raw):
            if not isinstance(row, dict):
                raise InvalidArgs(f"line {i + 1} must be an object")
            unknown = set(row) - {"item_code", *fields}
            if unknown:
                raise InvalidArgs(f"line {i + 1} has unknown field(s): "
                                  f"{', '.join(sorted(unknown))}")
            item = _text(row, "item_code", required=True, max_len=140)
            if item in seen:
                raise InvalidArgs(f"{item} is on more than one line; put it on one line")
            seen.add(item)
            out.append({"item_code": item, **{f: _positive(row, f, i) for f in fields}})
        return out
    return canon


def _suppliers(args: dict) -> list[str]:
    raw = args.get("suppliers")
    if not isinstance(raw, list) or not raw:
        raise InvalidArgs("'suppliers' must be a non-empty list of supplier IDs")
    out: list[str] = []
    for i, v in enumerate(raw):
        if not isinstance(v, str) or not v.strip() or len(v.strip()) > 140:
            raise InvalidArgs(f"supplier {i + 1} must be a supplier ID")
        if v.strip() in out:
            raise InvalidArgs(f"{v.strip()} is listed more than once")
        out.append(v.strip())
    return out


def _iso_date(a: dict, key: str) -> str:
    v = _text(a, key, required=True, max_len=10)
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError as exc:
        raise InvalidArgs(f"'{key}' must be an ISO date, e.g. 2026-10-29") from exc


# --- checks shared by entry and the later steps ----------------------------------------

def _items_known(svc: Service, codes: list[str]) -> C.CheckResult:
    missing = sorted(set(codes) - svc.erp.item_codes())
    if missing:
        return _block("items_known", f"Unknown item code(s): {', '.join(missing)}.")
    return _pass("items_known", "All items exist in the item master.")


def _request_checks(svc: Service, req: Request) -> list[C.CheckResult]:
    """What a request is raised with; re-run when it is submitted and approved."""
    cc = svc.erp.get_cost_centre(req.cost_centre)
    return [_pass("cost centre", f"Cost centre {cc.id}.") if cc else
            _block("cost centre", f"No cost centre '{req.cost_centre}' in the ERP."),
            _items_known(svc, [ln.item_code for ln in req.lines])]


def _estimate(svc: Service, req: Request) -> str:
    prices = svc.erp.last_purchase_prices(sorted({ln.item_code for ln in req.lines}))
    value = req.estimated_value(prices)
    tier = svc.policy.required_tier(value, "purchase_request")
    unpriced = sorted({ln.item_code for ln in req.lines if not prices.get(ln.item_code)})
    gap = f" ({', '.join(unpriced)} has no purchase history and is left out)" if unpriced \
        else ""
    return f"estimated {P.usd(value)} at last purchase prices{gap}, so a {tier.name} or " \
           "above approves it"


def _request_state(req: Request, want: DocState, doing: str) -> C.CheckResult:
    if req.state != want:
        return _block("request state", f"{req.id} is {req.state.value}; {doing} needs "
                      f"{want.value}.")
    return _pass("request state", f"{req.id} is {req.state.value}.")


def _owner_only(actor: str, owner: str, ref: str, doing: str) -> list[C.CheckResult]:
    if owner and actor != owner:
        return [_block("ownership", f"Only {owner}, who entered {ref}, {doing} it.")]
    return []


def _rfq_checks(svc: Service, rfq: Rfq) -> list[C.CheckResult]:
    """A request for quotation is for an approved request, from usable suppliers."""
    erp = svc.erp
    req = erp.get_request(rfq.request) if rfq.request else None
    res = [_request_state(req, DocState.APPROVED, "asking for quotes") if req else
           _block("request state", f"{rfq.id} isn't for a purchase request.")]
    res += [C.supplier_usable(erp.get_supplier(s), s) for s in rfq.suppliers]
    return res


def _quote_checks(svc: Service, q: Quote) -> list[C.CheckResult]:
    """A quote is from a usable supplier, for known items, dated no later than today, and,
    answering a request for quotation, from a supplier it asked about items it asked
    for."""
    erp, name = svc.erp, "request for quotation"
    res = [C.supplier_usable(erp.get_supplier(q.supplier_id), q.supplier_id),
           _items_known(svc, [ln.item_code for ln in q.lines])]
    if q.quoted_on > svc.today():
        res.append(_block("quote date", f"Dated {q.quoted_on.isoformat()}, after today; "
                          "a quote is entered once it is given."))
    else:
        res.append(_pass("quote date", f"Dated {q.quoted_on.isoformat()}."))
    if q.rfq is None:
        return res + [_pass(name, "Not in answer to a request for quotation.")]
    rfq = erp.get_rfq(q.rfq)
    if rfq is None:
        return res + [_block(name, f"No request for quotation '{q.rfq}'.")]
    problems = []
    if rfq.state != DocState.SUBMITTED:
        problems.append(f"{rfq.id} is {rfq.state.value}, not sent")
    if q.supplier_id not in rfq.suppliers:
        problems.append(f"{q.supplier_id} wasn't asked to quote on {rfq.id}")
    req = erp.get_request(rfq.request) if rfq.request else None
    asked = {ln.item_code for ln in req.lines} if req else set()
    extra = sorted({ln.item_code for ln in q.lines} - asked)
    if extra:
        problems.append(f"{', '.join(extra)} isn't on {rfq.id}")
    if problems:
        return res + [_block(name, "; ".join(problems) + ".")]
    return res + [_pass(name, f"Answers {rfq.id}.")]


# --- raise_purchase_request ------------------------------------------------------------

def _proposed_request(svc: Service, canon: dict) -> Request:
    return Request("(new)", svc.actor, canon["cost_centre"], DocState.DRAFT,
                   tuple(RequestLine(ln["item_code"], Decimal(ln["qty"]))
                         for ln in canon["lines"]), svc.today())


def _eval_raise(svc: Service, canon: dict) -> Result:
    summary = f"Raise a purchase request on {canon['cost_centre']}."
    if (bad := _unsupported(svc)):
        return summary, bad
    req = _proposed_request(svc, canon)
    res = _request_checks(svc, req) + _free_text({"notes": canon["notes"]})
    return (f"Raise a purchase request on {req.cost_centre}: {_describe(req.lines)}; "
            f"{_estimate(svc, req)}. A draft raised by {svc.actor}; next "
            "workflow_action(\"Material Request\", <id>, \"Submit for Approval\")."), res


def _write_raise(svc: Service, canon: dict) -> tuple[str, str]:
    req = _proposed_request(svc, canon)
    made = svc.erp.create_request(req.cost_centre, list(req.lines), canon["notes"])
    return made.id, (f"Purchase request {made.id} raised as a draft. Next: Submit for "
                     "Approval.")


# --- the request's workflow (reached through workflow_action) --------------------------

def evaluate_workflow(svc: Service, w: Write) -> Result:
    actor, ref, action = svc.actor, w.name, w.action or ""
    summary = f"Apply '{action}' to purchase request {ref}."
    if (bad := _unsupported(svc)):
        return summary, bad
    if action not in WORKFLOW_ACTIONS:
        return summary, [_block("pack", f"'{action}' is not a Material Request workflow "
                                f"action. Use one of: {', '.join(WORKFLOW_ACTIONS)}.")]
    req = svc.erp.get_request(ref)
    if req is None:
        return summary, [_block("request state", f"No purchase request '{ref}'.")]
    summary = (f"Apply '{action}' to purchase request {ref} ({_describe(req.lines)} on "
               f"{req.cost_centre}, raised by {req.requester}) as {actor}.")
    comment = _free_text({"comment": w.reason})
    if action == SUBMIT_FOR_APPROVAL:
        res = [_request_state(req, DocState.DRAFT, "submitting it"),
               *_owner_only(actor, req.requester, ref, "submits")]
        return f"{summary} It is {_estimate(svc, req)}.", res + _request_checks(svc, req) \
            + comment
    if action == APPROVE:
        prices = svc.erp.last_purchase_prices(sorted({ln.item_code for ln in req.lines}))
        res = [_request_state(req, DocState.PENDING, "approval"),
               P.request_authority(actor, req, prices, svc.policy)]
        return summary, res + _request_checks(svc, req) + comment
    if action == REJECT:
        res = [_request_state(req, DocState.PENDING, "rejection")]
        if svc.policy.approver_limit(actor, "purchase_request") is None:
            res.append(_block("request approval tier", f"Only purchase approvers reject "
                              f"requests; {actor} isn't one."))
        if actor == req.requester:
            res.append(_block("request approval tier", f"{actor} raised {ref} and can't "
                              "decide on it."))
        if not w.reason:
            res.append(_block("comment", f"Say why in the comment; it goes back to "
                              f"{req.requester}."))
        return summary, res + comment
    res = [_request_state(req, DocState.REJECTED, "revising it"),  # REVISE
           *_owner_only(actor, req.requester, ref, "revises")]
    return summary, res + comment


def apply_workflow(svc: Service, w: Write) -> tuple[str, str]:
    svc.erp.workflow(REQUEST, w.name, w.action, w.reason)
    req = svc.erp.get_request(w.name)
    note = f" Comment: {w.reason}" if w.reason else ""
    return w.name, (f"'{w.action}' applied to purchase request {w.name}; now "
                    f"{req.state.value}.{note}")


# --- request_quotes ---------------------------------------------------------------------

def _proposed_rfq(svc: Service, canon: dict) -> Rfq:
    return Rfq("(new)", canon["request"], tuple(canon["suppliers"]), DocState.DRAFT,
               svc.actor)


def _eval_request_quotes(svc: Service, canon: dict) -> Result:
    ref = canon["request"]
    summary = f"Ask for quotes for purchase request {ref}."
    if (bad := _unsupported(svc)):
        return summary, bad
    req = svc.erp.get_request(ref)
    if req is None:
        return summary, [_block("request state", f"No purchase request '{ref}'.")]
    res = _rfq_checks(svc, _proposed_rfq(svc, canon))
    return (f"Ask {len(canon['suppliers'])} supplier(s) to quote for {ref} "
            f"({_describe(req.lines)}): {', '.join(canon['suppliers'])}. A draft request for "
            "quotation; next submit_document(\"Request for Quotation\", <id>) sends it."), res


def _write_request_quotes(svc: Service, canon: dict) -> tuple[str, str]:
    rfq = svc.erp.create_rfq(canon["request"], canon["suppliers"])
    return rfq, f"Request for quotation {rfq} created as a draft. Next: submit it."


def evaluate_submit_rfq(svc: Service, w: Write) -> Result:
    ref = w.name
    summary = f"Submit request for quotation {ref}."
    if (bad := _unsupported(svc)):
        return summary, bad
    rfq = svc.erp.get_rfq(ref)
    if rfq is None:
        return summary, [_block("rfq state", f"No request for quotation '{ref}'.")]
    res = [_block("rfq state", f"{ref} is {rfq.state.value}; only a draft is submitted.")
           if rfq.state != DocState.DRAFT else _pass("rfq state", f"{ref} is a draft."),
           *_owner_only(svc.actor, rfq.created_by, ref, "submits")]
    return (f"Submit request for quotation {ref} for {rfq.request}, to "
            f"{', '.join(rfq.suppliers)}."), res + _rfq_checks(svc, rfq)


# --- enter_supplier_quote ---------------------------------------------------------------

def _proposed_quote(svc: Service, canon: dict) -> Quote:
    return Quote("(new)", canon["supplier"], canon["rfq"] or None, DocState.DRAFT,
                 tuple(Line(ln["item_code"], Decimal(ln["qty"]), Decimal(ln["rate"]))
                       for ln in canon["lines"]),
                 date.fromisoformat(canon["quoted_on"]), entered_by=svc.actor)


def _quote_total(q: Quote) -> Decimal:
    return sum((ln.amount for ln in q.lines), Decimal(0))


def _eval_quote(svc: Service, canon: dict) -> Result:
    summary = f"Enter a quotation from {canon['supplier']}."
    if (bad := _unsupported(svc)):
        return summary, bad
    q = _proposed_quote(svc, canon)
    answers = f"Answers {q.rfq}." if q.rfq else "Not in answer to a request for quotation."
    return (f"Enter {q.supplier_id}'s quotation of {q.quoted_on.isoformat()}: "
            f"{_describe(q.lines)}; total {P.usd(_quote_total(q))}. {answers} A draft; next "
            "submit_document(\"Supplier Quotation\", <id>)."), _quote_checks(svc, q)


def _write_quote(svc: Service, canon: dict) -> tuple[str, str]:
    q = _proposed_quote(svc, canon)
    made = svc.erp.create_quote(q.supplier_id, list(q.lines), q.rfq, q.quoted_on)
    return made.id, f"Supplier quotation {made.id} entered as a draft. Next: submit it."


def evaluate_submit_quote(svc: Service, w: Write) -> Result:
    ref = w.name
    summary = f"Submit supplier quotation {ref}."
    if (bad := _unsupported(svc)):
        return summary, bad
    q = svc.erp.get_quote(ref)
    if q is None:
        return summary, [_block("quote state", f"No supplier quotation '{ref}'.")]
    res = [_block("quote state", f"{ref} is {q.state.value}; only a draft is submitted.")
           if q.state != DocState.DRAFT else _pass("quote state", f"{ref} is a draft."),
           *_owner_only(svc.actor, q.entered_by, ref, "submits")]
    return (f"Submit {q.supplier_id}'s quotation {ref} of {q.quoted_on.isoformat()}: "
            f"{_describe(q.lines)}."), res + _quote_checks(svc, q)


def apply_submit(svc: Service, w: Write) -> tuple[str, str]:
    svc.erp.submit_doc(w.doctype, w.name)
    return w.name, f"{w.doctype} {w.name} submitted."


# --- edits to a draft (reached through update_document) --------------------------------

_EDITABLE_TEXT = {REQUEST: "items[].qty", QUOTE: "items[].qty, items[].rate"}


def _edited_lines(lines, fields: dict, keys: tuple[str, ...]) -> tuple[tuple | None, list]:
    """The lines after the edit (rows merged in order), or what's wrong with it."""
    rows = fields.get("items")
    if rows is None:
        return tuple(lines), []
    if len(rows) != len(lines):
        return None, [f"items: {len(rows)} rows given, the draft has {len(lines)}"]
    out, problems = [], []
    for i, (ln, row) in enumerate(zip(lines, rows, strict=True)):
        try:
            out.append(replace(ln, **{k: Decimal(_positive(row, k, i)) for k in keys
                                      if k in row}))
        except InvalidArgs as exc:
            problems.append(str(exc))
    return (tuple(out), []) if not problems else (None, problems)


def evaluate_update(svc: Service, w: Write) -> Result:
    ref, doctype, name = w.name, w.doctype, "edit after entry"
    what = "purchase request" if doctype == REQUEST else "supplier quotation"
    summary = f"Update {what} {ref}."
    if (bad := _unsupported(svc)):
        return summary, bad
    doc = svc.erp.get_request(ref) if doctype == REQUEST else svc.erp.get_quote(ref)
    if doc is None:
        return summary, [_block(name, f"No {what} '{ref}'.")]
    owner = doc.requester if doctype == REQUEST else doc.entered_by
    res = []
    if doc.state != DocState.DRAFT:
        res.append(_block(name, f"{ref} is {doc.state.value}; only drafts are edited."))
    if svc.actor != owner:
        res.append(_block(name, f"Only {owner}, who entered {ref}, edits it."))
    bad_fields = not_editable(doctype, w.fields)
    if bad_fields:
        res.append(_block(name, f"These can't be changed on a {what} draft: "
                          f"{', '.join(bad_fields)}. Editable: {_EDITABLE_TEXT[doctype]}.",
                          fields=bad_fields))
        return summary, res
    keys = ("qty",) if doctype == REQUEST else ("qty", "rate")
    lines, problems = _edited_lines(doc.lines, w.fields, keys)
    if problems:
        return summary, res + [_block(name, "; ".join(problems) + ".")]
    if not any(r.level == C.BLOCK for r in res):
        res.append(_pass(name, f"{ref} is a draft entered by {svc.actor}."))
    edited = replace(doc, lines=lines)
    res += _request_checks(svc, edited) if doctype == REQUEST else _quote_checks(svc, edited)
    tail = f"; {_estimate(svc, edited)}" if doctype == REQUEST else \
        f"; total {P.usd(_quote_total(edited))}"
    return f"Update draft {what} {ref}: {_describe(edited.lines)}{tail}.", res


def apply_update(svc: Service, w: Write) -> tuple[str, str]:
    fields = {"items": [{k: Decimal(str(v)) for k, v in row.items()}
                        for row in w.fields.get("items", [])]}
    svc.erp.update_draft(w.doctype, w.name, fields)
    return w.name, f"Draft {w.doctype} {w.name} updated: {', '.join(sorted(w.fields))}."


# --- cancellation (reached through cancel_document) ------------------------------------

def _live_orders_from(svc: Service, doctype: str, doc) -> list[str]:
    """Live orders made from this request or quotation."""
    if doctype == REQUEST:
        return sorted(o.id for o in svc.erp.list_orders(since=doc.created)
                      if o.request == doc.id and o.status in LIVE_STATUSES)
    return sorted(o.id for o in svc.erp.list_orders(since=doc.quoted_on)
                  if doc.id in o.quotations and o.status in LIVE_STATUSES)


def _quotes_answering(svc: Service, rfq: Rfq) -> list[str]:
    req = svc.erp.get_request(rfq.request) if rfq.request else None
    codes = sorted({ln.item_code for ln in req.lines}) if req else []
    return sorted(q.id for q in svc.erp.list_quotes(item_codes=codes, since=date.min)
                  if q.rfq == rfq.id and q.state in POSTED)


def evaluate_cancel(svc: Service, w: Write) -> Result:
    actor, ref, doctype, name = svc.actor, w.name, w.doctype, "cancel"
    summary = f"Cancel {doctype} {ref}. Nothing is deleted; it stays on record as cancelled."
    if (bad := _unsupported(svc)):
        return summary, bad
    erp = svc.erp
    doc = (erp.get_request(ref) if doctype == REQUEST else
           erp.get_rfq(ref) if doctype == RFQ else erp.get_quote(ref))
    if doc is None:
        return summary, [_block(name, f"No {doctype} '{ref}'.")]
    owner = (doc.requester if doctype == REQUEST else
             doc.created_by if doctype == RFQ else doc.entered_by)
    res: list[C.CheckResult] = []
    if doc.state not in POSTED:
        res.append(_block(name, f"{ref} is {doc.state.value}; only a submitted one is "
                          "cancelled (a draft counts for nothing, so needs no cancelling)."))
    if actor != owner and svc.policy.approver_limit(actor) is None:
        res.append(_block(name, f"Only {owner}, who entered {ref}, or a purchase approver "
                          "cancels it."))
    if not w.reason:
        res.append(_block(name, "A reason is required."))
    if doc.state in POSTED:
        if doctype == RFQ:
            if (quotes := _quotes_answering(svc, doc)):
                res.append(_block(name, f"Submitted quotations answer {ref} "
                                  f"({', '.join(quotes)}); cancel them first."))
        elif (orders := _live_orders_from(svc, doctype, doc)):
            res.append(_block(name, f"Live orders are made from {ref} "
                              f"({', '.join(orders)}); cancel them first."))
    if not any(r.level == C.BLOCK for r in res):
        res.append(_pass(name, f"{ref} is {doc.state.value}; {actor} can cancel it."))
    return summary, res + _free_text({"reason": w.reason})


def apply_cancel(svc: Service, w: Write) -> tuple[str, str]:
    svc.erp.cancel_doc(w.doctype, w.name, w.reason)
    return w.name, f"{w.doctype} {w.name} cancelled. Reason: {w.reason}"


# --- the actions -----------------------------------------------------------------------

ACTIONS: list[Action] = [
    Action("raise_purchase_request", canonicaliser("raise_purchase_request", {
        "cost_centre": lambda a: _text(a, "cost_centre", required=True, max_len=140),
        "lines": _item_lines(("qty",)),
        "notes": lambda a: _text(a, "notes"),
    }), _eval_raise, _write_raise),
    Action("request_quotes", canonicaliser("request_quotes", {
        "request": lambda a: _text(a, "request", required=True, max_len=140),
        "suppliers": _suppliers,
    }), _eval_request_quotes, _write_request_quotes, "request"),
    Action("enter_supplier_quote", canonicaliser("enter_supplier_quote", {
        "supplier": lambda a: _text(a, "supplier", required=True, max_len=140),
        "lines": _item_lines(("qty", "rate")),
        "rfq": lambda a: _text(a, "rfq", max_len=140),
        "quoted_on": lambda a: _iso_date(a, "quoted_on"),
    }), _eval_quote, _write_quote),
]
