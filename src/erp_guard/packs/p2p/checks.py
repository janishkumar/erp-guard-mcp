"""Purchase-to-pay checks. Pure functions: documents already read from the ERP, plus the
policy, in; results out. They share `erp_guard.checks`' result type and levels."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from erp_guard.checks import BLOCK, PASS, WARN, CheckResult
from erp_guard.models import PurchaseOrder, Status
from erp_guard.packs.p2p.models import DocState, ItemInfo, Receipt, ReceiptLine
from erp_guard.policy import Policy


def _per_item(lines) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(Decimal)
    for ln in lines:
        out[ln.item_code] += ln.qty
    return out


def _net_received(receipts: list[Receipt], po: str) -> dict[str, Decimal]:
    """Quantity per item received on the order: receipts add, returns subtract. Drafts count
    (they will be posted); cancelled documents don't."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for r in receipts:
        if r.state == DocState.CANCELLED:
            continue
        sign = -1 if r.is_return else 1
        for ln in r.lines:
            if ln.po == po:
                out[ln.item_code] += sign * ln.qty
    return out


def _qty(x: Decimal) -> str:
    return format(x.normalize(), "f")


def role_allowed(actor: str, policy: Policy, role: str, what: str) -> CheckResult:
    if role in policy.roles.get(actor, set()):
        return CheckResult("role", PASS, f"{actor} holds the {role} role.")
    return CheckResult("role", BLOCK, f"{actor} doesn't hold the {role} role, so can't {what}.",
                       {"actor": actor, "role": role})


def receipt_po_state(po: PurchaseOrder | None, ref: str) -> CheckResult:
    name = "receipt against approved PO"
    if po is None:
        return CheckResult(name, BLOCK, f"No purchase order '{ref}'.")
    if po.status != Status.APPROVED or po.closed:
        now = "closed" if po.closed and po.status == Status.APPROVED else po.status.value
        return CheckResult(name, BLOCK,
                           f"{po.id} is {now}; goods are received only against an approved, "
                           "open order.", {"status": now})
    return CheckResult(name, PASS, f"{po.id} is approved and open.")


def stock_items(codes: list[str], infos: dict[str, ItemInfo]) -> CheckResult:
    """Only stock items are received; services are invoiced against the order."""
    bad = sorted(c for c in codes if c in infos and not infos[c].is_stock)
    if bad:
        return CheckResult("stock items", BLOCK,
                           f"{', '.join(bad)} is not a stock item; services are invoiced "
                           "against the order, not received.", {"items": bad})
    return CheckResult("stock items", PASS, "All items are stock items.")


def over_receipt(po: PurchaseOrder, receipts: list[Receipt], new: list[ReceiptLine],
                 pct: Decimal) -> CheckResult:
    """Received (draft and submitted receipts, less returns) plus the new quantity must stay
    within the ordered quantity plus the tolerance."""
    ordered = _per_item(po.lines)
    received = _net_received(receipts, po.id)
    problems, items = [], []
    for item, qty in sorted(_per_item(new).items()):
        if item not in ordered:
            problems.append(f"{item} is not on {po.id}")
            items.append(item)
            continue
        limit = ordered[item] * (1 + pct / 100)
        total = received.get(item, Decimal(0)) + qty
        if total > limit:
            problems.append(
                f"{item}: receiving {_qty(qty)} would make {_qty(total)} received against "
                f"{_qty(ordered[item])} ordered (the most allowed is {_qty(limit)}, "
                f"{_qty(pct)}% over)")
            items.append(item)
    if problems:
        return CheckResult("over-receipt", BLOCK, "; ".join(problems) + ".",
                           {"items": items, "tolerance_pct": str(pct)})
    return CheckResult("over-receipt", PASS,
                       f"Within the ordered quantity plus {_qty(pct)}%.")


def receiver_independence(actor: str, po: PurchaseOrder) -> list[CheckResult]:
    name = "receiver independence"
    out = []
    if po.approver and actor == po.approver:
        out.append(CheckResult(name, BLOCK,
                               f"{actor} approved {po.id} and can't also receive its goods.",
                               {"approver": po.approver}))
    if actor == po.requester:
        out.append(CheckResult(name, WARN,
                               f"{actor} requested {po.id} and is also receiving its goods; "
                               "someone else should confirm the delivery.",
                               {"requester": po.requester}))
    return out or [CheckResult(name, PASS,
                               f"{actor} neither requested nor approved {po.id}.")]


def receipt_returnable(receipt: Receipt | None, ref: str) -> CheckResult:
    name = "receipt state"
    if receipt is None:
        return CheckResult(name, BLOCK, f"No purchase receipt '{ref}'.")
    if receipt.is_return:
        return CheckResult(name, BLOCK, f"{receipt.id} is itself a return; return goods from "
                           "the receipt that brought them in.")
    if receipt.state != DocState.SUBMITTED:
        return CheckResult(name, BLOCK, f"{receipt.id} is {receipt.state.value}; only a "
                           "submitted receipt can have goods returned.")
    return CheckResult(name, PASS, f"{receipt.id} is submitted.")


def return_limit(receipt: Receipt, returns: list[Receipt], new: list[ReceiptLine]
                 ) -> CheckResult:
    """Returned quantity per item must not exceed what the receipt brought in less what has
    already gone back (drafts included, cancelled returns not)."""
    received = _per_item(receipt.lines)
    back = _per_item(ln for r in returns
                     if r.is_return and r.return_against == receipt.id
                     and r.state != DocState.CANCELLED for ln in r.lines)
    problems, items = [], []
    for item, qty in sorted(_per_item(new).items()):
        left = received.get(item, Decimal(0)) - back.get(item, Decimal(0))
        if item not in received:
            problems.append(f"{item} is not on {receipt.id}")
        elif qty > left:
            problems.append(f"{item}: returning {_qty(qty)} but only {_qty(left)} of "
                            f"{_qty(received[item])} received is still with us")
        else:
            continue
        items.append(item)
    if problems:
        return CheckResult("return limit", BLOCK, "; ".join(problems) + ".", {"items": items})
    return CheckResult("return limit", PASS, "Within what the receipt brought in.")
