"""Checks for generic writes: pure functions over a document's metadata and proposed fields.

Nothing here touches the ERP. Existence of linked records comes in through a callable so
the service can answer it with a connector read.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from erp_guard.checks import BANK_KEY, BLOCK, PASS, CheckResult, _has_bank_details, free_text
from erp_guard.generic.meta import SYSTEM_FIELDS, TABLE_TYPES, DocMeta, FieldMeta
from erp_guard.policy import Policy

NUMBER_TYPES = frozenset({"Int", "Float", "Currency", "Percent"})
TEXT_TYPES = frozenset({"Data", "Text", "Small Text", "Long Text", "Text Editor"})
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_EMPTY = (None, "")


def _empty(v) -> bool:
    return v in _EMPTY or v == []


def _number(value, integral: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return False
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        return False
    if not d.is_finite():
        return False
    return not integral or d == d.to_integral_value()


def _type_problem(fm: FieldMeta, value) -> str | None:
    """Why `value` doesn't fit the field's type, or None."""
    t = fm.fieldtype
    if t in TABLE_TYPES:
        return None  # rows are checked against the child meta
    if isinstance(value, dict | list):
        return "must be a single value"
    if t in NUMBER_TYPES:
        if not _number(value, integral=t == "Int"):
            return f"must be {'a whole number' if t == 'Int' else 'a number'}, got {value!r}"
    elif t == "Check":
        if value not in (0, 1, True, False, "0", "1"):
            return f"must be 0 or 1, got {value!r}"
    elif t == "Date":
        try:
            if not (isinstance(value, str) and _ISO_DATE.fullmatch(value)):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            return f"must be a date as YYYY-MM-DD, got {value!r}"
    elif t == "Datetime":
        try:
            if not isinstance(value, str) or not _ISO_DATE.match(value):
                raise ValueError
            datetime.fromisoformat(value)
        except ValueError:
            return f"must be a date and time as YYYY-MM-DD HH:MM:SS, got {value!r}"
    elif t == "Time":
        try:
            if not isinstance(value, str):
                raise ValueError
            time.fromisoformat(value)
        except ValueError:
            return f"must be a time as HH:MM:SS, got {value!r}"
    elif t == "Select":
        options = [o.strip() for o in fm.options.split("\n") if o.strip()]
        if options and str(value) not in options:
            return f"must be one of {', '.join(options)}, got {value!r}"
    elif t in ("Link", "Dynamic Link") and not isinstance(value, str):
        return f"must be the name of a record, got {value!r}"
    return None


def _row_problems(meta: DocMeta, row, path: str, for_create: bool) -> list[str]:
    if not isinstance(row, dict):
        return [f"{path} must be an object"]
    out = []
    for key, value in row.items():
        if key in SYSTEM_FIELDS:
            continue  # system_fields reports these
        if key == "doctype":
            if value != meta.doctype:
                out.append(f"{path}.doctype must be {meta.doctype}, got {value!r}")
            continue
        fm = meta.fields.get(key)
        if fm is None:
            out.append(f"{path}.{key} is not a field of {meta.doctype}")
            continue
        if fm.fieldtype in TABLE_TYPES:
            out += _table_problems(meta, key, fm, value, f"{path}.{key}")
        elif not _empty(value):
            problem = _type_problem(fm, value)
            if problem:
                out.append(f"{path}.{key} {problem}")
    for key, fm in meta.fields.items():
        if not fm.reqd or fm.server_filled or fm.read_only:
            continue
        if (key not in row and for_create) or (key in row and _empty(row[key])):
            out.append(f"{path}.{key} is required")
    return out


def _table_problems(meta: DocMeta, key: str, fm: FieldMeta, value, path: str) -> list[str]:
    if not isinstance(value, list):
        return [f"{path} must be a list of rows"]
    child = meta.children.get(key)
    if child is None:
        if value:
            return [f"{path}: no metadata for table {fm.options or key}, so its rows "
                    "can't be checked"]
        return []
    out = []
    for i, row in enumerate(value):
        # Rows replace the table wholesale, so each is checked as a new row.
        out += _row_problems(child, row, f"{path}[{i}]", for_create=True)
    return out


def known_fields(meta: DocMeta, fields: dict, for_create: bool) -> list[CheckResult]:
    """Every field exists, has the right type, and (on create) the required ones are there."""
    problems = _row_problems(meta, fields, meta.doctype, for_create)
    if not problems:
        return [CheckResult("known fields", PASS, f"All fields fit {meta.doctype}.")]
    return [CheckResult("known fields", BLOCK, p, {"doctype": meta.doctype}) for p in problems]


def _link_targets(meta: DocMeta, row: dict, path: str):
    for key, value in row.items():
        fm = meta.fields.get(key)
        if fm is None:
            continue
        if fm.fieldtype in TABLE_TYPES:
            child = meta.children.get(key)
            if child and isinstance(value, list):
                for i, r in enumerate(value):
                    if isinstance(r, dict):
                        yield from _link_targets(child, r, f"{path}.{key}[{i}]")
        elif fm.fieldtype in ("Link", "Dynamic Link") and isinstance(value, str) and value:
            target = fm.options
            if fm.fieldtype == "Dynamic Link":
                target = row.get(fm.options)
            if isinstance(target, str) and target:
                yield f"{path}.{key}", target, value


def links_exist(meta: DocMeta, fields: dict,
                exists: Callable[[str, str], bool]) -> list[CheckResult]:
    """Every non-empty Link value names a record that exists."""
    out, asked = [], {}
    for path, doctype, name in _link_targets(meta, fields, meta.doctype):
        if (doctype, name) not in asked:
            asked[(doctype, name)] = exists(doctype, name)
        if not asked[(doctype, name)]:
            out.append(CheckResult("links", BLOCK, f"{path}: no {doctype} named '{name}'.",
                                   {"doctype": doctype, "name": name}))
    return out or [CheckResult("links", PASS, "Every linked record exists.")]


def _system_paths(obj, path: str):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SYSTEM_FIELDS:
                yield f"{path}.{k}" if path else k
            yield from _system_paths(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _system_paths(v, f"{path}[{i}]")


def system_fields(fields: dict) -> CheckResult:
    """Server-owned fields (owner, docstatus, parent ...) are never written, in any row."""
    found = sorted(_system_paths(fields, ""))
    if found:
        return CheckResult(
            "system fields", BLOCK,
            f"System fields can't be set: {', '.join(found)}. The ERP controls them.",
            {"fields": found})
    return CheckResult("system fields", PASS, "No system fields in the request.")


def _locked_problems(meta: DocMeta, fields: dict, path: str) -> list[str]:
    out = []
    for key, value in fields.items():
        fm = meta.fields.get(key)
        where = f"{path}.{key}" if path else key
        if fm is None or not fm.allow_on_submit:
            out.append(where)
        elif fm.fieldtype in TABLE_TYPES:
            child = meta.children.get(key)
            rows = value if isinstance(value, list) else []
            if child is None:
                out.append(where)
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    out.append(f"{where}[{i}]")
                elif child is not None:
                    out += _locked_problems(child, row, f"{where}[{i}]")
    return out


def locked(meta: DocMeta, current: dict | None, fields: dict) -> CheckResult:
    """A submitted document takes only allow-on-submit fields; a cancelled one nothing."""
    status = (current or {}).get("docstatus") or 0
    if status == 2:
        return CheckResult("locked document", BLOCK,
                           f"This {meta.doctype} is cancelled and can't be changed. "
                           "Amend it instead.",
                           {"docstatus": 2})
    if status == 1:
        bad = _locked_problems(meta, fields, "")
        if bad:
            return CheckResult(
                "locked document", BLOCK,
                f"This {meta.doctype} is submitted; {', '.join(bad)} can't be changed after "
                "submission.", {"docstatus": 1, "fields": bad})
    return CheckResult("locked document", PASS, f"This {meta.doctype} can be edited.")


def _bank_paths(obj, path: str):
    if isinstance(obj, dict):
        for k, v in obj.items():
            where = f"{path}.{k}" if path else str(k)
            if not _empty(v) and BANK_KEY.search(str(k)):
                yield where
            else:
                yield from _bank_paths(v, where)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _bank_paths(v, f"{path}[{i}]")
    elif isinstance(obj, str) and _has_bank_details(obj):
        yield path


def bank_details(fields: dict) -> CheckResult:
    """No bank fields, and no bank details written into any text, however deep."""
    found = sorted(_bank_paths(fields, ""))
    if found:
        return CheckResult(
            "bank details", BLOCK,
            f"Bank details in {', '.join(found)}. erp-guard never writes bank details; they go "
            "through the ERP's own verified vendor-banking process.", {"fields": found})
    return CheckResult("bank details", PASS, "No bank details in the request.")


def second_person(actor: str, current: dict, doctype: str, policy: Policy) -> CheckResult:
    """On listed document types, the person who created a document can't also act on it."""
    if doctype in policy.second_person and current.get("owner") == actor:
        return CheckResult(
            "second person", BLOCK,
            f"{actor} created this {doctype} and can't also act on it. Someone else has to.",
            {"owner": actor})
    return CheckResult("second person", PASS, "No second-person rule applies, or it is met.")


def _text_values(meta: DocMeta, row: dict, path: str, out: dict[str, str]) -> None:
    for key, value in row.items():
        fm = meta.fields.get(key)
        where = f"{path}.{key}" if path else key
        if fm is None:
            continue
        if fm.fieldtype in TEXT_TYPES and isinstance(value, str):
            out[where] = value
        elif fm.fieldtype in TABLE_TYPES and isinstance(value, list):
            child = meta.children.get(key)
            for i, r in enumerate(value):
                if child and isinstance(r, dict):
                    _text_values(child, r, f"{where}[{i}]", out)


def instruction_text(meta: DocMeta, fields: dict) -> list[CheckResult]:
    """Warn when free text reads like instructions to an assistant. Bank details in text are
    refused by `bank_details`, so that half of `free_text` is dropped here."""
    texts: dict[str, str] = {}
    _text_values(meta, fields, "", texts)
    found = [r for r in free_text(texts) if r.check == "instruction_like_text"]
    return found or [CheckResult("instruction_like_text", PASS, "No instruction-like text.")]
