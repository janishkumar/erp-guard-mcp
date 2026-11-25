"""Supplier-name matching, from Spend Leakage Finder's vendor resolution.

The normalisation and token guards below are copied unchanged from
spend-leakage-finder (src/leakage/vendors.py), where they were tuned on 76,555 real
City of Chicago payee spellings. That project matches every name against every other
with TF-IDF candidate generation; here a single new name is compared against a supplier
master of a few thousand rows, so plain rapidfuzz scoring is enough.

Two tiers:
- block: the pair passes Spend Leakage Finder's guarded match (`accept_pair`).
- warn:  a looser token-set similarity, for a reviewer to glance at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein

LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "LLC", "LLP", "LP", "LTD", "LIMITED", "CO", "COMPANY",
    "CORP", "CORPORATION", "PC", "PLLC", "PLC", "NA", "GROUP",
}
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r"\s+")
_NUMBERS = re.compile(r"\d+")


HONORIFICS = {"MR", "MRS", "MS", "DR", "MISS"}
_GENERATIONAL = re.compile(r"^(JR|SR|[IVX]{1,4})$")


def normalise_name(name: str) -> str:
    s = str(name).upper()
    s = s.replace("&", " AND ")
    s = re.sub(r"\bL\.?\s?L\.?\s?C\b\.?", " LLC ", s)
    s = _PUNCT.sub(" ", s)
    tokens = _SPACES.sub(" ", s).strip().split(" ")
    if tokens and tokens[0] == "THE" and len(tokens) > 1:
        tokens = tokens[1:]
    tokens = [t for t in tokens if t not in HONORIFICS] or tokens
    # Strip trailing legal suffixes, but never strip a name down to nothing.
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _numbers(name: str) -> tuple[str, ...]:
    return tuple(_NUMBERS.findall(name))


def _last_char_substitution(x: str, y: str) -> bool:
    return len(x) == len(y) and x[:-1] == y[:-1] and x[-1] != y[-1]


def _token_close(x: str, y: str, allow_truncation: bool = True) -> bool:
    if x == y:
        return True
    if x[0] != y[0]:  # keying errors rarely hit the first letter; MORRIS != NORRIS
        return False
    if allow_truncation and min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x)):
        return True  # truncation: COMM / COMMERCE
    if _last_char_substitution(x, y):  # MECCON / MECCOR are two different firms
        return False
    if min(len(x), len(y)) <= 6:  # short brand tokens: GRANT != GRANITE, CTS != CBTS
        return False
    # RESIDENTIAL vs RESIDENTS is a different word, not a typo.
    return Levenshtein.distance(x, y) <= 2 and fuzz.ratio(x, y) >= 80


ORG_WORDS = {
    "ASSOC", "ASSOCIATION", "ASSOCIATES", "SERVICES", "SERVICE", "CONSTRUCTION", "DEVELOPMENT",
    "SOCIETY", "CENTER", "CENTRE", "FOUNDATION", "CHURCH", "PARTNERS", "BANK", "UNIVERSITY",
    "COLLEGE", "HOSPITAL", "SCHOOL", "COUNCIL", "DEPT", "DEPARTMENT", "ILLINOIS", "CHICAGO",
    "AMERICA", "AMERICAN", "NATIONAL", "COMMUNITY", "SUPPLY", "SUPPLIES", "SYSTEMS", "SOLUTIONS",
    "TECHNOLOGIES", "TECHNOLOGY", "CONSULTING", "MANAGEMENT", "HOUSING", "INSURANCE", "TRUST",
    "FUND", "AUTHORITY", "AGENCY", "INTERNATIONAL", "ENTERPRISES", "INDUSTRIES", "HOLDINGS",
    "PROPERTIES", "REALTY", "COMMERCE", "CHAMBER", "LAW", "OFFICE", "OFFICES", "CLUB", "CITY",
    "COUNTY", "STATE", "INSTITUTE", "PRODUCTS", "EQUIPMENT", "ELECTRIC", "MOTORS", "AND", "OF",
    "LLC", "INC", "CORP", "CO", "COMPANY", "LTD", "GROUP", "LP", "LLP",
}


def is_org(raw_or_norm: str) -> bool:
    tokens = _PUNCT.sub(" ", raw_or_norm.upper()).split()
    long_tokens = [t for t in tokens if len(t) > 1]
    return len(long_tokens) >= 4 or any(t in ORG_WORDS for t in tokens)


def _person_compatible(ca: list[str], cb: list[str]) -> bool:
    """Same tokens except one single-character typo inside a long-enough token.

    An edit at the last character is refused: ANDREA/ANDREW, GABRIEL/GABRIELA and
    WALTER/WALTERS are usually different people.
    """
    for xs, ys in ((ca, cb), (sorted(ca), sorted(cb))):
        diffs = [(x, y) for x, y in zip(xs, ys, strict=True) if x != y]
        if not diffs:
            return True
        if len(diffs) == 1:
            x, y = diffs[0]
            if (
                min(len(x), len(y)) >= 7
                and x[0] == y[0]
                and x[-1] == y[-1]
                and Levenshtein.distance(x, y) == 1
            ):
                return True
    return False


def tokens_compatible(a: str, b: str, org: bool | None = None) -> bool:
    """Token-level guard against merging different people or numbered entities.

    Half of a municipal ledger's payees are individuals (refunds, settlements), where
    'KENNETH B SMITH' vs 'KENNETH D SMITH' is two people, not a typo. Individuals get a
    much stricter rule than organisations.
    """
    if org is None:
        org = is_org(a) or is_org(b)
    ta, tb = a.split(), b.split()
    # 'HOWARD ERNESTLEON' == 'HOWARD ERNEST LEON', but 'JUAN A' must not become 'JUANA'.
    if a.replace(" ", "") == b.replace(" ", "") and (
        org or not any(len(t) == 1 for t in ta + tb)
    ):
        return True
    gen_a = {t for t in ta if _GENERATIONAL.match(t)}
    gen_b = {t for t in tb if _GENERATIONAL.match(t)}
    if gen_a != gen_b:
        return False
    ini_a = {t for t in ta if len(t) == 1}
    ini_b = {t for t in tb if len(t) == 1}
    if ini_a != ini_b and (not org or (ini_a and ini_b)):
        return False
    ca = [t for t in ta if len(t) > 1 and t not in gen_a]
    cb = [t for t in tb if len(t) > 1 and t not in gen_b]
    if len(ca) != len(cb) or not ca:
        return False
    if not org:
        return _person_compatible(ca, cb)
    # Truncation is fine for trailing words (DEVEL / DEVELOPMENT) but not for the
    # leading brand word: FARO != FARONICS, HEALTH PARTNERS != HEALTHCARE PARTNERS.
    in_order = all(
        _token_close(x, y, allow_truncation=i > 0)
        for i, (x, y) in enumerate(zip(ca, cb, strict=True))
    )
    return in_order or all(
        _token_close(x, y, allow_truncation=False)
        for x, y in zip(sorted(ca), sorted(cb), strict=True)
    )


@dataclass(frozen=True)
class MatchConfig:
    cosine_min: float = 0.75
    ratio_min: float = 85.0
    min_len: int = 6


def accept_pair(a: str, b: str, cfg: MatchConfig) -> tuple[bool, float]:
    if min(len(a), len(b)) < cfg.min_len:
        return False, 0.0
    if _numbers(a) != _numbers(b):
        return False, 0.0
    ratio = fuzz.ratio(a, b)
    return ratio >= cfg.ratio_min and tokens_compatible(a, b), ratio


def _prefilter_score(a: str, b: str, **_) -> float:
    return max(fuzz.token_set_ratio(a, b), fuzz.ratio(a.replace(" ", ""), b.replace(" ", "")))


@dataclass(frozen=True)
class NameMatch:
    supplier_id: str
    name: str
    score: float
    level: str  # block | warn


def match_supplier_name(name: str, master: list[tuple[str, str]], warn_ratio: float = 90.0,
                        cfg: MatchConfig | None = None) -> list[NameMatch]:
    """Compare one proposed supplier name against (id, name) pairs in the master.

    Returns block-level matches first, then warn-level ones, best score first.
    """
    cfg = cfg or MatchConfig()
    norm = normalise_name(name)
    if not norm or not master:
        return []
    normed = [normalise_name(n) for _, n in master]
    # Cheap pre-filter, then the guarded rule on the survivors. The second score catches a
    # lost or extra space ("UNISONCONSULTING"), which token-set scoring rates badly.
    hits = process.extract(norm, normed, scorer=_prefilter_score, limit=25,
                           score_cutoff=min(warn_ratio, cfg.ratio_min) - 10)
    out: list[NameMatch] = []
    for cand, score, idx in hits:
        sid, raw = master[idx]
        if cand == norm:
            out.append(NameMatch(sid, raw, 100.0, "block"))
            continue
        ok, ratio = accept_pair(norm, cand, cfg)
        if ok:
            out.append(NameMatch(sid, raw, round(ratio, 1), "block"))
        elif score >= warn_ratio and _numbers(norm) == _numbers(cand):
            out.append(NameMatch(sid, raw, round(score, 1), "warn"))
    return sorted(out, key=lambda m: (m.level != "block", -m.score))
