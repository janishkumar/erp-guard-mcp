"""Closing a purchase order: close_purchase_order.

A closed order takes no more receipts or invoices (their checks refuse it) and can't be
amended or cancelled. Closing is ERPNext's own status change; nothing is deleted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from erp_guard import checks as C
from erp_guard.packs.p2p import checks as P
from erp_guard.service import Action, _text, acts_on, canonicaliser

if TYPE_CHECKING:
    from erp_guard.service import Service

Result = tuple[str, list[C.CheckResult]]


def _eval_close(svc: Service, canon: dict) -> Result:
    actor, ref = svc.actor, canon["po"]
    summary = f"Close {ref} as {actor}. Nothing more can be received or invoiced against it."
    if not all(hasattr(svc.erp, m) for m in ("list_receipts", "list_invoices", "close_order")):
        return summary, [C.CheckResult("connector", C.BLOCK,
                                       f"{svc.erp.name} doesn't support closing orders yet.")]
    po = svc.erp.get_order(ref)
    res = [P.close_po_state(po, ref)]
    if res[0].level == C.PASS:
        codes = sorted({ln.item_code for ln in po.lines})
        res.append(P.close_po(actor, po, svc.erp.list_receipts(po=ref),
                              svc.erp.list_invoices(po=ref), svc.erp.item_info(codes),
                              svc.policy, canon["reason"]))
    return summary, res + C.free_text({"reason": canon["reason"]} if canon["reason"] else {})


def _write_close(svc: Service, canon: dict) -> tuple[str, str]:
    ref = canon["po"]
    svc.erp.close_order(ref, canon["reason"])
    why = f" Reason: {canon['reason']}" if canon["reason"] else ""
    return ref, f"{ref} closed.{why}"


ACTIONS: list[Action] = [
    Action("close_purchase_order", canonicaliser("close_purchase_order", {
        "po": lambda a: _text(a, "po", required=True, max_len=140),
        "reason": lambda a: _text(a, "reason"),
    }), _eval_close, _write_close, "po", acts_on("Purchase Order", "po")),
]
