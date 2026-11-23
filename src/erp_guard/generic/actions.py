"""The generic write tools: create, update, submit, workflow action, cancel and amend on any
document type the scope rules allow, through the same preview, code, re-check and audit
as every other write.

Each tool's arguments become a `Write`. The scope of its document type decides the path:
system administration and ledger-posting types are refused, pack-owned types go to the
pack's checks (with the generic field checks merged in), and everything else gets the
generic checks.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import TYPE_CHECKING

from erp_guard import checks as C
from erp_guard.checks import BLOCK, PASS, CheckResult
from erp_guard.connectors.base import ErpError
from erp_guard.generic import checks as G
from erp_guard.generic.meta import TABLE_TYPES, DocMeta, doc_meta
from erp_guard.generic.scope import Scope, ScopeRules, classify, load_scope
from erp_guard.ops import Op, Write
from erp_guard.service import Action, InvalidArgs, _decimal, _text, canonicaliser

if TYPE_CHECKING:
    from erp_guard.service import Service

Result = tuple[str, list[CheckResult]]

MAX_FIELDS_JSON = 100_000  # characters of canonical JSON in one write
NEED_ERPNEXT = "Generic tools need ERPNext; this connector has no document metadata."


@cache
def scope_rules() -> ScopeRules:
    return load_scope()


# --- canonical arguments -------------------------------------------------------------

def _canon_value(v, path: str):
    """Keys sorted, numbers as plain decimal text, so equal payloads hash equally."""
    if isinstance(v, dict):
        out = {}
        for k in sorted(v, key=str):
            if not isinstance(k, str) or not k:
                raise InvalidArgs(f"'{path}' has a key that isn't a field name: {k!r}")
            out[k] = _canon_value(v[k], f"{path}.{k}")
        return out
    if isinstance(v, list):
        return [_canon_value(x, f"{path}[{i}]") for i, x in enumerate(v)]
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, int | float):
        return format(_decimal(v, path), "f")
    raise InvalidArgs(f"'{path}' must be text, a number, true/false, a list or an object")


def _fields(a: dict, required: bool = True) -> dict:
    v = a.get("fields")
    if v is None and not required:
        return {}
    if not isinstance(v, dict):
        raise InvalidArgs("'fields' must be an object of field name to value")
    canon = _canon_value(v, "fields")
    if len(json.dumps(canon)) > MAX_FIELDS_JSON:
        raise InvalidArgs(f"'fields' is larger than {MAX_FIELDS_JSON} characters")
    return canon


def _doctype(a: dict) -> str:
    return _text(a, "doctype", required=True, max_len=140)


def _name(a: dict) -> str:
    return _text(a, "name", required=True, max_len=140)


# --- metadata and scope --------------------------------------------------------------

def meta_of(svc: Service, doctype: str) -> tuple[DocMeta, dict]:
    """The document type's metadata (raises ErpError if ERPNext can't give it)."""
    raw = svc.erp.get_meta(doctype)
    docs = [raw, *raw.get("child_doctypes", {}).values()]
    return doc_meta({"docs": docs}, has_workflow=bool(raw.get("__workflow_docs"))), raw


def scope_of(svc: Service, meta: DocMeta, w: Write | None = None) -> Scope:
    """The document type's scope; for a write, the pack that owns the type may decide this
    document isn't its business (e.g. an Address not linked to a supplier), and then the
    type is classified as if no pack owned it."""
    owned = svc.pack_owned(meta.doctype)
    if owned and w is not None:
        owned = svc.pack_for(meta.doctype).owns_write(svc, w)
    return classify(scope_rules(), meta.doctype, meta.module, meta.issingle, owned,
                    meta.is_submittable, istable=meta.istable)


def scope_message(meta: DocMeta, scope: Scope) -> str:
    rules = scope_rules()
    if scope is Scope.NEVER:
        if meta.istable:
            return (f"{meta.doctype} is a child table: child rows are written through their "
                    "parent document.")
        if meta.issingle or meta.module in rules.never_modules or \
                meta.doctype in rules.never_doctypes:
            return f"{meta.doctype}: system administration is never done through the MCP."
        return (f"{meta.doctype} is not a reviewed business document type, so it can't be "
                "written through the MCP.")
    if scope is Scope.LEDGER:
        return f"{meta.doctype} posts to the ledger; read-only until its phase."
    if scope is Scope.PACK:
        return f"{meta.doctype} is owned by a pack; its checks apply."
    if scope is Scope.LATER:
        return (f"{meta.doctype} belongs to a later phase and posts nothing; the generic "
                "checks apply until then.")
    return f"{meta.doctype} is writable with the generic checks."


def _write_scope_message(svc: Service, meta: DocMeta, scope: Scope) -> str:
    if scope is not Scope.PACK and svc.pack_owned(meta.doctype):
        return (f"{meta.doctype} is owned by the {svc.pack_for(meta.doctype).name} pack only "
                "in some cases, and this isn't one; the generic checks apply.")
    return scope_message(meta, scope)


# --- summaries -----------------------------------------------------------------------

def _norm(v):
    """A value as compared in a diff: empty is None, numbers by value (2.0 == "2")."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int | float | str):
        try:
            d = Decimal(str(v).strip())
        except InvalidOperation:
            return v
        if d.is_finite():
            return d.normalize()
    return v


