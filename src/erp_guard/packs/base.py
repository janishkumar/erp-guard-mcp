"""What a domain pack offers the service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from erp_guard.checks import CheckResult
from erp_guard.ops import Write

if TYPE_CHECKING:
    from erp_guard.service import Service


class Pack(Protocol):
    name: str
    # Task tools by the document type they work on, preferred over the generic tools.
    task_tools: dict[str, tuple[str, ...]]
    # Document types owned only in some cases: when, for describe_doctype.
    ownership_notes: dict[str, str]

    def owns(self, doctype: str) -> bool:
        """True if writes to this document type must go through this pack's checks."""
        ...

    def owns_write(self, svc: Service, w: Write) -> bool:
        """For a type it owns: whether this write is the pack's (some types are owned only
        for some documents). False sends it to the generic checks."""
        ...

    def evaluate_write(self, svc: Service, w: Write) -> tuple[str, list[CheckResult]]:
        """(human summary, check results) for a proposed write. Reads only."""
        ...

    def apply_write(self, svc: Service, w: Write) -> tuple[str, str]:
        """Do the write; (target id, message)."""
        ...

    # Optional: `also_acts_on(svc, doctype, name) -> list[(doctype, name)]`. The existing
    # documents a write on this one also saves (beyond the one named); a confirmation code
    # binds their version too. Packs without it act on the named document only.
