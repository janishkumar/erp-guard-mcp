"""Preview -> one-time code -> re-check -> write -> audit. Every write goes through here."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from erp_guard import checks as C
from erp_guard.connectors.base import ErpConnector, ErpError
from erp_guard.models import (
    LIVE_STATUSES,
    AuditRecord,
    Line,
    PurchaseOrder,
    Snapshot,
    Status,
)
from erp_guard.policy import Policy

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L

WRITE_ACTIONS = (
    "create_purchase_order", "onboard_supplier", "submit_for_approval",
    "approve_purchase_order", "reject_purchase_order", "cancel_purchase_order",
    "amend_purchase_order",
)


class InvalidArgs(ValueError):
    pass


# --- argument canonicalisation -------------------------------------------------------

def _text(args: dict, key: str, required: bool = False, max_len: int = 2000) -> str:
    v = args.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise InvalidArgs(f"'{key}' is required")
        return ""
    if not isinstance(v, str):
        raise InvalidArgs(f"'{key}' must be text")
    v = v.strip()
    if len(v) > max_len:
        raise InvalidArgs(f"'{key}' is longer than {max_len} characters")
    return v


def _decimal(v, key: str) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidArgs(f"'{key}' must be a number") from exc
    if not d.is_finite():
        raise InvalidArgs(f"'{key}' must be a finite number")
    return d.normalize()


def _lines(args: dict) -> list[dict]:
    raw = args.get("lines")
    if not isinstance(raw, list) or not raw:
        raise InvalidArgs("'lines' must be a non-empty list")
    out = []
    for i, ln in enumerate(raw):
        if not isinstance(ln, dict):
            raise InvalidArgs(f"line {i + 1} must be an object")
        unknown = set(ln) - {"item_code", "qty", "rate", "description"}
        if unknown:
            raise InvalidArgs(f"line {i + 1} has unknown field(s): {', '.join(sorted(unknown))}")
        out.append({
            "item_code": _text(ln, "item_code", required=True, max_len=140),
            "qty": format(_decimal(ln.get("qty"), "qty"), "f"),
            "rate": format(_decimal(ln.get("rate"), "rate"), "f"),
            "description": _text(ln, "description", max_len=500),
        })
    return out


_FIELDS: dict[str, dict[str, Callable]] = {
    "create_purchase_order": {
        "supplier": lambda a: _text(a, "supplier", required=True, max_len=140),
        "cost_centre": lambda a: _text(a, "cost_centre", required=True, max_len=140),
        "lines": _lines,
        "required_by": lambda a: _text(a, "required_by", max_len=10),
        "notes": lambda a: _text(a, "notes"),
    },
    "onboard_supplier": {
        "name": lambda a: _text(a, "name", required=True, max_len=140),
        "tax_id": lambda a: _text(a, "tax_id", max_len=40),
        "notes": lambda a: _text(a, "notes"),
    },
    "submit_for_approval": {"po": lambda a: _text(a, "po", required=True, max_len=140)},
    "approve_purchase_order": {
        "po": lambda a: _text(a, "po", required=True, max_len=140),
        "comment": lambda a: _text(a, "comment"),
    },
    "reject_purchase_order": {
        "po": lambda a: _text(a, "po", required=True, max_len=140),
        "reason": lambda a: _text(a, "reason", required=True),
    },
    "cancel_purchase_order": {
        "po": lambda a: _text(a, "po", required=True, max_len=140),
        "reason": lambda a: _text(a, "reason", required=True),
    },
    "amend_purchase_order": {
        "po": lambda a: _text(a, "po", required=True, max_len=140),
        "lines": _lines,
        "notes": lambda a: _text(a, "notes"),
    },
}


def canonical_args(action: str, args: dict) -> dict:
    if action not in _FIELDS:
        raise InvalidArgs(f"Unknown action '{action}'. One of: {', '.join(WRITE_ACTIONS)}")
    if not isinstance(args, dict):
        raise InvalidArgs("args must be an object")
    spec = _FIELDS[action]
    unknown = set(args) - set(spec) - {"confirmation_code"}
    if unknown:
        raise InvalidArgs(f"Unknown argument(s) for {action}: {', '.join(sorted(unknown))}")
    return {k: fn(args) for k, fn in spec.items()}


def payload_hash(action: str, actor: str, canon: dict) -> str:
    blob = json.dumps({"action": action, "actor": actor, "args": canon}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


# --- confirmation codes --------------------------------------------------------------

@dataclass
class _Issued:
    action: str
    actor: str
    digest: str
    expires: datetime


class CodeBook:
    """One-time codes bound to (action, actor, payload hash). Burned on first use."""

    def __init__(self, ttl: timedelta, now: Callable[[], datetime] = datetime.now):
        self.ttl, self.now = ttl, now
        self._issued: dict[str, _Issued] = {}

    def issue(self, action: str, actor: str, digest: str) -> str:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        self._issued[code] = _Issued(action, actor, digest, self.now() + self.ttl)
        return code

    def redeem(self, code: str | None, action: str, actor: str, digest: str) -> str | None:
        """None if the code is good for exactly this write; otherwise why not."""
        if not code:
            return "No confirmation code. Call preview first, check the results, then pass " \
                   "the code it returns."
        issued = self._issued.pop(code.strip().upper(), None)
        if issued is None:
            return "Unknown or already-used confirmation code. Codes are single-use; " \
                   "preview again."
        if self.now() > issued.expires:
            return "Confirmation code expired. Preview again."
        if (issued.action, issued.actor) != (action, actor):
            return "This code was issued for a different action or user."
        if issued.digest != digest:
            return "The arguments differ from what was previewed. Preview the exact " \
                   "arguments you intend to write."
        return None


# --- results -------------------------------------------------------------------------

@dataclass
class Preview:
    action: str
    args: dict
    summary: str
    checks: list[C.CheckResult]
    code: str | None

    @property
    def blocked(self) -> bool:
        return C.blocked(self.checks)


@dataclass
class Outcome:
    action: str
    outcome: str  # written | blocked | refused | error
    message: str
    checks: list[C.CheckResult] = field(default_factory=list)
    target: str | None = None
    audit_id: str | None = None


# --- the service ---------------------------------------------------------------------

class Service:
    def __init__(self, erp: ErpConnector, policy: Policy,
                 today: Callable[[], date] = date.today,
                 now: Callable[[], datetime] = datetime.now):
        self.erp, self.policy, self.today = erp, policy, today
        self.codes = CodeBook(timedelta(minutes=policy.code_ttl_minutes), now)
        self._actor: str | None = None

    @property
    def actor(self) -> str:
        # Identity comes from the ERP credentials, never from a tool argument.
        if self._actor is None:
            self._actor = self.erp.whoami()
        return self._actor

    # snapshot + checks per action -----------------------------------------------------

    def fy_start(self) -> date:
        t, m = self.today(), self.policy.fy_start_month
        return date(t.year if t.month >= m else t.year - 1, m, 1)

    def _proposed(self, canon: dict, base: PurchaseOrder | None = None) -> PurchaseOrder:
        lines = tuple(Line(ln["item_code"], Decimal(ln["qty"]), Decimal(ln["rate"]),
                           ln["description"]) for ln in canon["lines"])
        if base is not None:
            return PurchaseOrder(base.id, base.supplier_id, base.cost_centre, base.requester,
                                 base.status, base.order_date, lines, canon.get("notes", ""))
        return PurchaseOrder("(new)", canon["supplier"], canon["cost_centre"], self.actor,
                             Status.DRAFT, self.today(), lines, canon.get("notes", ""))

    def _order_checks(self, order: PurchaseOrder, with_prices: bool) -> list[C.CheckResult]:
        """Split, budget and (for new lines) price, against fresh ERP data."""
        today = self.today()
        since = min(today - timedelta(days=self.policy.split_window_days), self.fy_start())
        cc_orders = self.erp.list_orders(cost_centre=order.cost_centre, since=since)
        snap = Snapshot(actor=self.actor, today=today,
                        cost_centre=self.erp.get_cost_centre(order.cost_centre))
        snap.committed = sum(
            (o.total for o in cc_orders
             if o.id != order.id and o.status in LIVE_STATUSES
             and o.order_date >= self.fy_start()),
            Decimal(0),
        )
        out = [C.split_purchase(order, cc_orders, self.policy, today), C.budget(order, snap)]
        if with_prices:
            out.insert(0, C.items_known(order, self.erp.item_codes()))
            prices = self.erp.last_purchase_prices(sorted({ln.item_code for ln in order.lines}))
            out.append(C.price_vs_last(order, prices, self.policy))
        return out

    def _texts(self, canon: dict) -> dict[str, str]:
        texts = {k: canon[k] for k in ("notes", "name", "reason", "comment") if canon.get(k)}
        for i, ln in enumerate(canon.get("lines", [])):
            if ln["description"]:
                texts[f"line {i + 1} description"] = ln["description"]
        return texts

    def evaluate(self, action: str, canon: dict) -> tuple[str, list[C.CheckResult]]:
        """(human summary, check results) for a proposed write. Reads only."""
        actor = self.actor
        if action == "onboard_supplier":
            res = [C.duplicate_supplier(canon["name"], canon["tax_id"] or None,
                                        self.erp.list_suppliers(), self.policy)]
            res += C.free_text(self._texts(canon))
            tid = f", tax ID {canon['tax_id']}" if canon["tax_id"] else ""
            return f"Create supplier '{canon['name']}'{tid}, onboarded by {actor}.", res

        if action == "create_purchase_order":
            order = self._proposed(canon)
            sup = self.erp.get_supplier(order.supplier_id)
            res = [C.supplier_usable(sup, order.supplier_id),
                   C.self_dealing(actor, sup, "requester")]
            res += self._order_checks(order, with_prices=True)
            res += C.free_text(self._texts(canon))
            name = sup.name if sup else order.supplier_id
            return (f"Create draft PO to {name} ({order.supplier_id}) on cost centre "
                    f"{order.cost_centre}: {_describe_lines(order)}; total "
                    f"${order.total:,.2f}, needs {self.policy.required_tier(order.total).name} "
                    f"approval. Requester: {actor}."), res

        order = self.erp.get_order(canon["po"])
        ref = canon["po"]
        if action == "submit_for_approval":
            res = [C.state(order, ref, {Status.DRAFT, Status.REJECTED}, "submitting")]
            if order is not None:
                res += self._order_checks(order, with_prices=False)
            return f"Submit {ref} for approval.", res

        if action in ("approve_purchase_order", "reject_purchase_order"):
            verb = "approve" if action == "approve_purchase_order" else "reject"
            res = [C.state(order, ref, {Status.PENDING},
                           "approval" if verb == "approve" else "rejection")]
            if order is not None:
                res.append(C.segregation_of_duties(actor, order))
                if verb == "approve":
                    res.append(C.approval_authority(actor, order, self.policy))
                    res.append(C.self_dealing(actor, self.erp.get_supplier(order.supplier_id),
                                              "approver"))
                    res += self._order_checks(order, with_prices=False)
            res += C.free_text(self._texts(canon))
            total = f" (${order.total:,.2f})" if order else ""
            return f"{verb.capitalize()} {ref}{total} as {actor}.", res

        if action == "cancel_purchase_order":
            res = [C.state(order, ref, LIVE_STATUSES | {Status.REJECTED}, "cancelling")]
            if order is not None and order.requester != actor and \
                    self.policy.approver_limit(actor) is None:
                res.append(C.CheckResult("ownership", C.BLOCK,
                                         f"Only the requester or an approver can cancel {ref}."))
            res += C.free_text(self._texts(canon))
            return f"Cancel {ref}. Nothing is deleted; the order stays on record.", res

        if action == "amend_purchase_order":
            res = [C.state(order, ref, {Status.DRAFT, Status.PENDING, Status.REJECTED},
                           "amending")]
            if order is not None:
                if order.requester != actor:
                    res.append(C.CheckResult("ownership", C.BLOCK,
                                             f"Only the requester can amend {ref}."))
                new = self._proposed(canon, base=order)
                res += self._order_checks(new, with_prices=True)
                res += C.free_text(self._texts(canon))
                return (f"Amend {ref}: {_describe_lines(new)}; total ${new.total:,.2f} (was "
                        f"${order.total:,.2f}). Goes back to Draft and needs resubmitting."), res
            return f"Amend {ref}.", res

        raise InvalidArgs(f"Unknown action '{action}'")

    # public API -----------------------------------------------------------------------

    def preview(self, action: str, args: dict) -> Preview:
        canon = canonical_args(action, args)
        summary, results = self.evaluate(action, canon)
        code = None
        if not C.blocked(results):
            code = self.codes.issue(action, self.actor, payload_hash(action, self.actor, canon))
        return Preview(action, canon, summary, results, code)

    def execute(self, action: str, args: dict, confirmation_code: str | None) -> Outcome:
        actor = self.actor
        try:
            canon = canonical_args(action, args)
        except InvalidArgs as exc:
            po = args.get("po") if isinstance(args, dict) else None
            return self._audit(Outcome(action, "refused", str(exc)), actor, "", po)
        digest = payload_hash(action, actor, canon)
        target = canon.get("po")

        why = self.codes.redeem(confirmation_code, action, actor, digest)
        if why:
            return self._audit(Outcome(action, "refused", why, target=target), actor, digest)

        _, results = self.evaluate(action, canon)  # fresh data, not the preview's
        if C.blocked(results):
            msg = "Blocked at write time: " + " ".join(
                r.message for r in results if r.level == C.BLOCK)
            return self._audit(Outcome(action, "blocked", msg, results, target), actor, digest)

        try:
            target, msg = self._write(action, canon)
        except ErpError as exc:
            return self._audit(Outcome(action, "error", f"ERP error: {exc}", results, target),
                               actor, digest)
        return self._audit(Outcome(action, "written", msg, results, target), actor, digest)

    def _write(self, action: str, canon: dict) -> tuple[str, str]:
        erp = self.erp
        if action == "onboard_supplier":
            s = erp.create_supplier(canon["name"], canon["tax_id"] or None, canon["notes"],
                                    self.actor)
            return s.id, f"Supplier {s.name} created as {s.id}."
        if action == "create_purchase_order":
            order = self._proposed(canon)
            req = date.fromisoformat(canon["required_by"]) if canon["required_by"] \
                else self.today() + timedelta(days=14)
            po = erp.create_order(order.supplier_id, order.cost_centre, list(order.lines),
                                  canon["notes"], req)
            return po.id, f"Draft purchase order {po.id} created (${po.total:,.2f})."
        po_id = canon["po"]
        if action == "submit_for_approval":
            erp.set_status(po_id, Status.PENDING, "")
            return po_id, f"{po_id} submitted for approval."
        if action == "approve_purchase_order":
            erp.approve_order(po_id)
            return po_id, f"{po_id} approved."
        if action == "reject_purchase_order":
            erp.set_status(po_id, Status.REJECTED, canon["reason"])
            return po_id, f"{po_id} rejected."
        if action == "cancel_purchase_order":
            erp.cancel_order(po_id, canon["reason"])
            return po_id, f"{po_id} cancelled."
        if action == "amend_purchase_order":
            order = self._proposed(canon, base=erp.get_order(po_id))
            erp.amend_order(po_id, list(order.lines), canon["notes"])
            return po_id, f"{po_id} amended and back in Draft."
        raise InvalidArgs(action)

    def _audit(self, out: Outcome, actor: str, digest: str, target: str | None = None) -> Outcome:
        rec = AuditRecord(action=out.action, actor=actor, outcome=out.outcome,
                          payload_hash=digest, checks=[r.to_dict() for r in out.checks],
                          target=out.target or target, detail=out.message[:1000])
        try:
            out.audit_id = self.erp.write_audit(rec)
        except ErpError as exc:
            # The write (if any) already happened; say loudly that the trail is missing.
            out.message += f" WARNING: audit record could not be written ({exc})."
        return out


def _describe_lines(order: PurchaseOrder) -> str:
    return ", ".join(f"{ln.qty} x {ln.item_code} @ ${ln.rate:,.2f}" for ln in order.lines)
