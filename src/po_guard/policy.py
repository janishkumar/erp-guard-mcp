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
class Policy:
    tiers: tuple[Tier, ...]
    split_window_days: int
    price_warn_pct: Decimal
    price_block_pct: Decimal
    dup_warn_ratio: float
    fy_start_month: int
    code_ttl_minutes: int
    users: dict[str, dict]

    def required_tier(self, total: Decimal) -> Tier:
        for tier in self.tiers:
            if total <= tier.limit:
                return tier
        return self.tiers[-1]

    def approver_limit(self, user: str) -> Decimal | None:
        """The user's approval limit, or None if they can't approve at all."""
        info = self.users.get(user, {})
        if info.get("role") != "approver":
            return None
        for tier in self.tiers:
            if tier.name == info.get("tier"):
                return tier.limit
        return None


def _default_path() -> Path:
    env = os.environ.get("PO_GUARD_POLICY")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[2] / "config" / "policy.toml"
    if repo.exists():
        return repo
    return Path(__file__).with_name("policy.toml")  # installed wheel


def load_policy(path: Path | None = None) -> Policy:
    raw = tomllib.loads((path or _default_path()).read_text())
    tiers = tuple(
        Tier(t["name"], Decimal(str(t["limit"]))) for t in raw["approval"]["tiers"]
    )
    return Policy(
        tiers=tiers,
        split_window_days=int(raw["split"]["window_days"]),
        price_warn_pct=Decimal(str(raw["price"]["warn_above_pct"])),
        price_block_pct=Decimal(str(raw["price"]["block_above_pct"])),
        dup_warn_ratio=float(raw["duplicate_supplier"]["warn_token_set_ratio"]),
        fy_start_month=int(raw["fiscal_year"]["start_month"]),
        code_ttl_minutes=int(raw["confirmation"]["ttl_minutes"]),
        users=dict(raw.get("users", {})),
    )
