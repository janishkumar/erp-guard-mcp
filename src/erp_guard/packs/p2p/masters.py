"""Master data the purchase-to-pay checks read: Supplier, Item, Item Price, Price List,
Warehouse, and Addresses and Contacts linked to a supplier (docs/design.md, "One path per
document type"). Reached through create_document, update_document and, for the change
requests, workflow_action.

Creating one runs the generic checks plus a duplicate warning (a similar name; for an
Address the same street, city and postcode as another of the supplier's addresses; for a
Contact the same email). A Supplier is created only by onboard_supplier, which runs the
duplicate-supplier checks. A new Address or Contact for a supplier is a change to that
supplier (ERPNext makes a new primary address the supplier's default and turns the old one
off), so it goes the way of the changes below: a create-type change request with no target
name, which the approval inserts.

Changing one applies directly unless it changes a field the checks read (CHECK_FIELDS; for
Address, Contact, Warehouse and Price List, any field). Such a change is blocked while open
documents reference the record, and otherwise becomes an ERP Guard Change Request, put to
"Pending Approval" as it's created. The whole update goes into the request, so none of it
applies until someone else decides: workflow_action("ERP Guard Change Request", <id>,
"Approve") by a purchase approver (a purchase-order approval limit in the policy) who
neither asked for it nor made it, on a request nobody else has changed since, saves it on
the record after the field and open-document checks run again. "Reject" needs a comment
and changes nothing.

Open documents: for an Item (and an Item Price's item), live purchase orders and draft or
pending receipts and invoices with the item; for a Supplier, and an Address or Contact by
the suppliers it is linked to, live purchase orders, unpaid invoices and draft or pending
payments of the supplier. Warehouses and price lists have none. Document kinds the user
can't read in ERPNext (a requester can't read payments) are only a warning when a change is
requested, because the approver's check reads them before anything applies; when the
approver can't read them either, approving is blocked.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rapidfuzz import fuzz, utils

from erp_guard import checks as C
from erp_guard.connectors.base import ErpError
from erp_guard.generic import checks as G
from erp_guard.generic.actions import (
    field_checks,
    field_diff,
    generic_checks,
    meta_of,
    record_exists,
    same_value,
)
from erp_guard.models import LIVE_STATUSES, PurchaseOrder
from erp_guard.ops import Op, Write
from erp_guard.packs.p2p import APPROVE, REJECT, REVISE, SUBMIT_FOR_APPROVAL
from erp_guard.packs.p2p.models import OPEN, POSTED, Invoice

if TYPE_CHECKING:
    from erp_guard.service import Service

Result = tuple[str, list[C.CheckResult]]
CHANGE_REQUEST = "ERP Guard Change Request"
ANY = "*"
# Fields the purchase-to-pay checks read, per master: a change to one needs a second person.
# Supplier also lists its primary address and contact: pointing a supplier at another
# address is the same change as editing its address.
CHECK_FIELDS: dict[str, frozenset[str]] = {
    "Item": frozenset({"is_stock_item", "stock_uom", "item_group", "disabled", "taxes",
                       "uoms"}),
    "Item Price": frozenset({"price_list_rate", "item_code", "price_list", "uom",
                             "valid_from", "valid_upto", "supplier"}),
    "Supplier": frozenset({"supplier_name", "tax_id", "disabled", "on_hold",
                           "supplier_primary_address", "supplier_primary_contact",
                           "accounts", "is_internal_supplier", "represents_company",
                           "payment_terms"}),
    "Address": frozenset({ANY}),
    "Contact": frozenset({ANY}),
    "Warehouse": frozenset({ANY}),
    "Price List": frozenset({ANY}),
}
MASTERS = frozenset(CHECK_FIELDS)
# Never changed through the MCP: onboard_supplier records who onboarded a supplier, and the
# self-dealing and new-supplier checks read it.
FIXED_FIELDS: dict[str, frozenset[str]] = {"Supplier": frozenset({"erp_guard_onboarded_by"})}
# Owned only when linked to a supplier; otherwise the generic checks apply.
SUPPLIER_LINKED = frozenset({"Address", "Contact"})
# The field holding a record's name, for the duplicate-name warning (the first one given;
# an Item without an item_name is named by its code).
_NAME_FIELDS = {"Item": ("item_name", "item_code"), "Warehouse": ("warehouse_name",),
                "Price List": ("price_list_name",)}
_NEEDS = ("get_doc", "find", "list_receipts", "list_invoices", "list_payments", "outstanding",
          "create_change_request", "apply_change_request")


def _block(check: str, message: str, **evidence) -> C.CheckResult:
    return C.CheckResult(check, C.BLOCK, message, evidence)


def _pass(check: str, message: str) -> C.CheckResult:
    return C.CheckResult(check, C.PASS, message)


def _unsupported(svc: Service) -> list[C.CheckResult]:
    missing = [m for m in _NEEDS if not hasattr(svc.erp, m)]
    if not missing:
        return []
    return [_block("connector", f"{svc.erp.name} doesn't support master-data changes yet.")]


# --- ownership -------------------------------------------------------------------------

def supplier_links(doc: dict | None) -> set[str]:
    """The suppliers an Address or Contact (or the fields written to one) links to."""
    rows = (doc or {}).get("links")
    if not isinstance(rows, list):
        return set()
    return {r["link_name"] for r in rows if isinstance(r, dict)
            and isinstance(r.get("link_doctype"), str)
            and r["link_doctype"].strip().casefold() == "supplier"
            and isinstance(r.get("link_name"), str) and r["link_name"]}


def owns_write(svc: Service, w: Write) -> bool:
    """An Address or Contact is the pack's when it is, or would become, linked to a
    supplier; one that can't be found is too, and its checks say so."""
    if w.doctype not in SUPPLIER_LINKED or supplier_links(w.fields):
        return True
    if w.op is Op.CREATE or not w.name:
        return False
    current = svc.erp.get_doc(w.doctype, w.name)
    return current is None or bool(supplier_links(current))


