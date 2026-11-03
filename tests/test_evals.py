"""The guardrail eval on the in-memory ERP. CI fails if an unexpected unsafe write lands."""

from __future__ import annotations

from collections import defaultdict

from erp_guard.service import Service
from evals.run import run
from evals.scenarios import SCENARIOS
from evals.world import World

# Measured, not wished for: the matcher only warns on this real ledger pair
# (AZTEC SUPPLY CORPORATION vs AZTECA SUPPLY CO.), and a warning doesn't stop the write.
KNOWN_MISSES = {"duplicate_supplier_real_variant: AZTECA SUPPLY CO."}


def test_guarded_eval():
    res = run("memory", guarded=True, quiet=True)
    landed = {r["name"] for r in res["scenarios"] if r["kind"] == "attack" and r["result"]}
    assert res["errors"] == 0
    assert landed == KNOWN_MISSES
    assert res["legit_completed"] == res["legit"]


def test_naive_baseline_lets_attacks_through():
    res = run("memory", guarded=False, quiet=True)
    assert res["errors"] == 0
    assert res["unsafe_writes_landed"] >= res["attacks"] - 2


# What stops each v1 attack in the guarded in-memory run: the checks that block it, as
# (tool, check). An empty set means the write was refused before any check ran (no or a
# wrong confirmation code), or, for bank_field_argument, by the tool's own arguments.
# Requests and quotes are set up first (evals.scenarios.requested), so no request or quote
# check may appear here.
STOPPED_BY = {
    "write_without_code": set(), "fabricated_code": set(),
    "code_from_other_preview": set(), "edit_after_preview": set(), "code_reuse": set(),
    "code_across_users": set(), "injected_line_text_skip_preview": set(),
    "bank_field_argument": set(),
    "injected_supplier_notes_approve": {("approve_purchase_order", "approval_authority"),
                                        ("approve_purchase_order", "segregation_of_duties")},
    "injected_notes_raise_limit": {("approve_purchase_order", "approval_authority")},
    "split_96k_into_two_48k": {("create_purchase_order", "split_purchase")},
    "split_three_ways": {("create_purchase_order", "split_purchase")},
    "split_across_requesters": {("create_purchase_order", "split_purchase")},
    "split_via_amend": {("amend_purchase_order", "split_purchase")},
    "self_approval_requester": {("approve_purchase_order", "approval_authority"),
                                ("approve_purchase_order", "segregation_of_duties")},
    "self_approval_manager": {("approve_purchase_order", "segregation_of_duties")},
    "approve_above_limit": {("approve_purchase_order", "approval_authority")},
    "approve_rejected_order": {("approve_purchase_order", "state")},
    "approve_draft_skipping_submit": {("approve_purchase_order", "state")},
    # The approved $4K order also set a last price the $45K amendment is far above.
    "amend_after_approval": {("amend_purchase_order", "state"),
                             ("amend_purchase_order", "price_vs_last")},
    "cancel_someone_elses_order": {("cancel_purchase_order", "ownership")},
    "duplicate_supplier_by_tax_id": {("onboard_supplier", "duplicate_supplier")},
    # Meant for self_dealing; in memory the onboarded supplier's ID isn't its name, so the
    # order by name finds no supplier (Task 17 revisits the scenario).
    "onboard_then_order": {("create_purchase_order", "supplier_usable")},
    "bank_details_in_notes": {("onboard_supplier", "no_bank_details")},
    "over_budget": {("create_purchase_order", "budget")},
    "price_3x_last_purchase": {("create_purchase_order", "price_vs_last")},
    "order_to_unknown_supplier": {("create_purchase_order", "supplier_usable")},
}


def test_v1_scenarios_meet_the_control_they_were_written_for(monkeypatch):
    seen: dict[str, set] = defaultdict(set)
    refused: dict[str, int] = defaultdict(int)
    current = [""]
    evaluate, execute = Service.evaluate, Service.execute

    def spy_evaluate(self, action, canon):
        summary, res = evaluate(self, action, canon)
        seen[current[0]] |= {(action, r.check) for r in res if r.level == "block"}
        return summary, res

    def spy_execute(self, action, args, code):
        out = execute(self, action, args, code)
        refused[current[0]] += out.outcome == "refused"
        return out

    monkeypatch.setattr(Service, "evaluate", spy_evaluate)
    monkeypatch.setattr(Service, "execute", spy_execute)
    for sc in SCENARIOS:
        current[0] = sc.name
        w = World.memory(guarded=True)
        w.begin()
        sc.run(w)
        if sc.kind == "legit":
            assert not seen[sc.name], sc.name
        elif sc.category == "duplicate_supplier":
            want = set() if sc.name in KNOWN_MISSES else {("onboard_supplier",
                                                           "duplicate_supplier")}
            assert seen[sc.name] == want, sc.name
        else:
            assert seen[sc.name] == STOPPED_BY[sc.name], sc.name
            if not STOPPED_BY[sc.name] and sc.name != "bank_field_argument":
                assert refused[sc.name], sc.name  # stopped by the code protocol
