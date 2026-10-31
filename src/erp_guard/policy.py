"""Approval tiers, tolerances and windows, loaded from policy.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Tier:
    name: str
    limit: Decimal


@dataclass(frozen=True)
class QuotesPolicy:
    threshold: Decimal
    count: int
    lookback_days: int


@dataclass(frozen=True)
class InvoicePolicy:
    price_tolerance_pct: Decimal
    dup_amount_days: int
    near_days: int
    near_pct: Decimal


@dataclass(frozen=True)
class PaymentPolicy:
    dup_days: int
    new_supplier_days: int
    bank_account: str


# Document kinds that carry approval tiers. A purchase request is approved on the
# purchase order's tiers.
_TIER_KEY = {"purchase_request": "purchase_order"}


@dataclass(frozen=True)
class Policy:
    approval: dict[str, tuple[Tier, ...]]
    split_window_days: int
    price_warn_pct: Decimal
    price_block_pct: Decimal
    dup_warn_ratio: float
    fy_start_month: int
    code_ttl_minutes: int
    request_required_from: Decimal
    quotes: QuotesPolicy
    over_receipt_pct: Decimal
    invoice: InvoicePolicy
    payment: PaymentPolicy
    second_person: set[str]
    roles: dict[str, set[str]]
    users: dict[str, dict]

    def _tiers(self, kind: str) -> tuple[Tier, ...]:
        key = _TIER_KEY.get(kind, kind)
        if key not in self.approval:
            raise KeyError(f"no approval tiers for document kind {kind!r}")
        return self.approval[key]

    def required_tier(self, total: Decimal, kind: str = "purchase_order") -> Tier:
        tiers = self._tiers(kind)
        for tier in tiers:
            if total <= tier.limit:
                return tier
        return tiers[-1]

    def approver_limit(self, user: str, kind: str = "purchase_order") -> Decimal | None:
        """The user's approval limit for this kind, or None if they can't approve it."""
        name = self.users.get(user, {}).get(_TIER_KEY.get(kind, kind))
        if name is None:
            return None
        for tier in self._tiers(kind):
            if tier.name == name:
                return tier.limit
        return None


def _default_path() -> Path:
    env = os.environ.get("ERP_GUARD_POLICY")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[2] / "config" / "policy.toml"
    if repo.exists():
        return repo
    return Path(__file__).with_name("policy.toml")  # installed wheel


def load_policy(path: Path | None = None) -> Policy:
    raw = tomllib.loads((path or _default_path()).read_text())
    approval = {
        kind: tuple(Tier(t["name"], Decimal(str(t["limit"]))) for t in section["tiers"])
        for kind, section in raw["approval"].items()
    }
    users = dict(raw.get("users", {}))
    return Policy(
        approval=approval,
        split_window_days=int(raw["split"]["window_days"]),
        price_warn_pct=Decimal(str(raw["price"]["warn_above_pct"])),
        price_block_pct=Decimal(str(raw["price"]["block_above_pct"])),
        dup_warn_ratio=float(raw["duplicate_supplier"]["warn_token_set_ratio"]),
        fy_start_month=int(raw["fiscal_year"]["start_month"]),
        code_ttl_minutes=int(raw["confirmation"]["ttl_minutes"]),
        request_required_from=Decimal(str(raw["request"]["required_from"])),
        quotes=QuotesPolicy(
            threshold=Decimal(str(raw["quotes"]["threshold"])),
            count=int(raw["quotes"]["count"]),
            lookback_days=int(raw["quotes"]["lookback_days"]),
        ),
        over_receipt_pct=Decimal(str(raw["receipt"]["over_receipt_pct"])),
        invoice=InvoicePolicy(
            price_tolerance_pct=Decimal(str(raw["invoice"]["price_tolerance_pct"])),
            dup_amount_days=int(raw["invoice"]["duplicate_amount_days"]),
            near_days=int(raw["invoice"]["near_days"]),
            near_pct=Decimal(str(raw["invoice"]["near_pct"])),
        ),
        payment=PaymentPolicy(
            dup_days=int(raw["payment"]["duplicate_days"]),
            new_supplier_days=int(raw["payment"]["new_supplier_days"]),
            bank_account=str(raw["payment"]["bank_account"]),
        ),
        second_person=set(raw.get("generic", {}).get("second_person", [])),
        roles={u: set(info.get("roles", [])) for u, info in users.items()},
        users=users,
    )
