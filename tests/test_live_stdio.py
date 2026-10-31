"""End to end over real MCP stdio: the server as a subprocess, two ERPNext users.

Skipped unless the local ERPNext from `make up && make seed` is reachable.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_live_erpnext import _up

from erp_guard.connectors.erpnext import ErpNext
from erp_guard.env import load_env

pytestmark = [pytest.mark.live, pytest.mark.skipif(not _up(), reason="local ERPNext not up")]
ROOT = Path(__file__).resolve().parents[1]


def params(tag: str) -> StdioServerParameters:
    load_env(ROOT / ".env")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "ERP_GUARD_ERP": "erpnext",
           "ERPNEXT_API_KEY": os.environ[f"ERPNEXT_KEY_{tag}"],
           "ERPNEXT_API_SECRET": os.environ[f"ERPNEXT_SECRET_{tag}"]}
    return StdioServerParameters(command=sys.executable, args=["-m", "erp_guard.server"],
                                 env=env, cwd=str(ROOT))


async def call(session: ClientSession, tool: str, **args) -> str:
    res = await session.call_tool(tool, args)
    return "\n".join(c.text for c in res.content if getattr(c, "text", None))


async def _flow() -> tuple[str, list[str]]:
    seen = []
    order = {"supplier": "GRAYBAR ELECTRIC COMPANY, INC", "cost_centre": "Facilities - LDU",
             "lines": [{"item_code": "HVAC-SVC", "qty": 1, "rate": 650}]}
    async with stdio_client(params("REQUESTER")) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        seen.append(await call(s, "whoami"))
        text = await call(s, "preview", action="create_purchase_order", args=order)
        code = re.search(r"Confirmation code: `(\w+)`", text).group(1)
        seen.append(await call(s, "create_purchase_order", **order, confirmation_code=code))
        po = re.search(r"(PUR-ORD-\d{4}-\d+)", seen[-1]).group(1)
        text = await call(s, "preview", action="submit_for_approval", args={"po": po})
        code = re.search(r"Confirmation code: `(\w+)`", text).group(1)
        seen.append(await call(s, "submit_for_approval", po=po, confirmation_code=code))
        seen.append(await call(s, "preview", action="approve_purchase_order", args={"po": po}))
    async with stdio_client(params("MANAGER")) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        text = await call(s, "preview", action="approve_purchase_order", args={"po": po})
        code = re.search(r"Confirmation code: `(\w+)`", text).group(1)
        seen.append(await call(s, "approve_purchase_order", po=po, confirmation_code=code))
        seen.append(await call(s, "audit_log", target=po))
    return po, seen


def test_stdio_end_to_end():
    po, seen = asyncio.run(_flow())
    try:
        assert "requester@example.com" in seen[0]
        assert seen[1].startswith("**Done:**") and seen[2].startswith("**Done:**")
        assert "No confirmation code" in seen[3] and "segregation_of_duties" in seen[3]
        assert seen[4].startswith("**Done:**")
        assert seen[5].count("written") >= 3
        o = ErpNext.from_env("DIRECTOR").get_order(po)
        assert o.status.value == "Approved" and o.approver == "manager@example.com"
    finally:
        ErpNext.from_env("DIRECTOR").cancel_order(po, "test cleanup")