def same_value(old, new) -> bool:
    return _norm(old) == _norm(new)


def _show(v) -> str:
    if v is None or v == "":
        return "(empty)"
    s = repr(v) if isinstance(v, str) else json.dumps(v)
    return s if len(s) <= 80 else s[:77] + "..."


def _rows_changed(old: list, new: list) -> tuple[int, int, int]:
    """(+added, ~changed, -removed); rows replace the table, so they pair by position."""
    old = [r for r in old if isinstance(r, dict)]
    changed = sum(1 for o, n in zip(old, new, strict=False) if not isinstance(n, dict)
                  or any(not same_value(o.get(k), v) for k, v in n.items()))
    return max(0, len(new) - len(old)), changed, max(0, len(old) - len(new))


def field_diff(meta: DocMeta, current: dict | None, fields: dict) -> list[str]:
    """`field: old → new` per changed field (every field for a new document); tables as
    +added / ~changed / -removed rows."""
    out = []
    for key in sorted(fields):
        new = fields[key]
        old = (current or {}).get(key)
        fm = meta.fields.get(key)
        if isinstance(new, list) and (fm is None or fm.fieldtype in TABLE_TYPES):
            a, c, r = _rows_changed(old if isinstance(old, list) else [], new)
            if a or c or r or current is None:
                out.append(f"{key}: +{a} rows / ~{c} changed / -{r} removed")
        elif current is None:
            out.append(f"{key}: {_show(new)}")
        elif not same_value(old, new):
            out.append(f"{key}: {_show(old)} → {_show(new)}")
    return out


# --- evaluate ------------------------------------------------------------------------

_VERB = {Op.CREATE: "Create", Op.UPDATE: "Update", Op.SUBMIT: "Submit",
         Op.WORKFLOW: "Workflow action on", Op.CANCEL: "Cancel", Op.AMEND: "Amend"}


def _label(w: Write) -> str:
    return f"{_VERB[w.op]} {w.doctype}" + (f" {w.name}" if w.name else "") + "."


def field_checks(meta: DocMeta, fields: dict, for_create: bool, required: bool = True
                 ) -> list[CheckResult]:
    """System fields, bank details, known fields and instruction-like text."""
    return [G.system_fields(fields), G.bank_details(fields),
            *G.known_fields(meta, fields, for_create, required=required),
            *G.instruction_text(meta, fields)]