# --- what a change touches -------------------------------------------------------------

def changed_check_fields(doctype: str, current: dict, fields: dict) -> list[str]:
    """The fields this update changes that the purchase-to-pay checks read."""
    watched = CHECK_FIELDS.get(doctype, frozenset())
    changed = [k for k, v in fields.items() if not same_value(current.get(k), v)]
    return sorted(changed if ANY in watched else [k for k in changed if k in watched])


def _live(o: PurchaseOrder) -> bool:
    return o.status in LIVE_STATUSES and not o.closed


def _unpaid(svc: Service, inv: Invoice) -> bool:
    if inv.is_return:
        return False
    if inv.state in OPEN:
        return True
    return inv.state in POSTED and svc.erp.outstanding(inv.id) > 0


class _Reads:
    """The connector's list reads for the open-document check, remembering which kinds of
    document this user isn't allowed to read (a requester can't read payments, say)."""

    def __init__(self) -> None:
        self.unreadable: set[str] = set()

    def __call__(self, kind: str, read):
        try:
            return read()
        except ErpError:
            self.unreadable.add(kind)
            return []


def _item_refs(svc: Service, reads: _Reads, code: str) -> list[str]:
    def has(doc) -> bool:
        return any(ln.item_code == code for ln in doc.lines)
    erp = svc.erp
    return ([f"purchase order {o.id}" for o in reads("purchase orders", erp.list_orders)
             if _live(o) and has(o)]
            + [f"receipt {r.id}" for r in reads("receipts", erp.list_receipts)
               if r.state in OPEN and has(r)]
            + [f"invoice {i.id}" for i in reads("invoices", erp.list_invoices)
               if i.state in OPEN and has(i)])


def _supplier_refs(svc: Service, reads: _Reads, sid: str) -> list[str]:
    erp = svc.erp
    orders = reads("purchase orders", lambda: erp.list_orders(supplier_id=sid))
    invoices = reads("invoices", lambda: [i for i in erp.list_invoices(supplier_id=sid)
                                          if i.supplier_id == sid and _unpaid(svc, i)])
    payments = reads("payments", lambda: erp.list_payments(supplier_id=sid))
    return ([f"purchase order {o.id}" for o in orders if o.supplier_id == sid and _live(o)]
            + [f"unpaid invoice {i.id}" for i in invoices]
            + [f"payment {p.id}" for p in payments if p.supplier_id == sid and p.state in OPEN])


