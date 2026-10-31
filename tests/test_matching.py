from __future__ import annotations

from po_guard import demo
from po_guard.matching import match_supplier_name, normalise_name

M = [("S1", "HERC RENTALS INC"), ("S2", "GRANITE CONSTRUCTION COMPANY."),
     ("S3", "LOCAL 150 OPERATING ENGINEERS"), ("S4", "UNISON CONSULTING, INC."),
     ("S5", "FARO TECHNOLOGIES")]


def top(name):
    hits = match_supplier_name(name, M)
    return (hits[0].supplier_id, hits[0].level) if hits else None


def test_formatting_noise_blocks():
    assert top("Herc Rentals, Inc.") == ("S1", "block")
    assert top("THE HERC RENTALS LLC") == ("S1", "block")


def test_typos_and_lost_spaces_block():
    assert top("Granite Constructon Co") == ("S2", "block")
    assert top("UNISONCONSULTING INC") == ("S4", "block")


def test_guards_from_spend_leakage_finder_hold():
    assert top("LOCAL 130 OPERATING ENGINEERS") is None  # numbers must agree
    assert top("FARONICS TECHNOLOGIES") != ("S5", "block")  # leading-word truncation
    assert top("Grant Construction") == ("S2", "warn")  # GRANT != GRANITE, but worth a look


def test_unrelated_names_pass():
    assert top("Lakeview Office Furniture LLC") is None
    assert match_supplier_name("", M) == [] and normalise_name("The Acme Co.") == "ACME"


def test_seed_controls_have_no_match_and_variants_mostly_block():
    master = [(r["name"], r["name"]) for r in demo.seed_suppliers("master")]
    for c in demo.seed_suppliers("control"):
        assert not match_supplier_name(c["name"], master), c["name"]
    variants = demo.seed_suppliers("variant")
    blocked = sum(bool((h := match_supplier_name(v["name"], master)) and h[0].level == "block"
                       and h[0].supplier_id == v["variant_of"]) for v in variants)
    assert blocked == len(variants) - 1  # AZTECA SUPPLY CO. only warns (see README)