def record_exists(svc: Service):
    """(doctype, name) -> whether the record exists and this user can read it."""
    def exists(doctype: str, name: str) -> bool:
        try:
            if doctype == "DocType":
                # A link to a document type (an address's link_doctype, say): only System
                # Manager reads DocType records, so ask for the type's metadata instead.
                return bool(svc.erp.get_meta(name))
            return svc.erp.get_doc(doctype, name) is not None
        except ErpError:
            return False  # not readable as this user: treated as not there
    return exists


def _texts(w: Write) -> list[CheckResult]:
    if not w.reason:
        return []
    return C.free_text({"comment" if w.op is Op.WORKFLOW else "reason": w.reason})


def _state(current: dict, want: int, doing: str) -> CheckResult:
    names = {0: "a draft", 1: "submitted", 2: "cancelled"}
    have = current.get("docstatus") or 0
    if have != want:
        return CheckResult("state", BLOCK,
                           f"{current.get('name')} is {names.get(have, have)}; {doing} needs "
                           f"it {names[want]}.", {"docstatus": have})
    return CheckResult("state", PASS, f"{current.get('name')} is {names[want]}.")


def _workflow_checks(svc: Service, meta: DocMeta, w: Write) -> list[CheckResult]:
    if not meta.has_workflow:
        return [CheckResult("workflow", BLOCK, f"{meta.doctype} has no approval workflow; "
                            "use submit_document to submit it.")]
    try:
        allowed = svc.erp.transitions(w.doctype, w.name)
    except ErpError as exc:
        return [CheckResult("workflow", BLOCK, f"No workflow transitions for {w.name}: {exc}")]
    if w.action not in allowed:
        listed = ", ".join(allowed) or "none"
        return [CheckResult("workflow", BLOCK,
                            f"'{w.action}' is not an available action on {w.name} for this "
                            f"user. Available: {listed}.", {"available": allowed})]
    return [CheckResult("workflow", PASS, f"'{w.action}' is an available action.")]


def generic_checks(svc: Service, meta: DocMeta, w: Write) -> Result:
    """(summary, the generic checks) for a write; packs that write a document as given
    (master data) run these too."""
    current = None
    if w.op is not Op.CREATE:
        current = svc.erp.get_doc(w.doctype, w.name)
        if current is None:
            return _label(w), [CheckResult("document", BLOCK, f"No {w.doctype} '{w.name}'.")]
    res: list[CheckResult] = []
    if w.op in (Op.CREATE, Op.UPDATE, Op.AMEND):
        res += field_checks(meta, w.fields, for_create=w.op is Op.CREATE)
        res += G.links_exist(meta, w.fields, record_exists(svc))
    if w.op is Op.UPDATE:
        res.append(G.locked(meta, current, w.fields))
    elif w.op is Op.SUBMIT:
        res.append(_state(current, 0, "submitting"))
        if not meta.is_submittable:
            res.append(CheckResult("submittable", BLOCK, f"{meta.doctype} isn't submittable."))
        elif meta.has_workflow:
            res.append(CheckResult("workflow", BLOCK,
                                   f"{meta.doctype} has an approval workflow; use "
                                   "workflow_action instead of submit_document."))
        res.append(G.second_person(svc.actor, current, w.doctype, svc.policy))
    elif w.op is Op.WORKFLOW:
        res += _workflow_checks(svc, meta, w)
        res.append(G.second_person(svc.actor, current, w.doctype, svc.policy))
    elif w.op is Op.CANCEL:
        res.append(_state(current, 1, "cancelling"))
    elif w.op is Op.AMEND:
        res.append(_state(current, 2, "amending"))
    res += _texts(w)
    return _summary(meta, w, current), res


