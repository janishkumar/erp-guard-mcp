"""Scope rules: which document types the write tools may touch, loaded from scope.toml.

The rules apply in order and the first match decides (docs/design.md, "Scope rules"):

1. never: single document types, the system modules and a list of names
2. pack: the document type is owned by a pack, which runs its own checks
3. later: owned by a later phase but posts nothing; generic writes until then
4. ledger: posts to the general or stock ledger; read-only until its phase
5. generic: everything else in the business modules

Anything else fails closed: a submittable business document type that nobody has reviewed
(not packed, later, ledger or safe) and any document type outside the business modules are
both refused.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Scope(StrEnum):
    NEVER = "never"
    PACK = "pack"
    LATER = "later"
    LEDGER = "ledger"
    GENERIC = "generic"


@dataclass(frozen=True)
class ScopeRules:
    never_modules: frozenset[str]
    never_doctypes: frozenset[str]
    later_doctypes: frozenset[str]
    ledger_doctypes: frozenset[str]
    safe_doctypes: frozenset[str]
    business_modules: frozenset[str]


def _default_path() -> Path:
    env = os.environ.get("ERP_GUARD_SCOPE")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[3] / "config" / "scope.toml"
    if repo.exists():
        return repo
    return Path(__file__).resolve().parents[1] / "scope.toml"  # installed wheel


def load_scope(path: Path | None = None) -> ScopeRules:
    raw = tomllib.loads((path or _default_path()).read_text())
    return ScopeRules(
        never_modules=frozenset(raw["never"]["modules"]),
        never_doctypes=frozenset(raw["never"]["doctypes"]),
        later_doctypes=frozenset(raw["later"]["doctypes"]),
        ledger_doctypes=frozenset(raw["ledger"]["doctypes"]),
        safe_doctypes=frozenset(raw["safe"]["doctypes"]),
        business_modules=frozenset(raw["business"]["modules"]),
    )


def classify(rules: ScopeRules, doctype: str, module: str, issingle: bool, pack_owned: bool,
             is_submittable: bool) -> Scope:
    """The scope of one document type, from its ERPNext metadata and pack ownership."""
    if issingle or module in rules.never_modules or doctype in rules.never_doctypes:
        return Scope.NEVER
    if pack_owned:
        return Scope.PACK
    if doctype in rules.later_doctypes:
        return Scope.LATER
    if doctype in rules.ledger_doctypes:
        return Scope.LEDGER
    if module in rules.business_modules:
        if is_submittable and doctype not in rules.safe_doctypes:
            return Scope.NEVER  # submittable and unreviewed: fail closed
        return Scope.GENERIC
    return Scope.NEVER


WRITABLE = frozenset({Scope.GENERIC, Scope.LATER, Scope.PACK})


def writable_predicate(rules: ScopeRules, meta_of: Callable[[str], dict],
                       pack_owned: Callable[[str], bool]) -> Callable[[str], bool]:
    """For the ERP client: may anything be written to this document type? `meta_of` returns
    the DocType document; a type whose metadata can't be read is not writable."""
    def writable(doctype: str) -> bool:
        try:
            m = meta_of(doctype)
        except Exception:  # noqa: BLE001 - unknown or unreadable: fail closed
            return False
        return classify(rules, doctype, m.get("module") or "", bool(m.get("issingle")),
                        pack_owned(doctype), bool(m.get("is_submittable"))) in WRITABLE
    return writable
