"""The controls. Pure functions: proposed action + ERP snapshot + policy -> results.

None of these touch the ERP. The service gathers a Snapshot through the connector and
passes it in, so each check is unit-testable and identical across ERPs.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from decimal import Decimal

from erp_guard.matching import match_supplier_name
from erp_guard.models import LIVE_STATUSES, PurchaseOrder, Snapshot, Status, Supplier
from erp_guard.policy import Policy

PASS, WARN, BLOCK = "pass", "warn", "block"


@dataclass(frozen=True)
class CheckResult:
    check: str
    level: str
    message: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def blocked(results: list[CheckResult]) -> bool:
    return any(r.level == BLOCK for r in results)


def _money(x: Decimal) -> str:
    return f"${x:,.2f}"


# --- suppliers -----------------------------------------------------------------------

def _norm_tax_id(s: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


def duplicate_supplier(name: str, tax_id: str | None, suppliers: list[Supplier],
                       policy: Policy) -> CheckResult:
    tid = _norm_tax_id(tax_id)
    if tid:
        for s in suppliers:
            if _norm_tax_id(s.tax_id) == tid:
                return CheckResult(
                    "duplicate_supplier", BLOCK,
                    f"Tax ID {tax_id} already belongs to supplier {s.name} ({s.id}).",
                    {"match": s.id, "match_name": s.name, "by": "tax_id"},
                )
    matches = match_supplier_name(name, [(s.id, s.name) for s in suppliers],
                                  warn_ratio=policy.dup_warn_ratio)
    if matches and matches[0].level == "block":
        m = matches[0]
        return CheckResult(
            "duplicate_supplier", BLOCK,
            f"'{name}' matches existing supplier {m.name} ({m.supplier_id}).",
            {"match": m.supplier_id, "match_name": m.name, "score": m.score, "by": "name"},
        )
    if matches:
        return CheckResult(
            "duplicate_supplier", WARN,
            "Similar names already in the supplier master: "
            + "; ".join(f"{m.name} ({m.supplier_id})" for m in matches[:3])
            + ". Check these aren't the same company.",
            {"similar": [m.supplier_id for m in matches[:3]]},
        )
    return CheckResult("duplicate_supplier", PASS, "No matching supplier by name or tax ID.")


def supplier_usable(supplier: Supplier | None, supplier_ref: str) -> CheckResult:
    if supplier is None:
        return CheckResult("supplier_usable", BLOCK, f"No supplier '{supplier_ref}' in the ERP.")
    if supplier.disabled or supplier.on_hold:
        state = "disabled" if supplier.disabled else "on hold"
        return CheckResult("supplier_usable", BLOCK, f"Supplier {supplier.name} is {state}.")
    return CheckResult("supplier_usable", PASS, f"Supplier {supplier.name} is active.")


def self_dealing(user: str, supplier: Supplier | None, role: str) -> CheckResult:
    if supplier is not None and supplier.onboarded_by and supplier.onboarded_by == user:
        return CheckResult(
            "self_dealing", BLOCK,
            f"{user} onboarded supplier {supplier.name}, so can't also be its {role}.",
            {"onboarded_by": supplier.onboarded_by},
        )
    return CheckResult("self_dealing", PASS, f"The {role} didn't onboard this supplier.")


# --- orders --------------------------------------------------------------------------

def split_purchase(order: PurchaseOrder, recent: list[PurchaseOrder], policy: Policy,
                   today) -> CheckResult:
    since = today - timedelta(days=policy.split_window_days)
    others = [
        o for o in recent
        if o.id != order.id
        and o.supplier_id == order.supplier_id
        and o.cost_centre == order.cost_centre
        and o.status in LIVE_STATUSES
        and o.order_date >= since
    ]
    tier = policy.required_tier(order.total)
    combined = order.total + sum((o.total for o in others), Decimal(0))
    evidence = {"related_orders": [o.id for o in others], "combined": str(combined),
                "tier": tier.name, "tier_limit": str(tier.limit)}
    if others and combined > tier.limit:
        return CheckResult(
            "split_purchase", BLOCK,
            f"Possible split purchase. This order ({_money(order.total)}) is within the "
            f"{tier.name} limit of "
            f"{_money(tier.limit)}, but with {len(others)} other live order(s) to the same "
            f"supplier and cost centre in the last {policy.split_window_days} days "
            f"({', '.join(o.id for o in others)}) the total is {_money(combined)}. "
            "Raise one order for the full amount so it gets the right approval.",
            evidence,
        )
    return CheckResult("split_purchase", PASS,
                       f"No split pattern ({len(others)} related order(s) in window).", evidence)


def budget(order: PurchaseOrder, snap: Snapshot) -> CheckResult:
    cc = snap.cost_centre
    if cc is None:
        return CheckResult("budget", BLOCK, f"No cost centre '{order.cost_centre}' in the ERP.")
    remaining = cc.annual_budget - snap.committed
    evidence = {"budget": str(cc.annual_budget), "committed": str(snap.committed),
                "remaining": str(remaining), "order_total": str(order.total)}
    if order.total > remaining:
        return CheckResult(
            "budget", BLOCK,
            f"Cost centre {cc.id} has {_money(remaining)} left of {_money(cc.annual_budget)} "
            f"this fiscal year; this order is {_money(order.total)}.",
            evidence,
        )
    return CheckResult("budget", PASS,
                       f"{_money(remaining - order.total)} of budget left after this order.",
                       evidence)


def price_vs_last(order: PurchaseOrder, last_prices: dict[str, Decimal],
                  policy: Policy) -> CheckResult:
    worst_level, notes, evidence = PASS, [], {}
    for ln in order.lines:
        last = last_prices.get(ln.item_code)
        if not last:
            continue
        pct = (ln.rate - last) / last * 100
        evidence[ln.item_code] = {"rate": f"{ln.rate:f}", "last": f"{last:f}",
                                  "pct": str(round(pct, 1))}
        if pct > policy.price_block_pct:
            worst_level = BLOCK
            notes.append(f"{ln.item_code} at {_money(ln.rate)} is {pct:.0f}% above the last "
                         f"purchase price {_money(last)}")
        elif pct > policy.price_warn_pct:
            worst_level = WARN if worst_level == PASS else worst_level
            notes.append(f"{ln.item_code} is {pct:.0f}% above the last price {_money(last)}")
    if notes:
        return CheckResult("price_vs_last", worst_level, "; ".join(notes) + ".", evidence)
    return CheckResult("price_vs_last", PASS,
                       "Prices within tolerance of the last purchase (or no history).", evidence)


def items_known(order: PurchaseOrder, known: set[str]) -> CheckResult:
    missing = sorted({ln.item_code for ln in order.lines} - known)
    if missing:
        return CheckResult("items_known", BLOCK, f"Unknown item code(s): {', '.join(missing)}.")
    if not order.lines:
        return CheckResult("items_known", BLOCK, "An order needs at least one line.")
    bad = [ln.item_code for ln in order.lines if ln.qty <= 0 or ln.rate <= 0]
    if bad:
        return CheckResult("items_known", BLOCK,
                           f"Quantity and rate must be positive ({', '.join(bad)}).")
    return CheckResult("items_known", PASS, "All items exist in the item master.")


# --- approvals -----------------------------------------------------------------------

def state(order: PurchaseOrder | None, ref: str, allowed: set[Status],
          action: str) -> CheckResult:
    if order is None:
        return CheckResult("state", BLOCK, f"No purchase order '{ref}'.")
    if order.status not in allowed:
        return CheckResult(
            "state", BLOCK,
            f"{order.id} is {order.status.value}; {action} needs "
            f"{' or '.join(sorted(s.value for s in allowed))}.",
        )
    return CheckResult("state", PASS, f"{order.id} is {order.status.value}.")


def segregation_of_duties(actor: str, order: PurchaseOrder) -> CheckResult:
    if actor == order.requester:
        return CheckResult(
            "segregation_of_duties", BLOCK,
            f"{actor} raised {order.id} and can't approve or reject it.",
            {"requester": order.requester},
        )
    return CheckResult("segregation_of_duties", PASS,
                       f"Approver {actor} is not the requester {order.requester}.")


def approval_authority(actor: str, order: PurchaseOrder, policy: Policy) -> CheckResult:
    limit = policy.approver_limit(actor)
    if limit is None:
        return CheckResult("approval_authority", BLOCK, f"{actor} has no approval authority.")
    if order.total > limit:
        need = policy.required_tier(order.total)
        return CheckResult(
            "approval_authority", BLOCK,
            f"{order.id} totals {_money(order.total)}; {actor}'s limit is {_money(limit)}. "
            f"Needs a {need.name}.",
            {"limit": str(limit), "total": str(order.total)},
        )
    return CheckResult("approval_authority", PASS,
                       f"Within {actor}'s limit of {_money(limit)}.")


# --- free text -----------------------------------------------------------------------

_INSTRUCTION = re.compile(
    r"ignore (all |any )?(previous|prior|above)|disregard|system\s*:|assistant\s*:|"
    r"approve (this|it|immediately|now)|skip (the )?(check|preview|approval)|"
    r"confirmation code|you (must|should) now|new instructions|"
    r"\bapproved by (the )?(cfo|ceo|board)\b|\bpay (this |it )?(today|now|immediately)\b",
    re.IGNORECASE,
)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")
# Field names that hold bank details. `payment` is deliberately absent: payment_type and
# payment_schedule are ordinary fields.
BANK_KEY = re.compile(r"bank|iban|swift|bic|routing|account_no", re.IGNORECASE)
_BANK_WORDS = re.compile(
    r"\b(routing|aba|swift|bic|sort code|iban|bank account|account (number|no\.?|#))\b",
    re.IGNORECASE,
)


def _has_bank_details(text: str) -> bool:
    if _BANK_WORDS.search(text):
        return True
    return any(sum(c.isdigit() for c in m.group(0)) >= 8 for m in _IBAN.finditer(text))


def free_text(texts: dict[str, str]) -> list[CheckResult]:
    """Bank details in free text are refused; instruction-like text is flagged.

    The instruction flag is a courtesy to the human reading the preview. The actual
    defence is that free text never changes what a tool does.
    """
    out = []
    bank = [k for k, v in texts.items() if v and _has_bank_details(v)]
    if bank:
        out.append(CheckResult(
            "no_bank_details", BLOCK,
            f"Bank-account details in {', '.join(bank)}. erp-guard never writes bank details; "
            "they go through the ERP's own verified vendor-banking process.",
        ))
    else:
        out.append(CheckResult("no_bank_details", PASS, "No bank details in free text."))
    sus = {k: _INSTRUCTION.search(v).group(0) for k, v in texts.items()
           if v and _INSTRUCTION.search(v)}
    if sus:
        out.append(CheckResult(
            "instruction_like_text", WARN,
            "Free text reads like instructions to an assistant ("
            + "; ".join(f"{k}: '{v}'" for k, v in sus.items())
            + "). It is stored as data and changes nothing, but a human should read it.",
            {"fields": sorted(sus)},
        ))
    return out
