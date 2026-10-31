"""What a write does to one ERP document, independent of which tool asked for it."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Op(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    SUBMIT = "submit"
    WORKFLOW = "workflow"
    CANCEL = "cancel"
    AMEND = "amend"


@dataclass(frozen=True)
class Write:
    op: Op
    doctype: str
    name: str | None = None
    fields: dict = field(default_factory=dict)
    action: str | None = None  # workflow action, for Op.WORKFLOW
    reason: str = ""
