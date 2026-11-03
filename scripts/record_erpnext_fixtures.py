"""Record ERPNext metadata fixtures for the offline connector tests.

Runs as the local Administrator (password `admin`, set by docker/compose.yml; local
development only) against the ERPNext that `make up && make seed` starts. Writes:

- tests/fixtures/erpnext/doctypes.json: every non-child DocType with its module and
  whether it is single or submittable.
- tests/fixtures/erpnext/meta/<DocType>.json: the `frappe.desk.form.load.getdoctype`
  response (the DocType and its child tables) for the types the tests use. The bundled
  client assets (`__js`, `__css`, print formats and the like) and per-user list
  settings are dropped; the field metadata is kept as ERPNext returns it.

Reads only. Re-run after an ERPNext upgrade and commit the result.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

URL = os.environ.get("ERPNEXT_URL", "http://localhost:8080")
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "erpnext"
META_TYPES = ["Task", "Project", "Customer", "Item", "Purchase Invoice", "Purchase Receipt",
              "Payment Entry", "Supplier", "Purchase Order", "Journal Entry", "Server Script",
              "Material Request", "Request for Quotation", "Supplier Quotation"]
DOCTYPE_FIELDS = ["name", "module", "issingle", "istable", "is_submittable"]


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def main() -> None:
    h = httpx.Client(base_url=URL, timeout=120)
    h.post("/api/method/login", json={"usr": "Administrator", "pwd": "admin"}).raise_for_status()

    r = h.post("/api/method/frappe.client.get_list", json={
        "doctype": "DocType", "fields": DOCTYPE_FIELDS, "filters": [["istable", "=", 0]],
        "order_by": "name asc", "limit_page_length": 0})
    r.raise_for_status()
    doctypes = r.json()["message"]
    _write(OUT / "doctypes.json", {"message": doctypes})

    for doctype in META_TYPES:
        r = h.get("/api/method/frappe.desk.form.load.getdoctype", params={"doctype": doctype})
        r.raise_for_status()
        docs = [{k: v for k, v in d.items() if not k.startswith("__")}
                for d in r.json()["docs"]]
        _write(OUT / "meta" / f"{doctype}.json", {"docs": docs})

    print(f"{len(doctypes)} doctypes, {len(META_TYPES)} metas -> {OUT}")


if __name__ == "__main__":
    main()
