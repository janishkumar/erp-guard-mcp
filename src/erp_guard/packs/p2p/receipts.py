"""Goods receipts and returns: receive_goods, return_goods, and cancelling a receipt.

A receipt records a fact, so it is submitted as it is created; the checks run first. A
return is a receipt of negative quantities against the receipt the goods came in on.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from erp_guard import checks as C
from erp_guard.ops import Write
from erp_guard.packs.p2p import checks as P
from erp_guard.packs.p2p.models import POSTED, DocState, ReceiptLine
from erp_guard.service import Action, InvalidArgs, _decimal, _text, acts_on, canonicaliser

if TYPE_CHECKING:
    from erp_guard.service import Service

Result = tuple[str, list[C.CheckResult]]


def _item_lines(args: dict) -> list[dict]:
    """[{"item_code", "qty"}], each item once, quantities positive."""
    raw = args.get("lines")
    if not isinstance(raw, list) or not raw:
        raise InvalidArgs("'lines' must be a non-empty list")
    out, seen = [], set()
    for i, ln in enumerate(raw):
        if not isinstance(ln, dict):
            raise InvalidArgs(f"line {i + 1} must be an object")
        unknown = set(ln) - {"item_code", "qty"}
        if unknown:
            raise InvalidArgs(f"line {i + 1} has unknown field(s): {', '.join(sorted(unknown))}")
        item = _text(ln, "item_code", required=True, max_len=140)
        qty = _decimal(ln.get("qty"), "qty")
        if qty <= 0:
            raise InvalidArgs(f"line {i + 1}: 'qty' must be more than zero")
        if item in seen:
            raise InvalidArgs(f"{item} is on more than one line; put each item on one line")
        seen.add(item)
        out.append({"item_code": item, "qty": format(qty, "f")})
    return out


def _free_text(canon: dict) -> list[C.CheckResult]:
    return C.free_text({k: canon[k] for k in ("notes", "reason") if canon.get(k)})


def _unsupported(svc: Service) -> list[C.CheckResult]:
    """Receipts need a connector that models them (ERPNext, the in-memory one)."""
    if hasattr(svc.erp, "list_receipts"):
        return []
    return [C.CheckResult("connector", C.BLOCK,
                          f"{svc.erp.name} doesn't support goods receipts yet.")]


def _describe(lines: list[ReceiptLine]) -> str:
    return ", ".join(f"{ln.qty.normalize():f} x {ln.item_code}" for ln in lines)


# --- receive_goods ---------------------------------------------------------------------

def _new_lines(po: str, canon: dict) -> list[ReceiptLine]:
    return [ReceiptLine(po, ln["item_code"], Decimal(ln["qty"])) for ln in canon["lines"]]


def _eval_receive(svc: Service, canon: dict) -> Result:
    actor, ref = svc.actor, canon["po"]
    new = _new_lines(ref, canon)
    summary = f"Receive {_describe(new)} against {ref}, recorded by {actor}."
    if (bad := _unsupported(svc)):
        return summary, bad
    po = svc.erp.get_order(ref)
    res = [P.role_allowed(actor, svc.policy, "receiver", "record goods receipts"),
           P.receipt_po_state(po, ref)]
    if po is not None:
        res += P.receiver_independence(actor, po)
        res.append(P.stock_items([ln.item_code for ln in new],
                                 svc.erp.item_info([ln.item_code for ln in new])))
        res.append(P.over_receipt(po, svc.erp.list_receipts(po=ref), new,
                                  svc.policy.over_receipt_pct))
    res += _free_text(canon)
    return summary, res


def _write_receive(svc: Service, canon: dict) -> tuple[str, str]:
    new = _new_lines(canon["po"], canon)
    r = svc.erp.create_receipt(canon["po"], new)
    note = f" Notes: {canon['notes']}" if canon["notes"] else ""
    return r.id, f"Receipt {r.id} recorded against {canon['po']}: {_describe(new)}.{note}"


# --- return_goods ----------------------------------------------------------------------

def _return_lines(receipt, canon: dict) -> list[ReceiptLine]:
    """The lines returned, each on the order its goods were received against."""
    po_of = {ln.item_code: ln.po for ln in receipt.lines}
    return [ReceiptLine(po_of.get(ln["item_code"], ""), ln["item_code"], Decimal(ln["qty"]))
            for ln in canon["lines"]]


def _eval_return(svc: Service, canon: dict) -> Result:
    actor, ref = svc.actor, canon["receipt"]
    summary = f"Return goods from {ref}, recorded by {actor}."
    if (bad := _unsupported(svc)):
        return summary, bad
    receipt = svc.erp.get_receipt(ref)
    res = [P.role_allowed(actor, svc.policy, "receiver", "return goods"),
           P.receipt_returnable(receipt, ref)]
    if receipt is not None and res[-1].level == C.PASS:
        new = _return_lines(receipt, canon)
        returns = svc.erp.list_receipts(supplier_id=receipt.supplier_id)
        res.append(P.return_limit(receipt, returns, new))
        orders = sorted({ln.po for ln in new if ln.po})
        if len(orders) > 1:
            res.append(C.CheckResult(
                "return limit", C.BLOCK,
                f"{ref} brought these goods in against more than one order "
                f"({', '.join(orders)}); return them one order at a time."))
        summary = f"Return {_describe(new)} from {ref}, recorded by {actor}."
    res += _free_text(canon)
    return summary, res


def _write_return(svc: Service, canon: dict) -> tuple[str, str]:
    receipt = svc.erp.get_receipt(canon["receipt"])
    new = _return_lines(receipt, canon)
    r = svc.erp.create_receipt(new[0].po, new, is_return=True, return_against=receipt.id)
    return r.id, (f"Return {r.id} recorded against {receipt.id}: {_describe(new)}. "
                  f"Reason: {canon['reason']}")


# --- cancel a receipt (reached through cancel_document) --------------------------------

def _restored_goods(svc: Service, ret) -> list[C.CheckResult]:
    """Cancelling a return puts its goods back on the books as received: that must still be
    within what the order allows."""
    out = []
    for po_id in sorted({ln.po for ln in ret.lines}):
        po = svc.erp.get_order(po_id)
        back = [ln for ln in ret.lines if ln.po == po_id]
        if po is None:
            continue
        over = P.over_receipt(po, svc.erp.list_receipts(po=po_id), back,
                              svc.policy.over_receipt_pct)
        if over.level == C.BLOCK:
            out.append(C.CheckResult(
                "cancel receipt", C.BLOCK,
                f"Cancelling return {ret.id} puts its goods back as received: {over.message}",
                over.evidence))
    return out


def evaluate_cancel(svc: Service, w: Write) -> Result:
    actor, ref = svc.actor, w.name
    summary = (f"Cancel purchase receipt {ref}. Nothing is deleted; it stays on record as "
               "cancelled.")
    if (bad := _unsupported(svc)):
        return summary, bad
    name = "cancel receipt"
    receipt = svc.erp.get_receipt(ref)
    if receipt is None:
        return summary, [C.CheckResult(name, C.BLOCK, f"No purchase receipt '{ref}'.")]
    res: list[C.CheckResult] = []
    if receipt.state != DocState.SUBMITTED:
        res.append(C.CheckResult(name, C.BLOCK, f"{ref} is {receipt.state.value}; only a "
                                 "submitted receipt is cancelled."))
    if receipt.receiver != actor and svc.policy.approver_limit(actor) is None:
        res.append(C.CheckResult(name, C.BLOCK, f"Only {receipt.receiver}, who recorded "
                                 f"{ref}, or an approver can cancel it."))
    invoices = [i for po in sorted({ln.po for ln in receipt.lines})
                for i in svc.erp.list_invoices(po=po)
                if i.state != DocState.CANCELLED
                and any(ln.receipt == ref for ln in i.lines)]
    posted = [i.id for i in invoices if i.state in POSTED]
    open_ = [i.id for i in invoices if i.state not in POSTED]
    if posted:
        res.append(C.CheckResult(name, C.BLOCK, f"{ref} has submitted invoice(s) against it "
                                 f"({', '.join(posted)}); cancel those first.",
                                 {"invoices": posted}))
    if open_:
        res.append(C.CheckResult(name, C.WARN, f"Draft invoice(s) refer to {ref} "
                                 f"({', '.join(open_)}); they won't match once it is "
                                 "cancelled.", {"invoices": open_}))
    returns = [r.id for r in svc.erp.list_receipts(supplier_id=receipt.supplier_id)
               if r.is_return and r.return_against == ref and r.state != DocState.CANCELLED]
    if returns:
        res.append(C.CheckResult(name, C.BLOCK, f"{ref} has return(s) against it "
                                 f"({', '.join(returns)}); cancel those first.",
                                 {"returns": returns}))
    if receipt.is_return:
        res += _restored_goods(svc, receipt)
    if not res:
        res.append(C.CheckResult(name, C.PASS, f"{ref} can be cancelled by {actor}."))
    return summary, res + C.free_text({"reason": w.reason} if w.reason else {})


def apply_cancel(svc: Service, w: Write) -> tuple[str, str]:
    svc.erp.cancel_doc("Purchase Receipt", w.name, w.reason)
    return w.name, f"Purchase Receipt {w.name} cancelled. Reason: {w.reason}"


# --- the actions -----------------------------------------------------------------------

def _po(a: dict) -> str:
    return _text(a, "po", required=True, max_len=140)


ACTIONS: list[Action] = [
    Action("receive_goods", canonicaliser("receive_goods", {
        "po": _po,
        "lines": _item_lines,
        "notes": lambda a: _text(a, "notes"),
    }), _eval_receive, _write_receive, "po", acts_on("Purchase Order", "po")),
    Action("return_goods", canonicaliser("return_goods", {
        "receipt": lambda a: _text(a, "receipt", required=True, max_len=140),
        "lines": _item_lines,
        "reason": lambda a: _text(a, "reason", required=True),
    }), _eval_return, _write_return, "receipt", acts_on("Purchase Receipt", "receipt")),
]
