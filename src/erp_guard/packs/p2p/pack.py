"""The purchase-to-pay pack as the generic tools see it: which document types it owns, and
which (document type, operation) pairs it supports.

So far only creating a Purchase Order is supported, by translating the generic fields
onto `create_purchase_order` and running its checks. Every other operation on a P2P-owned
type is refused until its task tools land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from erp_guard.checks import BLOCK, CheckResult
from erp_guard.connectors.base import ErpError
from erp_guard.ops import Op, Write
from erp_guard.packs.p2p import P2P_OWNED

if TYPE_CHECKING:
    from erp_guard.service import Service

# Generic Purchase Order field -> create_purchase_order argument.
_PO_HEADER = {"supplier": "supplier", "erp_guard_cost_centre": "cost_centre",
              "erp_guard_notes": "notes", "items": "lines"}
_PO_ROW = ("item_code", "qty", "rate", "description")


def _refuse(message: str) -> CheckResult:
    return CheckResult("pack", BLOCK, message, {"pack": "purchase-to-pay"})


class P2PPack:
    name = "purchase-to-pay"
    # The pack's task tools, by the document type they work on; describe_doctype points the
    # model at these first.
    task_tools: dict[str, tuple[str, ...]] = {
        "Purchase Order": ("create_purchase_order", "submit_for_approval",
                           "approve_purchase_order", "reject_purchase_order",
                           "cancel_purchase_order", "amend_purchase_order"),
        "Supplier": ("onboard_supplier",),
    }

    def owns(self, doctype: str) -> bool:
        return doctype in P2P_OWNED

    def _route(self, svc: Service, w: Write) -> tuple[str, dict] | CheckResult:
        """The pack action and its canonical arguments for this write, or why not."""
        if (w.doctype, w.op) != ("Purchase Order", Op.CREATE):
            return _refuse(f"{w.op.value} on {w.doctype} is not supported by the "
                           "purchase-to-pay pack yet.")
        from erp_guard.service import InvalidArgs
        fields = {k: v for k, v in w.fields.items()
                  if not (k == "doctype" and v == "Purchase Order")}
        unsupported = sorted(set(fields) - set(_PO_HEADER))
        rows = fields.get("items")
        if isinstance(rows, list):
            unsupported += sorted({f"items[].{k}" for r in rows if isinstance(r, dict)
                                   for k in r if k not in _PO_ROW and k != "doctype"})
        if unsupported:
            return _refuse(
                f"Purchase Order field(s) {', '.join(unsupported)} are not supported through "
                "create_document. It takes supplier, erp_guard_cost_centre, erp_guard_notes and "
                "items (item_code, qty, rate, description); or use create_purchase_order.")
        args = {_PO_HEADER[k]: v for k, v in fields.items()}
        if isinstance(rows, list):
            args["lines"] = [{k: v for k, v in r.items() if k != "doctype"}
                             if isinstance(r, dict) else r for r in rows]
        action = svc.actions["create_purchase_order"]
        try:
            return action.name, action.canonicalise(args)
        except InvalidArgs as exc:
            return _refuse(f"Purchase Order: {exc}.")

    def evaluate_write(self, svc: Service, w: Write) -> tuple[str, list[CheckResult]]:
        route = self._route(svc, w)
        if isinstance(route, CheckResult):
            return f"{w.op.value.capitalize()} {w.doctype} {w.name or ''}".rstrip() + ".", \
                [route]
        name, canon = route
        return svc.actions[name].evaluate(svc, canon)

    def apply_write(self, svc: Service, w: Write) -> tuple[str, str]:
        route = self._route(svc, w)
        if isinstance(route, CheckResult):  # evaluate blocks first; never reached
            raise ErpError(route.message)
        name, canon = route
        return svc.actions[name].write(svc, canon)