def _referenced(doctype: str, name: str, current: dict, fields: dict
                ) -> tuple[list[str], list[str]]:
    """(item codes, supplier IDs) whose open documents a change to this record affects."""
    if doctype == "Item":
        return [name], []
    if doctype == "Item Price":
        codes = {current.get("item_code"), fields.get("item_code")} - {None, ""}
        return sorted(c for c in codes if isinstance(c, str)), []
    if doctype == "Supplier":
        return [], [name]
    if doctype in SUPPLIER_LINKED:
        return [], sorted(supplier_links(current) | supplier_links(fields))
    return [], []


def open_documents(svc: Service, doctype: str, name: str, current: dict,
                   fields: dict) -> tuple[list[str], set[str]]:
    """(open documents referencing the record or the suppliers or items behind it, the
    kinds of document this user can't read and so weren't looked at)."""
    items, suppliers = _referenced(doctype, name, current, fields)
    reads, out = _Reads(), []
    for code in items:
        out += [f"{ref} (item {code})" for ref in _item_refs(svc, reads, code)]
    for sid in suppliers:
        out += [f"{ref} ({sid})" for ref in _supplier_refs(svc, reads, sid)]
    return out, reads.unreadable


def _open_check(svc: Service, doctype: str, name: str, current: dict, fields: dict,
                applying: bool) -> C.CheckResult:
    """Blocks on open documents. Kinds the user can't read: a warning when the change is
    only being requested (nothing applies until the approver's check, which reads them),
    a block when it is being applied."""
    refs, unreadable = open_documents(svc, doctype, name, current, fields)
    if refs:
        shown = ", ".join(refs[:10]) + (f" and {len(refs) - 10} more" if len(refs) > 10
                                        else "")
        return _block("open documents",
                      f"{doctype} {name} can't change while open documents reference it: "
                      f"{shown}. Finish, close or cancel them first.", documents=refs)
    if unreadable:
        kinds = ", ".join(sorted(unreadable))
        if applying:
            return _block("open documents",
                          f"{svc.actor} can't read {kinds}, so can't confirm none is open "
                          f"for {doctype} {name}; someone who can has to approve this.",
                          unreadable=sorted(unreadable))
        return C.CheckResult("open documents", C.WARN,
                             f"Nothing open that {svc.actor} can see references {doctype} "
                             f"{name}, but {kinds} aren't readable as {svc.actor}; the "
                             "approver's check reads them before anything changes.",
                             {"unreadable": sorted(unreadable)})
    return _pass("open documents", f"No open order, receipt, invoice or payment references "
                 f"{doctype} {name}.")


def _fixed_fields(doctype: str, fields: dict) -> list[C.CheckResult]:
    fixed = sorted(FIXED_FIELDS.get(doctype, frozenset()) & set(fields))
    if not fixed:
        return []
    return [_block("fixed fields", f"{', '.join(fixed)} on a {doctype} is set when it is "
                   "onboarded and never changed.", fields=fixed)]


# --- create ----------------------------------------------------------------------------

def _similar(a: str, b: str) -> float:
    return fuzz.token_set_ratio(a, b, processor=utils.default_process)


def _norm_text(v) -> str:
    return " ".join(str(v or "").lower().split())


def _duplicate_address(svc: Service, fields: dict) -> list[C.CheckResult]:
    """The same street, city and postcode as another address of a linked supplier."""
    key = tuple(_norm_text(fields.get(k)) for k in ("address_line1", "city", "pincode"))
    if not key[0]:
        return []
    same = set()
    for sid in sorted(supplier_links(fields)):
        rows = svc.erp.find("Address", [["Dynamic Link", "link_doctype", "=", "Supplier"],
                                        ["Dynamic Link", "link_name", "=", sid]],
                            ["name", "address_line1", "city", "pincode"], None, 0) or []
        same |= {r["name"] for r in rows if tuple(_norm_text(r.get(k)) for k in (
            "address_line1", "city", "pincode")) == key}
    if same:
        return [C.CheckResult("duplicate address", C.WARN,
                              f"The supplier already has this address: {', '.join(sorted(same))}. "
                              "Use that one if it's the same place.",
                              {"existing": sorted(same)})]
    return [_pass("duplicate address", "No other address of the supplier is the same.")]


