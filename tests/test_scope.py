import json
from pathlib import Path

import pytest

from erp_guard.generic.scope import Scope, classify, load_scope
from erp_guard.packs.p2p import P2P_OWNED

FIXTURE = Path(__file__).parent / "fixtures" / "erpnext" / "doctypes.json"
# Recorded from ERPNext v16.52.0: every non-child DocType with its module and flags.
DOCTYPES = {d["name"]: d for d in json.loads(FIXTURE.read_text())["message"]}


@pytest.fixture(scope="module")
def rules():
    return load_scope()


def scope_of(rules, name, pack_owned=False):
    d = DOCTYPES[name]
    return classify(rules, name, d["module"], bool(d["issingle"]), pack_owned,
                    bool(d["is_submittable"]))


def test_never_rules(rules):
    assert scope_of(rules, "User") is Scope.NEVER                # Core module
    assert scope_of(rules, "Server Script") is Scope.NEVER       # Core module
    assert scope_of(rules, "System Settings") is Scope.NEVER     # single
    assert scope_of(rules, "Bank Account") is Scope.NEVER        # by name, Accounts
    assert scope_of(rules, "Company") is Scope.NEVER             # by name, Setup
    assert scope_of(rules, "Budget") is Scope.NEVER              # by name, submittable
    assert scope_of(rules, "Accounts Settings") is Scope.NEVER   # single in a business module
    for name in ("Accounting Dimension", "Inventory Dimension",          # customisation
                 "Email Digest", "Process Statement Of Accounts", "Email Campaign"):  # scheduled
        assert scope_of(rules, name) is Scope.NEVER, name


def test_never_beats_pack_ownership(rules):
    assert scope_of(rules, "ERP Guard Audit", pack_owned=True) is Scope.NEVER
    assert classify(rules, "Webhook", "Integrations", False, True, False) is Scope.NEVER


def test_ledger_read_only(rules):
    for name in ("Journal Entry", "Sales Invoice", "Stock Entry"):
        assert scope_of(rules, name) is Scope.LEDGER, name


def test_money_masters_read_only(rules):
    for name in ("Pricing Rule", "Party Link", "Purchase Taxes and Charges Template"):
        assert scope_of(rules, name) is Scope.LEDGER, name


def test_pack_owned(rules):
    assert scope_of(rules, "Purchase Invoice", pack_owned=True) is Scope.PACK
    # Without the pack, a ledger-posting invoice is read-only, not generic.
    assert scope_of(rules, "Purchase Invoice") is Scope.LEDGER


def test_later_generic(rules):
    assert scope_of(rules, "Timesheet") is Scope.LATER
    assert scope_of(rules, "Customer") is Scope.LATER
    assert scope_of(rules, "Lead") is Scope.GENERIC              # CRM, not submittable
    assert scope_of(rules, "Quality Inspection Template") is Scope.GENERIC


def test_safe_submittable_is_generic(rules):
    assert scope_of(rules, "Maintenance Visit") is Scope.GENERIC


def test_fail_closed(rules):
    # A submittable business document nobody has reviewed is refused.
    assert classify(rules, "New Thing", "Stock", False, False, True) is Scope.NEVER
    # The same unreviewed name, not submittable, is generic.
    assert classify(rules, "New Thing", "Stock", False, False, False) is Scope.GENERIC
    # Outside the business modules (and not named in rule 1): refused.
    assert scope_of(rules, "Telephony Call Type") is Scope.NEVER
    assert classify(rules, "Anything", "Unknown Module", False, False, False) is Scope.NEVER


def test_every_submittable_classified(rules):
    unclassified = sorted(
        name for name, d in DOCTYPES.items()
        if d["is_submittable"] and not d["istable"] and d["module"] in rules.business_modules
        and name not in rules.never_doctypes                       # named in rule 1
        and scope_of(rules, name, pack_owned=name in P2P_OWNED) is Scope.NEVER
    )
    assert not unclassified, f"submittable business doctypes not classified: {unclassified}"


def test_listed_names_exist(rules):
    """A typo in scope.toml would silently leave a document type unlisted."""
    listed = (rules.never_doctypes | rules.later_doctypes | rules.ledger_doctypes
              | rules.safe_doctypes | P2P_OWNED)
    missing = sorted(listed - DOCTYPES.keys() - {"ERP Guard Change Request"})
    assert not missing, f"not a v16.52.0 doctype: {missing}"
    known_modules = {d["module"] for d in DOCTYPES.values()}
    assert not (rules.never_modules | rules.business_modules) - known_modules


def test_safe_list_is_submittable_only(rules):
    for name in rules.safe_doctypes:
        assert DOCTYPES[name]["is_submittable"], name
        assert DOCTYPES[name]["module"] in rules.business_modules, name


def test_lists_disjoint(rules):
    lists = [rules.never_doctypes, rules.later_doctypes, rules.ledger_doctypes,
             rules.safe_doctypes]
    for i, a in enumerate(lists):
        for b in lists[i + 1:]:
            assert not a & b, a & b
    assert not P2P_OWNED & (rules.never_doctypes | rules.later_doctypes | rules.safe_doctypes)


def test_load_scope_path_and_env(tmp_path, monkeypatch):
    p = tmp_path / "scope.toml"
    p.write_text(
        '[never]\nmodules = ["Core"]\ndoctypes = ["Company"]\n'
        '[later]\ndoctypes = ["Customer"]\n[ledger]\ndoctypes = ["Journal Entry"]\n'
        '[safe]\ndoctypes = []\n[business]\nmodules = ["Selling"]\n'
    )
    r = load_scope(p)
    assert r.never_modules == frozenset({"Core"})
    assert r.business_modules == frozenset({"Selling"})
    monkeypatch.setenv("ERP_GUARD_SCOPE", str(p))
    assert load_scope() == r