def _summary(meta: DocMeta, w: Write, current: dict | None) -> str:
    if w.op is Op.CREATE:
        diff = field_diff(meta, None, w.fields)
        return f"Create a new {w.doctype} draft: " + ("; ".join(diff) or "no fields") + "."
    if w.op is Op.UPDATE:
        diff = field_diff(meta, current, w.fields)
        return f"Update {w.doctype} {w.name}: " + ("; ".join(diff) or "no field changes") + "."
    if w.op is Op.AMEND:
        diff = field_diff(meta, current, w.fields)
        return (f"Amend cancelled {w.doctype} {w.name} into a new draft: "
                + ("; ".join(diff) or "no field changes") + ".")
    if w.op is Op.SUBMIT:
        return f"Submit {w.doctype} {w.name}."
    if w.op is Op.WORKFLOW:
        state = (current or {}).get("workflow_state") or "no state"
        return f"Apply workflow action '{w.action}' to {w.doctype} {w.name} (now {state})."
    return (f"Cancel {w.doctype} {w.name}. Nothing is deleted; it stays on record as "
            "cancelled.")


# The generic field checks, which a pack's result may also carry when it runs them itself.
_FIELD_CHECKS = frozenset({"system fields", "bank details", "known fields", "links",
                           "locked document", "instruction_like_text"})


def _dedupe(results: list[CheckResult]) -> list[CheckResult]:
    """Each result once; a generic field check's pass is dropped where the same check also
    found something (a pack that ran it with stricter options, say)."""
    flagged = {r.check for r in results if r.check in _FIELD_CHECKS and r.level != PASS}
    seen, out = set(), []
    for r in results:
        if r.level == PASS and r.check in flagged:
            continue
        if (r.check, r.level, r.message) not in seen:
            seen.add((r.check, r.level, r.message))
            out.append(r)
    return out


def evaluate_write(svc: Service, w: Write) -> Result:
    if not hasattr(svc.erp, "get_meta"):
        return _label(w), [CheckResult("scope", BLOCK, NEED_ERPNEXT)]
    try:
        meta, _ = meta_of(svc, w.doctype)
    except ErpError as exc:
        return _label(w), [CheckResult("scope", BLOCK,
                                       f"Can't read the metadata of {w.doctype}: {exc}")]
    try:
        scope = scope_of(svc, meta, w)
    except ErpError as exc:
        return _label(w), [CheckResult("scope", BLOCK, f"Can't tell who owns {w.doctype} "
                                       f"{w.name or ''}: {exc}")]
    head = CheckResult("scope", BLOCK if scope in (Scope.NEVER, Scope.LEDGER) else PASS,
                       _write_scope_message(svc, meta, scope), {"scope": scope.value})
    if head.level == BLOCK:
        return _label(w), [head]
    if scope is Scope.PACK:
        summary, res = svc.pack_for(w.doctype).evaluate_write(svc, w)
        if w.op in (Op.CREATE, Op.UPDATE, Op.AMEND):
            # The pack builds the full document, so required fields are its business.
            res = res + field_checks(meta, w.fields, for_create=False, required=False)
        return summary, _dedupe([head, *res, *_texts(w)])
    summary, res = generic_checks(svc, meta, w)
    return summary, [head, *res]


# --- write ---------------------------------------------------------------------------

NUMERIC_TYPES = frozenset({"Int", "Float", "Currency", "Percent"})


def _number(text: str, fieldtype: str):
    try:
        d = Decimal(text)
    except InvalidOperation:
        return text  # not a number: ERPNext says why
    if not d.is_finite():
        return text
    if fieldtype == "Int" and d == d.to_integral_value():
        return int(d)
    f = float(d)
    return f if math.isfinite(f) else text  # "1e400" would be inf: ERPNext says why


def typed_fields(meta: DocMeta, fields: dict) -> dict:
    """The fields as ERPNext takes them: numeric field types (top level and child rows) as
    JSON numbers, converted from the canonical text the confirmation code was bound to.
    ERPNext casts top-level numbers itself, but a controller can do arithmetic on child
    rows before it does."""
    out = {}
    for k, v in fields.items():
        fm, child = meta.fields.get(k), meta.children.get(k)
        if child is not None and isinstance(v, list):
            out[k] = [typed_fields(child, r) if isinstance(r, dict) else r for r in v]
        elif fm is not None and fm.fieldtype in NUMERIC_TYPES and isinstance(v, str):
            out[k] = _number(v, fm.fieldtype)
        else:
            out[k] = v
    return out


