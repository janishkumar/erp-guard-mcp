"""The guardrail eval on the in-memory ERP. CI fails if an unexpected unsafe write lands."""

from __future__ import annotations

from evals.run import run

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