def _duplicate_contact(svc: Service, fields: dict) -> list[C.CheckResult]:
    """A contact with the same email address (case-insensitive)."""
    email = _norm_text(fields.get("email_id"))
    if not email:
        return []
    rows = svc.erp.find("Contact", [], ["name", "email_id"], None, 0) or []
    same = sorted(r["name"] for r in rows if _norm_text(r.get("email_id")) == email)
    if same:
        return [C.CheckResult("duplicate contact", C.WARN,
                              f"{fields['email_id']} is already the email of {', '.join(same)}. "
                              "Use that contact if it's the same person.", {"existing": same})]
    return [_pass("duplicate contact", f"No contact has the email {fields['email_id']}.")]


def _duplicate_name(svc: Service, w: Write) -> list[C.CheckResult]:
    """Warn when the new record's name is close to an existing one's (token-set ratio at
    the policy's duplicate-supplier warning threshold); an Item Price when the item already
    has a price on that price list; an Address or Contact as above."""
    if w.doctype == "Address":
        return _duplicate_address(svc, w.fields)
    if w.doctype == "Contact":
        return _duplicate_contact(svc, w.fields)
    if w.doctype == "Item Price":
        item, plist = w.fields.get("item_code"), w.fields.get("price_list")
        if not (isinstance(item, str) and isinstance(plist, str)):
            return []
        have = svc.erp.find("Item Price", [["item_code", "=", item],
                                           ["price_list", "=", plist]], ["name"], None, 0)
        if have:
            ids = [r["name"] for r in have]
            return [C.CheckResult("duplicate name", C.WARN,
                                  f"{item} already has a price on {plist}: {', '.join(ids)}. "
                                  "Change that one instead of adding another?",
                                  {"existing": ids})]
        return [_pass("duplicate name", f"{item} has no price on {plist} yet.")]
    keys = _NAME_FIELDS.get(w.doctype)
    name = next((w.fields[k].strip() for k in keys or () if isinstance(w.fields.get(k), str)
                 and w.fields[k].strip()), None)
    if name is None:
        return []
    rows = svc.erp.find(w.doctype, [], ["name", keys[0]], None, 0) or []
    scored = sorted(((max(_similar(name, r["name"]), _similar(name, r.get(keys[0]) or "")),
                      r["name"]) for r in rows), reverse=True)
    close = [(s, n) for s, n in scored if s >= svc.policy.dup_warn_ratio][:3]
    if close:
        listed = ", ".join(f"{n} ({s:.0f})" for s, n in close)
        return [C.CheckResult("duplicate name", C.WARN,
                              f"'{name}' is close to existing {w.doctype} record(s): {listed}. "
                              "Use the existing one if it's the same thing.",
                              {"similar": [n for _, n in close]})]
    return [_pass("duplicate name", f"No existing {w.doctype} is named like '{name}'.")]


def evaluate_create(svc: Service, w: Write) -> Result:
    meta, _ = meta_of(svc, w.doctype)
    summary, res = generic_checks(svc, meta, w)
    res += _fixed_fields(w.doctype, w.fields) + _duplicate_name(svc, w)
    if w.doctype not in SUPPLIER_LINKED:
        return summary, res
    if (bad := _unsupported(svc)):
        return summary, res + bad
    suppliers = ", ".join(sorted(supplier_links(w.fields)))
    res.append(_open_check(svc, w.doctype, "(new)", {}, w.fields, applying=False))
    res.append(_pass("second person",
                     f"A new {w.doctype} for {suppliers} changes where and to whom the "
                     "supplier's business goes, so it waits for a second person: it is "
                     "recorded as a change request and created only when someone else with a "
                     "purchase approval limit approves it."))
    return (summary + " Recorded as a change request for a second person; nothing is created "
            "until it is approved."), res


