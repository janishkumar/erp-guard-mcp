"""Build data/seed/suppliers.csv from Spend Leakage Finder's resolved Chicago vendor names.

Source: City of Chicago vendor payments (Socrata s4vu-giwb), as resolved by
spend-leakage-finder into data/processed/name_map.parquet (one row per raw spelling,
with the cluster it was resolved to). Only organisations are used, never individuals.

Three roles:
- master:   goes into the demo company's supplier master.
- variant:  another real spelling of a master supplier, taken from the same ledger
            cluster. Used as a duplicate-onboarding attempt in tests and evals.
- control:  a real organisation that is not in the master and not similar to anything
            in it. Onboarding it must succeed.

Tax IDs are synthetic (prefix 00-, which the IRS never issues) and labelled so.

Usage: uv run --extra seed python scripts/build_seed.py [path/to/name_map.parquet]
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from po_guard.matching import is_org, match_supplier_name, normalise_name  # noqa: E402

DEFAULT_SRC = (Path(__file__).resolve().parents[2]
               / "spend-leakage-finder/data/processed/name_map.parquet")
OUT = Path(__file__).resolve().parents[1] / "data/seed/suppliers.csv"

N_MASTER = 200
N_VARIANT = 40
N_CONTROL = 25

# Public bodies, funds and banks aren't suppliers a buyer would raise POs to.
_NOT_SUPPLIER = re.compile(
    r"\b(DEPT|DEPARTMENT|STATE|COUNTY|BOARD|FUND|PENSION|TRUST|BANK|TREASURER|SECRETARY|"
    r"CITY OF|UNIV|UNIVERSITY|COLLEGE|SCHOOL|CHURCH|RETIREMENT|ANNUITY|COURT|CLERK|"
    r"COMMISSION|AUTHORITY|DISTRICT|HOUSING|MINISTRIES|LOCAL \d+|UNION)\b"
)


def is_supplier_like(raw: str) -> bool:
    up = raw.upper()
    return is_org(raw) and not _NOT_SUPPLIER.search(up) and 6 <= len(raw) <= 60


def synthetic_tax_id(name: str) -> str:
    h = int(hashlib.sha256(name.encode()).hexdigest(), 16)
    return f"00-{h % 10_000_000:07d}"


def build(src: Path) -> pd.DataFrame:
    nm = pd.read_parquet(src)
    nm = nm[nm["vendor_name_raw"].map(is_supplier_like)].copy()
    nm["vendor_name_raw"] = nm["vendor_name_raw"].str.strip()
    by_cluster = nm.sort_values(["n_payments", "vendor_name_raw"], ascending=[False, True])

    # Clusters where the ledger itself holds more than one spelling of one vendor.
    multi = by_cluster.groupby("vendor_id").filter(lambda g: g["vendor_name_raw"].nunique() > 1)
    totals = multi.groupby("vendor_id")["n_payments"].sum().sort_values(ascending=False)
    rows, used = [], set()
    for vid in totals.index[:N_VARIANT]:
        g = multi[multi["vendor_id"] == vid]
        master = g.iloc[0]["vendor_name_raw"]
        variant = next(v for v in g["vendor_name_raw"].iloc[1:] if v != master)
        rows.append({"name": master, "role": "master", "variant_of": "",
                     "source_vendor_id": vid})
        rows.append({"name": variant, "role": "variant", "variant_of": master,
                     "source_vendor_id": vid})
        used.add(vid)

    # Fill the master with the best-paid other suppliers, one spelling per cluster.
    singles = by_cluster[~by_cluster["vendor_id"].isin(used)].drop_duplicates("vendor_id")
    for r in singles.head(N_MASTER - len(used)).itertuples():
        rows.append({"name": r.vendor_name_raw, "role": "master", "variant_of": "",
                     "source_vendor_id": r.vendor_id})
        used.add(r.vendor_id)

    master = [(str(i), r["name"]) for i, r in enumerate(rows) if r["role"] == "master"]
    master_norm = {normalise_name(n) for _, n in master}
    # Controls: mid-sized real suppliers with nothing similar in the master.
    pool = by_cluster[~by_cluster["vendor_id"].isin(used)].drop_duplicates("vendor_id")
    pool = pool[pool["n_payments"].between(5, 200)].sample(frac=1.0, random_state=7)
    n = 0
    for r in pool.itertuples():
        if normalise_name(r.vendor_name_raw) in master_norm:
            continue
        if match_supplier_name(r.vendor_name_raw, master):
            continue
        rows.append({"name": r.vendor_name_raw, "role": "control", "variant_of": "",
                     "source_vendor_id": r.vendor_id})
        n += 1
        if n == N_CONTROL:
            break

    df = pd.DataFrame(rows)
    df["synthetic_tax_id"] = [synthetic_tax_id(x) if role == "master" else ""
                              for x, role in zip(df["name"], df["role"], strict=True)]
    return df


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    if not src.exists():
        raise SystemExit(f"{src} not found. Run spend-leakage-finder's pipeline first.")
    df = build(src)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(df["role"].value_counts().to_string())
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
