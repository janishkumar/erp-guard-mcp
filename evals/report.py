"""Write the README's measured sections from evals/results/*.json.

Replaces the text between <!-- name:start --> and <!-- name:end --> markers, so every
number in those sections comes from a result file produced by code in this repo.

    python -m evals.report
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "evals" / "results"
README = ROOT / "README.md"

CATEGORY = {
    "protocol": "Confirmation-code protocol (no code, fake code, reused or swapped code, "
                "edited after preview)",
    "injection": "Prompt injection in supplier notes and order text",
    "split": "Split purchases under the approval limit",
    "sod": "Segregation of duties and approval authority",
    "supplier": "Supplier master (tax-ID duplicate, self-dealing, bank details, unknown "
                "supplier)",
    "duplicate_supplier": "Duplicate supplier, real Chicago spelling variants",
    "budget": "Budget per cost centre",
    "price": "Price vs last purchase",
}
# Illustrative only: the share of PO value that is leakage of the kinds po-guard checks.
LEAKAGE_RATES = [0.001, 0.0025, 0.005, 0.01]
ANNUAL_PO_SPEND = 100_000_000


def _load(name: str) -> dict | None:
    p = RES / name
    return json.loads(p.read_text()) if p.exists() else None


def eval_section() -> str:
    live_g, live_n = _load("erpnext-guarded.live.json"), _load("erpnext-naive.live.json")
    mem_g, mem_n = _load("memory-guarded.json"), _load("memory-naive.json")
    g, n = (live_g, live_n) if live_g else (mem_g, mem_n)
    where = "live ERPNext v16 in Docker" if live_g else "the in-memory ERP"
    lines = [
        f"Measured on {where} ({g['run_at']}). {g['attacks']} attack scenarios, "
        f"{g['legit']} legitimate flows. Ground truth is ERP state after each scenario.",
        "",
        "| | With po-guard | Same tools, no guardrails |",
        "|---|---:|---:|",
        f"| Unsafe writes that landed | **{g['unsafe_writes_landed']} / {g['attacks']}** | "
        f"{n['unsafe_writes_landed']} / {n['attacks']} |" if n else
        f"| Unsafe writes that landed | **{g['unsafe_writes_landed']} / {g['attacks']}** | n/a |",
        f"| Legitimate flows completed | {g['legit_completed']} / {g['legit']} | "
        + (f"{n['legit_completed']} / {n['legit']} |" if n else "n/a |"),
        "",
        "| Attack category | Scenarios | Landed with po-guard | Landed without |",
        "|---|---:|---:|---:|",
    ]
    for cat, c in g["by_category"].items():
        nc = n["by_category"].get(cat, {}) if n else {}
        lines.append(f"| {CATEGORY.get(cat, cat)} | {c['total']} | {c['landed']} | "
                     f"{nc.get('landed', 'n/a')} |")
    missed = [r for r in g["scenarios"] if r["kind"] == "attack" and r["result"]]
    if missed:
        lines += ["", "What got through with po-guard:", ""]
        lines += [f"- `{r['name']}`: {r['what']}" for r in missed]
    if n:
        held = [r for r in n["scenarios"] if r["kind"] == "attack" and not r["result"]]
        if held:
            who = "ERPNext's own validation or the tool schema" if live_g else "the tool schema"
            lines += ["", f"Without guardrails, {who} still stopped {len(held)}: "
                          + "; ".join(f"`{r['name']}`" for r in held) + "."]
    if live_g and mem_g:
        lines += ["", f"The in-memory run CI does on every push: "
                      f"{mem_g['unsafe_writes_landed']} / {mem_g['attacks']} landed, "
                      f"{mem_g['legit_completed']} / {mem_g['legit']} legitimate flows "
                      "completed."]
    return "\n".join(lines)


def matching_section() -> str:
    m = _load("matching.json")
    if not m:
        return "_Run `python -m evals.matching`._"
    return (f"On {m['n_variants']} unseen spelling variants of the seeded supplier names "
            f"(dropped, swapped or doubled letters, suffix and punctuation changes): "
            f"**{m['block']} blocked ({m['blocked_pct']}%)**, {m['warn']} warned, "
            f"{m['miss']} missed, {m['wrong_match']} matched the wrong supplier. Blocked or "
            f"warned: {m['caught_pct']}%.")


def roi_section() -> str:
    g = _load("erpnext-guarded.live.json") or _load("memory-guarded.json")
    stop = 1 - g["unsafe_writes_landed"] / g["attacks"]
    shares = [0.25, 0.5, 1.0]
    lines = [
        f"Illustrative, for ${ANNUAL_PO_SPEND / 1e6:.0f}M of annual PO spend. Two assumptions "
        "drive it: how much PO value is leakage of the kinds po-guard checks, and how much of "
        "the PO volume is raised through po-guard. The only measured input is the eval's stop "
        f"rate ({stop:.1%} of attack scenarios), and real leakage is messier than the "
        "scenarios, so read each cell as an upper bound. Software cost is $0.", "",
        "| Leakage (% of PO value) | " + " | ".join(
            f"{s_:.0%} of POs through po-guard" for s_ in shares) + " |",
        "|---:|" + "---:|" * len(shares)]
    for r in LEAKAGE_RATES:
        cells = [f"${ANNUAL_PO_SPEND * r * s_ * stop:,.0f}" for s_ in shares]
        lines.append(f"| {r:.2%} | " + " | ".join(cells) + " |")
    lines += ["", "Cells are leakage stopped before the PO exists, per year."]
    return "\n".join(lines)


def main() -> None:
    text = README.read_text()
    for name, fn in (("eval", eval_section), ("matching", matching_section),
                     ("roi", roi_section)):
        pat = re.compile(rf"(<!-- {name}:start -->\n).*?(\n<!-- {name}:end -->)", re.S)
        text, k = pat.subn(lambda m, fn=fn: m.group(1) + fn() + m.group(2), text)
        if not k:
            raise SystemExit(f"README has no {name} markers")
    README.write_text(text)
    print("README sections updated")


if __name__ == "__main__":
    main()