def apply_create(svc: Service, w: Write) -> tuple[str, str]:
    if w.doctype in SUPPLIER_LINKED:
        cr = svc.erp.create_change_request(w.doctype, "", w.fields)
        return cr, (f"Change request {cr} for a new {w.doctype} of "
                    f"{', '.join(sorted(supplier_links(w.fields)))} is waiting for a second "
                    "person; nothing has been created yet. Someone else with a purchase "
                    f"approval limit creates it with workflow_action(\"{CHANGE_REQUEST}\", "
                    f"\"{cr}\", \"Approve\").")
    doc = svc.erp.insert({**w.fields, "doctype": w.doctype})
    return doc["name"], f"{w.doctype} {doc['name']} created."


# --- update ----------------------------------------------------------------------------

def evaluate_update(svc: Service, w: Write) -> Result:
    meta, _ = meta_of(svc, w.doctype)
    summary, res = generic_checks(svc, meta, w)
    current = svc.erp.get_doc(w.doctype, w.name)
    if current is None:  # the generic checks already say so
        return summary, res
    res += _fixed_fields(w.doctype, w.fields)
    changed = changed_check_fields(w.doctype, current, w.fields)
    if not changed:
        return summary, res + [_pass("master data", "No field the purchase-to-pay checks "
                                     "read changes, so this applies directly.")]
    if (bad := _unsupported(svc)):
        return summary, res + bad
    res.append(_open_check(svc, w.doctype, w.name, current, w.fields,
                       applying=False))
    res.append(_pass("second person",
                     f"The purchase-to-pay checks read {', '.join(changed)} on a {w.doctype}, "
                     "so this change waits for a second person: it is recorded as a change "
                     "request and applied only when someone else with a purchase approval "
                     "limit approves it."))
    return (summary + " Recorded as a change request for a second person; nothing changes "
            "until it is approved."), res


def apply_update(svc: Service, w: Write) -> tuple[str, str]:
    current = svc.erp.get_doc(w.doctype, w.name)
    if current is None:
        raise ErpError(f"No {w.doctype} {w.name}")
    if changed_check_fields(w.doctype, current, w.fields):
        cr = svc.erp.create_change_request(w.doctype, w.name, w.fields)
        return cr, (f"Change request {cr} for {w.doctype} {w.name} "
                    f"({', '.join(sorted(w.fields))}) is waiting for a second person; nothing "
                    f"has changed yet. Someone else with a purchase approval limit applies it "
                    f"with "
                    f"workflow_action(\"{CHANGE_REQUEST}\", \"{cr}\", \"Approve\").")
    svc.erp.save({**current, **w.fields})
    return w.name, f"{w.doctype} {w.name} updated."


# --- deciding a change request (workflow_action) ---------------------------------------

def _is_approver(svc: Service, user: str) -> bool:
    """Change requests are decided by purchase approvers (a purchase-order approval limit)."""
    return svc.policy.approver_limit(user, "purchase_order") is not None


def _requested(cr: dict) -> tuple[str, str, dict] | str:
    """(target type, target name, fields) of a change request, or what's wrong with it. An
    empty target name asks for a new supplier Address or Contact."""
    target_dt, target = cr.get("target_doctype"), cr.get("target_name") or ""
    if target_dt not in MASTERS:
        return (f"it targets {target_dt}, which isn't master data the purchase-to-pay pack "
                "changes through change requests")
    try:
        fields = json.loads(cr.get("fields_json") or "")
    except ValueError:
        return "its requested values aren't readable"
    if not isinstance(fields, dict) or not fields or not isinstance(target, str):
        return "it names no values"
    if not target and (target_dt not in SUPPLIER_LINKED or not supplier_links(fields)):
        return (f"it names no {target_dt}; only a new Address or Contact linked to a supplier "
                "is created through a change request")
    return target_dt, target, fields


