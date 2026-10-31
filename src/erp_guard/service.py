"""Preview -> one-time code -> re-check -> write -> audit. Every write goes through here."""

from __future__ import annotations

import hashlib
import importlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from erp_guard import checks as C
from erp_guard.connectors.base import ErpConnector, ErpError
from erp_guard.models import AuditRecord
from erp_guard.policy import Policy

if TYPE_CHECKING:
    from erp_guard.packs.base import Pack

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L

# Modules whose ACTIONS list joins the default registry. A new pack module adds itself here.
_ACTION_MODULES = ("erp_guard.packs.p2p.orders", "erp_guard.generic.actions")


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


def canonicaliser(action: str, spec: dict[str, Callable[[dict], object]]
                  ) -> Callable[[dict], dict]:
    """Canonicalise args field by field; any argument not in `spec` is refused."""
    def canonicalise(args: dict) -> dict:
        unknown = set(args) - set(spec) - {"confirmation_code"}
        if unknown:
            raise InvalidArgs(f"Unknown argument(s) for {action}: {', '.join(sorted(unknown))}")
        return {k: fn(args) for k, fn in spec.items()}
    return canonicalise


def canonical_args(action: str, args: dict, actions: dict[str, Action] | None = None) -> dict:
    if actions is None:
        actions = {a.name: a for a in default_actions()}
    if action not in actions:
        raise InvalidArgs(f"Unknown action '{action}'. One of: {', '.join(actions)}")
    if not isinstance(args, dict):
        raise InvalidArgs("args must be an object")
    return actions[action].canonicalise(args)


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


# --- actions -------------------------------------------------------------------------

@dataclass(frozen=True)
class Action:
    """One write the service can preview and execute."""

    name: str
    canonicalise: Callable[[dict], dict]
    evaluate: Callable[[Service, dict], tuple[str, list[C.CheckResult]]]  # reads only
    write: Callable[[Service, dict], tuple[str, str]]  # (target id, message)
    target_key: str | None = None  # canon key naming the target doc


def default_actions() -> list[Action]:
    """Every action of every pack. Imported lazily: action modules import this one."""
    out: list[Action] = []
    for mod in _ACTION_MODULES:
        out += importlib.import_module(mod).ACTIONS
    return out


def __getattr__(name: str):
    if name == "WRITE_ACTIONS":
        return tuple(a.name for a in default_actions())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --- the service ---------------------------------------------------------------------

class Service:
    def __init__(self, erp: ErpConnector, policy: Policy,
                 today: Callable[[], date] = date.today,
                 now: Callable[[], datetime] = datetime.now,
                 packs: list[Pack] | None = None):
        self.erp, self.policy, self.today = erp, policy, today
        self.codes = CodeBook(timedelta(minutes=policy.code_ttl_minutes), now)
        self._actor: str | None = None
        if packs is None:
            from erp_guard.packs.p2p.pack import P2PPack
            packs = [P2PPack()]
        # Generic writes to a document type a pack owns are handed to that pack.
        self.packs: list[Pack] = packs
        self.actions: dict[str, Action] = {}
        self.register(*default_actions())

    def register(self, *actions: Action) -> None:
        for a in actions:
            if a.name in self.actions:
                raise ValueError(f"Action '{a.name}' is already registered")
            self.actions[a.name] = a

    @property
    def actor(self) -> str:
        # Identity comes from the ERP credentials, never from a tool argument.
        if self._actor is None:
            self._actor = self.erp.whoami()
        return self._actor

    def fy_start(self) -> date:
        t, m = self.today(), self.policy.fy_start_month
        return date(t.year if t.month >= m else t.year - 1, m, 1)

    def evaluate(self, action: str, canon: dict) -> tuple[str, list[C.CheckResult]]:
        """(human summary, check results) for a proposed write. Reads only."""
        return self.actions[action].evaluate(self, canon)

    # public API -----------------------------------------------------------------------

    def preview(self, action: str, args: dict) -> Preview:
        canon = canonical_args(action, args, self.actions)
        summary, results = self.evaluate(action, canon)
        code = None
        if not C.blocked(results):
            code = self.codes.issue(action, self.actor, payload_hash(action, self.actor, canon))
        return Preview(action, canon, summary, results, code)

    def execute(self, action: str, args: dict, confirmation_code: str | None) -> Outcome:
        actor = self.actor
        try:
            canon = canonical_args(action, args, self.actions)
        except InvalidArgs as exc:
            key = a.target_key if (a := self.actions.get(action)) else None
            ref = args.get(key) if key and isinstance(args, dict) else None
            return self._audit(Outcome(action, "refused", str(exc)), actor, "", ref)
        digest = payload_hash(action, actor, canon)
        key = self.actions[action].target_key
        target = canon.get(key) if key else None

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
        return self.actions[action].write(self, canon)

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

