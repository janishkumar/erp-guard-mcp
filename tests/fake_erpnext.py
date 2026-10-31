"""A small in-process ERPNext behind httpx.MockTransport, for the generic layer's tests.

Metadata comes from the fixtures recorded from v16.52.0; documents live in a dict. It
answers only the requests the connector makes, and records every one.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import httpx

from erp_guard.connectors.erpnext import AUDIT_DOCTYPE, ErpNext, FrappeClient

FIX = Path(__file__).parent / "fixtures" / "erpnext"


def _not_found(what: str) -> httpx.Response:
    return httpx.Response(404, json={"exc_type": "DoesNotExistError", "exception": what})


class FakeErpNext:
    def __init__(self, user: str = "requester@example.com"):
        self.user = user
        self.calls: list[httpx.Request] = []
        self.docs: dict[tuple[str, str], dict] = {}
        self.roles: dict[str, list[str]] = {}
        self.workflows: set[str] = set()        # doctypes with an active workflow
        self.transitions: dict[tuple[str, str], list[str]] = {}
        self.meta_overrides: dict[str, dict] = {}
        self._n = 0

    # helpers for tests -----------------------------------------------------------------
    def add(self, doc: dict) -> dict:
        self.docs[(doc["doctype"], doc["name"])] = doc
        return doc

    def of(self, doctype: str) -> list[dict]:
        return [d for (dt, _), d in self.docs.items() if dt == doctype]

    def audit(self) -> list[dict]:
        return self.of(AUDIT_DOCTYPE)

    def method_calls(self, method: str) -> list[dict]:
        return [json.loads(r.content) for r in self.calls
                if r.url.path == f"/api/method/{method}"]

    def _name(self, doctype: str) -> str:
        self._n += 1
        return f"{doctype.upper().replace(' ', '-')}-{self._n}"

    def _store(self, doc: dict) -> dict:
        doc = {**doc, "name": doc.get("name") or self._name(doc["doctype"]),
               "owner": doc.get("owner") or self.user, "docstatus": doc.get("docstatus", 0),
               "modified": "2026-10-29 10:00:00"}
        return self.add(doc)

    # transport -------------------------------------------------------------------------
    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = unquote(req.url.path)
        body = json.loads(req.content) if req.content else {}
        if path.startswith("/api/method/"):
            return self._method(path.removeprefix("/api/method/"), req, body)
        parts = path.removeprefix("/api/resource/").split("/", 1)
        return self._resource(req.method, parts[0], parts[1] if len(parts) > 1 else None,
                              req, body)

    def _meta(self, doctype: str) -> httpx.Response:
        f = FIX / "meta" / f"{doctype}.json"
        if not f.exists():
            return _not_found(doctype)
        raw = json.loads(f.read_text())
        raw["docs"][0].update(self.meta_overrides.get(doctype, {}))
        raw["docs"][0]["__workflow_docs"] = (
            [{"doctype": "Workflow", "name": f"{doctype} Approval"}]
            if doctype in self.workflows else [])
        return httpx.Response(200, json=raw)

    def _get_list(self, body: dict) -> list[dict]:
        dt = body["doctype"]
        if dt == "Has Role":
            user = body["filters"][0][2]
            return [{"role": r} for r in self.roles.get(user, [])]
        if dt == "Purchase Order" and any("`tabPurchase Order Item`" in f
                                          for f in body.get("fields", [])):
            names = body["filters"][0][2]
            return [{"name": d["name"], "idx": i + 1, **{k: it[k] for k in
                                                         ("item_code", "qty", "rate")}}
                    for d in self.of(dt) if d["name"] in names
                    for i, it in enumerate(d.get("items", []))]
        rows = self.of(dt)
        for field, op, value in body.get("filters") or []:
            if op == "=":
                rows = [r for r in rows if r.get(field) == value]
        fields = body.get("fields") or ["name"]
        out = [{k: r.get(k) for k in fields} for r in rows]
        return out[: body["limit_page_length"]] if body.get("limit_page_length") else out

    def _method(self, method: str, req: httpx.Request, body: dict) -> httpx.Response:
        def ok(message):
            return httpx.Response(200, json={"message": message})
        if method == "frappe.auth.get_logged_user":
            return ok(self.user)
        if method == "frappe.desk.form.load.getdoctype":
            return self._meta(req.url.params["doctype"])
        if method == "frappe.client.get":
            d = self.docs.get((req.url.params["doctype"], req.url.params["name"]))
            return ok(d) if d else _not_found(req.url.params["name"])
        if method == "frappe.client.get_list":
            return ok(self._get_list(body))
        if method == "frappe.client.insert":
            return ok(self._store(body["doc"]))
        if method == "frappe.client.save":
            return ok(self.add({**body["doc"]}))
        if method == "frappe.client.submit":
            return ok(self.add({**body["doc"], "docstatus": 1}))
        if method == "frappe.client.cancel":
            d = self.docs[(body["doctype"], body["name"])]
            d["docstatus"] = 2
            return ok(None)
        if method == "frappe.model.workflow.get_transitions":
            doc = body["doc"]
            if doc["doctype"] not in self.workflows:
                return httpx.Response(417, json={"exc_type": "DoesNotExistError",
                                                 "exception": "Workflow None not found"})
            return ok([{"action": a, "next_state": a + "d"}
                       for a in self.transitions.get((doc["doctype"], doc["name"]), [])])
        if method == "frappe.model.workflow.apply_workflow":
            d = self.docs[(body["doc"]["doctype"], body["doc"]["name"])]
            d["workflow_state"] = body["action"] + "d"
            return ok(d)
        if method.endswith(".make_purchase_receipt"):
            src = self.docs[("Purchase Order", body["source_name"])]
            return ok({"doctype": "Purchase Receipt", "docstatus": 0, "__islocal": 1,
                       "owner": self.user, "supplier": src["supplier"],
                       "items": [{"doctype": "Purchase Receipt Item", "idx": 1,
                                  "parenttype": "Purchase Receipt", "item_code": it["item_code"],
                                  "qty": it["qty"]} for it in src.get("items", [])]})
        return httpx.Response(400, json={"exception": f"fake has no {method}"})

    def _resource(self, verb: str, doctype: str, name: str | None, req: httpx.Request,
                  body: dict) -> httpx.Response:
        if verb == "GET" and name:
            d = self.docs.get((doctype, name))
            return httpx.Response(200, json={"data": d}) if d else _not_found(name)
        if verb == "GET":
            fields = json.loads(req.url.params.get("fields", '["name"]'))
            rows = self.of(doctype)
            if doctype == "Item":
                rows = [{"name": r["name"], "last_purchase_rate": r.get("last_purchase_rate"),
                         "disabled": 0} for r in rows]
            return httpx.Response(200, json={"data": [{k: r.get(k) for k in fields}
                                                      for r in rows]})
        if verb == "POST":
            doc = {"doctype": doctype, **body}
            if doctype == "Supplier":
                doc["name"] = body["supplier_name"]
            if doctype == "Purchase Order":
                doc["items"] = [{**it, "doctype": "Purchase Order Item"} for it in body["items"]]
            return httpx.Response(200, json={"data": self._store(doc)})
        d = self.docs[(doctype, name)]
        d.update(body)
        return httpx.Response(200, json={"data": d})


def connector(fake: FakeErpNext, **client_kw) -> ErpNext:
    """An ErpNext over the fake, with the scope-derived write predicate production uses."""
    client = FrappeClient("http://erp.local", "k", "s", transport=httpx.MockTransport(fake),
                          **client_kw)
    erp = ErpNext(client, "Lakeside Demo Utilities")
    if "writable" not in client_kw:
        erp.use_scope()
    return erp