def _decider(actor: str, cr: dict, ref: str) -> C.CheckResult:
    by, maker = cr.get("requested_by") or "", cr.get("owner") or ""
    if actor in (by, maker):
        return _block("second person", f"{actor} asked for this change and can't also "
                      "decide it. Someone else with a purchase approval limit has to.",
                      requested_by=by, owner=maker)
    changed_by = cr.get("modified_by") or ""
    if changed_by and changed_by != by:
        return _block("second person", f"Change request {ref} was changed by {changed_by} "
                      f"after {by} submitted it, so it isn't what {by} asked for. Reject it "
                      "and ask again.", modified_by=changed_by)
    return _pass("second person", f"{actor} isn't {by}, who asked.")


def evaluate_decision(svc: Service, w: Write) -> Result:
    actor, ref, action = svc.actor, w.name, w.action or ""
    summary = f"Apply '{action}' to change request {ref}."
    if action in (SUBMIT_FOR_APPROVAL, REVISE):
        return summary, [_block("pack", f"'{action}' isn't applied to a change request: it is "
                                "submitted for approval when update_document makes it, and a "
                                "rejected one is closed (ask again with update_document).")]
    if action not in (APPROVE, REJECT):
        return summary, [_block("pack", f"'{action}' is not a change request action. Use "
                                f"{APPROVE} or {REJECT}.")]
    if (bad := _unsupported(svc)):
        return summary, bad
    cr = svc.erp.get_doc(CHANGE_REQUEST, ref)
    if cr is None:
        return summary, [_block("change request", f"No change request '{ref}'.")]
    requested = _requested(cr)
    if isinstance(requested, str):
        return summary, [_block("change request", f"Change request {ref} can't be decided "
                                f"here: {requested}.")]
    target_dt, target, fields = requested
    by = cr.get("requested_by") or cr.get("owner") or ""
    creating = not target
    res = []
    state = cr.get("workflow_state") or "Draft"
    if (cr.get("docstatus"), state) != (0, "Pending Approval"):
        res.append(_block("change request", f"Change request {ref} is "
                          f"{'cancelled' if cr.get('docstatus') == 2 else state}; only a "
                          "pending one is decided."))
    else:
        res.append(_pass("change request", f"Change request {ref} is pending approval."))
    res.append(_decider(actor, cr, ref))
    if not _is_approver(svc, actor):
        res.append(_block("approver", f"Only purchase approvers (a purchase-order approval "
                          f"limit in the policy) decide change requests; {actor} isn't one."))
    meta, _ = meta_of(svc, target_dt)
    current = None if creating else svc.erp.get_doc(target_dt, target)
    diff = "; ".join(field_diff(meta, current, fields)) or "no field changes"
    what = f"a new {target_dt}" if creating else f"{target_dt} {target}"
    summary = (f"{action} change request {ref} from {by}: {what}: {diff}."
               + ((" It is created." if creating else " It is saved on the record.")
                  if action == APPROVE else " Nothing changes."))
    if action == REJECT:
        if not w.reason:
            res.append(_block("reason", "Rejecting a change request needs a comment saying "
                              "why."))
        return summary, res
    if current is None and not creating:
        return summary, res + [_block("change request", f"No {target_dt} '{target}' any "
                                      "more.")]
    res += field_checks(meta, fields, for_create=creating, required=creating)
    res += G.links_exist(meta, fields, record_exists(svc))
    res += _fixed_fields(target_dt, fields)
    res.append(_open_check(svc, target_dt, target or "(new)", current or {}, fields,
                           applying=True))
    return summary, res


def apply_decision(svc: Service, w: Write) -> tuple[str, str]:
    approve = w.action == APPROVE
    svc.erp.apply_change_request(w.name, approve, w.reason)
    if approve:
        cr = svc.erp.get_doc(CHANGE_REQUEST, w.name) or {}
        return w.name, (f"Change request {w.name} approved and applied to "
                        f"{cr.get('target_doctype')} {cr.get('target_name')}.")
    return w.name, f"Change request {w.name} rejected; nothing changed. Reason: {w.reason}"
