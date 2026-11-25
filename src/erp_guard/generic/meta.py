"""Field metadata for one document type, built from ERPNext's getdoctype response."""

from __future__ import annotations

from dataclasses import dataclass, field

# Set by the server, never by a caller (docs/design.md, "Generic writes").
SYSTEM_FIELDS = frozenset({
    "name", "owner", "creation", "modified", "modified_by", "docstatus", "amended_from",
    "idx", "parent", "parentfield", "parenttype", "workflow_state",
})

LAYOUT_TYPES = frozenset({"Section Break", "Column Break", "Tab Break", "HTML", "Button",
                          "Heading", "Fold"})
TABLE_TYPES = frozenset({"Table", "Table MultiSelect"})


@dataclass(frozen=True)
class FieldMeta:
    fieldname: str
    fieldtype: str
    reqd: bool
    options: str
    allow_on_submit: bool
    read_only: bool
    # The server supplies this value when it is left out (a default, fetch_from or a
    # naming series), so a missing value is not the caller's mistake.
    server_filled: bool = False


@dataclass(frozen=True)
class DocMeta:
    doctype: str
    module: str
    issingle: bool
    is_submittable: bool
    has_workflow: bool
    fields: dict[str, FieldMeta]
    children: dict[str, DocMeta] = field(default_factory=dict)  # table fieldname -> child meta
    istable: bool = False  # a child table: its rows belong to a parent document


def _field(f: dict) -> FieldMeta:
    name = f["fieldname"]
    return FieldMeta(
        fieldname=name,
        fieldtype=f["fieldtype"],
        reqd=bool(f.get("reqd")),
        options=f.get("options") or "",
        allow_on_submit=bool(f.get("allow_on_submit")),
        read_only=bool(f.get("read_only")),
        server_filled=bool(f.get("default") or f.get("fetch_from") or name == "naming_series"),
    )


def _build(doc: dict, by_name: dict[str, dict], has_workflow: bool, seen: frozenset) -> DocMeta:
    fields = {f["fieldname"]: _field(f) for f in doc["fields"]
              if f.get("fieldname") and f["fieldtype"] not in LAYOUT_TYPES}
    children = {}
    for name, fm in fields.items():
        if fm.fieldtype in TABLE_TYPES and fm.options in by_name and fm.options not in seen:
            children[name] = _build(by_name[fm.options], by_name, False, seen | {fm.options})
    return DocMeta(
        doctype=doc["name"], module=doc.get("module") or "",
        issingle=bool(doc.get("issingle")), is_submittable=bool(doc.get("is_submittable")),
        has_workflow=has_workflow, fields=fields, children=children,
        istable=bool(doc.get("istable")),
    )


def doc_meta(raw: dict, has_workflow: bool = False) -> DocMeta:
    """Build from a getdoctype response: docs[0] is the type, the rest are its child types.

    getdoctype does not say whether a Workflow is attached to the type, so the caller
    supplies `has_workflow` from a separate lookup (default False).
    """
    docs = raw["docs"]
    by_name = {d["name"]: d for d in docs[1:]}
    return _build(docs[0], by_name, has_workflow, frozenset({docs[0]["name"]}))