def apply_write(svc: Service, w: Write) -> tuple[str, str]:
    meta, _ = meta_of(svc, w.doctype)
    scope = scope_of(svc, meta, w)
    if scope is Scope.PACK:
        return svc.pack_for(w.doctype).apply_write(svc, w)
    if scope not in (Scope.GENERIC, Scope.LATER):  # evaluate blocks these first
        raise ErpError(scope_message(meta, scope))
    erp = svc.erp
    fields = typed_fields(meta, w.fields)
    if w.op is Op.CREATE:
        doc = erp.insert({**fields, "doctype": w.doctype})
        return doc["name"], f"{w.doctype} {doc['name']} created as a draft."
    if w.op is Op.UPDATE:
        current = erp.get_doc(w.doctype, w.name)
        if current is None:
            raise ErpError(f"No {w.doctype} {w.name}")
        erp.save({**current, **fields})
        return w.name, f"{w.doctype} {w.name} updated."
    if w.op is Op.SUBMIT:
        erp.submit(w.doctype, w.name)
        return w.name, f"{w.doctype} {w.name} submitted."
    if w.op is Op.WORKFLOW:
        doc = erp.apply_workflow(w.doctype, w.name, w.action) or {}
        state = doc.get("workflow_state")
        note = f" Comment: {w.reason}" if w.reason else ""
        return w.name, (f"'{w.action}' applied to {w.doctype} {w.name}"
                        + (f"; now {state}." if state else ".") + note)
    if w.op is Op.CANCEL:
        erp.cancel(w.doctype, w.name)
        return w.name, f"{w.doctype} {w.name} cancelled. Reason: {w.reason}"
    doc = erp.amend(w.doctype, w.name, fields)
    return doc["name"], f"{w.doctype} {w.name} amended as new draft {doc['name']}."


# --- the actions ---------------------------------------------------------------------

def _write_of(op: Op):
    def build(canon: dict) -> Write:
        return Write(op, canon["doctype"], canon.get("name"), canon.get("fields", {}),
                     canon.get("action"), canon.get("reason") or canon.get("comment") or "")
    return build


def _target(canon: dict) -> list[tuple[str, str]]:
    return [(canon["doctype"], canon["name"])] if canon.get("name") else []


def _action(name: str, op: Op, spec: dict) -> Action:
    build = _write_of(op)
    # A write on an existing document is bound to its version at preview time.
    return Action(name, canonicaliser(name, spec),
                  lambda svc, canon: evaluate_write(svc, build(canon)),
                  lambda svc, canon: apply_write(svc, build(canon)),
                  "name" if "name" in spec else None, _target if "name" in spec else None)


ACTIONS: list[Action] = [
    _action("create_document", Op.CREATE, {"doctype": _doctype, "fields": _fields}),
    _action("update_document", Op.UPDATE, {"doctype": _doctype, "name": _name,
                                           "fields": _fields}),
    _action("submit_document", Op.SUBMIT, {"doctype": _doctype, "name": _name}),
    _action("workflow_action", Op.WORKFLOW, {
        "doctype": _doctype, "name": _name,
        "action": lambda a: _text(a, "action", required=True, max_len=140),
        "comment": lambda a: _text(a, "comment")}),
    _action("cancel_document", Op.CANCEL, {
        "doctype": _doctype, "name": _name,
        "reason": lambda a: _text(a, "reason", required=True)}),
    _action("amend_document", Op.AMEND, {"doctype": _doctype, "name": _name,
                                         "fields": lambda a: _fields(a, required=False)}),
]
