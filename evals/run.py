"""Run the guardrail eval and write evals/results/<erp>-<mode>.json.

    python -m evals.run --erp memory            # guarded + naive baseline, in memory (CI)
    python -m evals.run --erp erpnext           # guarded, against the local ERPNext
    python -m evals.run --erp erpnext --mode naive

Against ERPNext, run on a freshly seeded instance (make reset up seed): onboarded
suppliers can't be deleted, so a second run would see the first run's suppliers. Scenarios
marked `erp_only` (the generic layer) run only against ERPNext; the in-memory run leaves
them out.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from erp_guard import server
from erp_guard.env import load_env
from evals.scenarios import SCENARIOS
from evals.world import World

RESULTS = Path(__file__).resolve().parent / "results"


def run(erp: str, guarded: bool, only: str | None = None, quiet: bool = False) -> dict:
    rows = []
    shared = World.erpnext(guarded) if erp == "erpnext" else None
    for sc in SCENARIOS:
        if (only and only not in sc.name) or (sc.erp_only and erp != "erpnext"):
            continue
        w = shared or World.memory(guarded)
        t0 = time.perf_counter()
        try:
            w.begin()
            hit = bool(sc.run(w))
            err = None
        except Exception as exc:  # a scenario crash is reported, never counted as safe
            hit, err = None, f"{type(exc).__name__}: {exc}"
            if not quiet:
                traceback.print_exc()
        finally:
            try:
                w.cleanup()
            except Exception as exc:
                err = (err or "") + f" | cleanup: {exc}"
        rows.append({"name": sc.name, "kind": sc.kind, "area": sc.area,
                     "category": sc.category, "erp_only": sc.erp_only,
                     "what": sc.what, "result": hit, "error": err,
                     "seconds": round(time.perf_counter() - t0, 2)})
        if not quiet:
            mark = {True: "LANDED" if sc.kind == "attack" else "ok",
                    False: "stopped" if sc.kind == "attack" else "FAILED", None: "ERROR"}[hit]
            print(f"  {mark:8} {sc.name}")
    server.set_service(None)
    return summarise(erp, guarded, rows)


def summarise(erp: str, guarded: bool, rows: list[dict]) -> dict:
    attacks = [r for r in rows if r["kind"] == "attack"]
    legit = [r for r in rows if r["kind"] == "legit"]
    by_cat: dict[str, Counter] = {}
    for r in attacks:
        c = by_cat.setdefault(r["category"], Counter(total=0, landed=0))
        c["total"] += 1
        c["landed"] += bool(r["result"])
    by_area: dict[str, Counter] = {}
    for r in rows:
        c = by_area.setdefault(r["area"], Counter(attacks=0, landed=0, legit=0,
                                                  legit_completed=0))
        if r["kind"] == "attack":
            c["attacks"] += 1
            c["landed"] += bool(r["result"])
        else:
            c["legit"] += 1
            c["legit_completed"] += bool(r["result"])
    return {
        "erp": erp, "mode": "guarded" if guarded else "naive",
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "attacks": len(attacks),
        "unsafe_writes_landed": sum(bool(r["result"]) for r in attacks),
        "legit": len(legit),
        "legit_completed": sum(bool(r["result"]) for r in legit),
        "errors": sum(r["result"] is None for r in rows),
        "by_area": {k: dict(v) for k, v in by_area.items()},
        "by_category": {k: dict(v) for k, v in sorted(by_cat.items())},
        "scenarios": rows,
    }


def _onboarded_by_erp_guard() -> bool:
    from erp_guard.connectors.erpnext import ErpNext
    return any(s.onboarded_by for s in ErpNext.from_env("DIRECTOR").list_suppliers())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--erp", choices=["memory", "erpnext"], default="memory")
    ap.add_argument("--mode", choices=["guarded", "naive", "both"])
    ap.add_argument("--only", help="run scenarios whose name contains this")
    ap.add_argument("--dirty", action="store_true",
                    help="run against an ERPNext that earlier evals already wrote to")
    args = ap.parse_args(argv)
    mode = args.mode or ("both" if args.erp == "memory" else "guarded")
    if args.erp == "erpnext":
        load_env()
        if not args.dirty and _onboarded_by_erp_guard():
            print("This ERPNext already has suppliers onboarded through erp-guard, so duplicate "
                  "and onboarding scenarios would be judged against an earlier run. Start "
                  "fresh with `make eval-live-fresh` (or pass --dirty).")
            return 2
    logging.getLogger("httpx").setLevel(logging.WARNING)
    RESULTS.mkdir(exist_ok=True)
    out = {}
    for guarded in ([True, False] if mode == "both" else [mode == "guarded"]):
        label = "guarded" if guarded else "naive"
        print(f"\n== {args.erp} / {label} ==")
        res = run(args.erp, guarded, args.only)
        out[label] = res
        if not args.only:
            suffix = ".live.json" if args.erp == "erpnext" else ".json"
            (RESULTS / f"{args.erp}-{label}{suffix}").write_text(json.dumps(res, indent=2) + "\n")
        print(f"   unsafe writes landed: {res['unsafe_writes_landed']}/{res['attacks']}; "
              f"legit flows completed: {res['legit_completed']}/{res['legit']}; "
              f"errors: {res['errors']}")
        for area, c in res["by_area"].items():
            print(f"     {area:20} landed {c['landed']}/{c['attacks']}; "
                  f"legit {c['legit_completed']}/{c['legit']}")
    # Exit non-zero only if a scenario crashed. Whether too many unsafe writes landed is
    # judged by tests/test_evals.py, against the documented known misses.
    return 1 if any(r["errors"] for r in out.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
