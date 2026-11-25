"""How the duplicate-supplier check does on spelling variants it hasn't seen.

For each of the 200 seeded supplier names, make one realistic data-entry variant
(dropped letter, swapped letters, legal-suffix change, punctuation noise, doubled
letter: Spend Leakage Finder's perturbation set) and onboard it against the master.
Reported: share blocked, share only warned, share missed, and wrong matches (the
variant matched a different supplier). Synthetic variants of real names.

    python -m evals.matching
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from erp_guard import demo
from erp_guard.matching import match_supplier_name
from erp_guard.policy import load_policy

OUT = Path(__file__).resolve().parent / "results" / "matching.json"


def perturb(name: str, rng: random.Random) -> str:
    choice = rng.randrange(5)
    s = name
    if choice == 0 and len(s) > 6:  # dropped character
        p = rng.randrange(1, len(s) - 1)
        s = s[:p] + s[p + 1:]
    elif choice == 1 and len(s) > 6:  # adjacent swap
        p = rng.randrange(1, len(s) - 2)
        s = s[:p] + s[p + 1] + s[p] + s[p + 2:]
    elif choice == 2:  # legal suffix variant
        s = re.sub(r"\b(INC|LLC|CORP|CO)\.?$", "", s, flags=re.IGNORECASE).strip(" ,")
        s = s + rng.choice([" INC", " LLC", ", INC.", " CORPORATION", " CO"])
    elif choice == 3:  # punctuation / ampersand noise
        s = s.replace(" AND ", " & ").replace(",", "").replace(".", "") + "."
    else:  # doubled letter
        letters = [i for i, c in enumerate(s) if c.isalpha()]
        if letters:
            p = rng.choice(letters)
            s = s[:p] + s[p] + s[p:]
    return s


def main() -> dict:
    policy = load_policy()
    master = [(r["name"], r["name"]) for r in demo.seed_suppliers("master")]
    rng = random.Random(7)
    counts = {"block": 0, "warn": 0, "miss": 0, "wrong_match": 0}
    by_kind: dict[str, dict[str, int]] = {}
    examples = []
    kinds = ["dropped", "swapped", "suffix", "punctuation", "doubled"]
    for sid, name in master:
        state = rng.getstate()
        kind = kinds[rng.randrange(5)]  # peek at the choice perturb() is about to make
        rng.setstate(state)
        variant = perturb(name, rng)
        if variant == name:
            continue
        hits = match_supplier_name(variant, master, warn_ratio=policy.dup_warn_ratio)
        top = hits[0] if hits else None
        if top is None:
            level = "miss"
        elif top.supplier_id != sid and not any(h.supplier_id == sid for h in hits):
            level = "wrong_match"
        else:
            level = top.level if top.supplier_id == sid else next(
                h.level for h in hits if h.supplier_id == sid)
        counts[level] += 1
        by_kind.setdefault(kind, {"block": 0, "warn": 0, "miss": 0, "wrong_match": 0})
        by_kind[kind][level] += 1
        if level in ("miss", "wrong_match") and len(examples) < 10:
            examples.append({"master": name, "variant": variant, "result": level})
    n = sum(counts.values())
    res = {"n_variants": n, **counts,
           "blocked_pct": round(100 * counts["block"] / n, 1),
           "caught_pct": round(100 * (counts["block"] + counts["warn"]) / n, 1),
           "by_kind": by_kind, "examples": examples}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2) + "\n")
    return res


if __name__ == "__main__":
    r = main()
    print(f"{r['n_variants']} variants: {r['block']} blocked ({r['blocked_pct']}%), "
          f"{r['warn']} warned, {r['miss']} missed, {r['wrong_match']} matched the wrong "
          f"supplier; caught (block or warn) {r['caught_pct']}%")
