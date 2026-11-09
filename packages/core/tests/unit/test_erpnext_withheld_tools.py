# ruff: noqa: F811 — pytest fixtures imported from test_erpnext_drafts share their names with the test parameters.
"""Tools oe_connector serves and this install pins, but nothing here may call
yet (prompts.connected_systems.ERPNEXT_WITHHELD_TOOLS): reading an attachment
(S4) and a supplier's bank-change fact (S5). The gateway refuses them on every
path, whatever marker or turn is bound, and tool search never shows them."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from openexecutive.orchestrator import erpnext_caller, erpnext_tool_pin, mcp_gateway
from openexecutive.prompts import connected_systems as cs

from .test_delegation_tools import OWNER, db, fresh_turn_state  # noqa: F401
from .test_erpnext_drafts import _owner_turn, erp, extension, roster  # noqa: F401

WITHHELD = sorted(cs.ERPNEXT_WITHHELD_TOOLS)


def test_withheld_tools_are_pinned_and_on_no_list_that_uses_them() -> None:
    pinned = {f"{erpnext_tool_pin.ERPNEXT_PREFIX}{name}" for name in erpnext_tool_pin.TOOL_DIGESTS}
    assert pinned >= cs.ERPNEXT_WITHHELD_TOOLS
    for used in (
        set(cs.PINNED_ERPNEXT_TOOLS), set(cs.ERPNEXT_CODE_ONLY_TOOLS), set(cs.ERPNEXT_SUPPLIER_TOOLS),
        set(cs.ERPNEXT_DRAFT_TOOLS), set(cs.ERPNEXT_DISCOVERY_QUERIES),
    ):
        assert not cs.ERPNEXT_WITHHELD_TOOLS & used


@pytest.mark.parametrize("tool", WITHHELD)
def test_the_gateway_refuses_a_withheld_tool_under_every_marker(erp: Any, roster: Any, tool: str) -> None:
    from openexecutive.orchestrator.schedule_tools import set_session

    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    call = {"name": tool, "arguments": {"doctype": "Purchase Invoice", "name": "PINV-1"}}
    import contextlib

    markers = [
        contextlib.nullcontext,
        erpnext_caller.brief_run,
        erpnext_caller.oversight_run,
        erpnext_caller.answer_check_run,
        lambda: erpnext_caller.supplier_run("Northwind Cables Ltd"),
        lambda: erpnext_caller.draft_check_run(call["arguments"]),
        # draft_executor_run can't be bound to them at all: it takes draft tools only.
    ]

    async def go(marker: Any) -> str:
        with set_session(_owner_turn(roster)), marker():
            return await gateway.call_tool(call)

    for marker in markers:
        out = json.loads(asyncio.run(go(marker)))
        # A supplier's run is refused first by its own allowlist; every other
        # path by the withholding itself.
        assert "doesn't use this ERPNext tool yet" in out["error"] or "supplier" in out["error"], marker
    assert erp.calls == []


def test_tool_search_never_shows_a_withheld_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit._erpnext_pins import pin_listing

    listing = "".join(f"## {name}\n**Description:** {name}\n\n" for name in ("erpnext__run_report", *WITHHELD))
    pin_listing(monkeypatch, listing)
    kept = mcp_gateway._drop_blocked_tools(listing)
    assert "erpnext__run_report" in kept
    for name in WITHHELD:
        assert name not in kept
