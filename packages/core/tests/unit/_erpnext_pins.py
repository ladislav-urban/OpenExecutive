"""Pin the made-up ERPNext tool definitions a test's fake search returns, so
the gateway's digest check (orchestrator/erpnext_tool_pin.py) keeps them."""
from __future__ import annotations

import pytest

from openexecutive.orchestrator import erpnext_tool_pin
from openexecutive.workflows.tool_catalog import parse_search_results


def pin_listing(monkeypatch: pytest.MonkeyPatch, *listings: str) -> None:
    """Pin every ``erpnext__*`` tool in these ``search_tools`` answers as it
    appears there (the rest of the pin stays as it is)."""
    pins = dict(erpnext_tool_pin.TOOL_DIGESTS)
    for listing in listings:
        for info in parse_search_results(listing):
            if info.name.startswith(erpnext_tool_pin.ERPNEXT_PREFIX):
                name = info.name[len(erpnext_tool_pin.ERPNEXT_PREFIX):]
                pins[name] = erpnext_tool_pin.definition_digest(
                    {"name": name, "description": info.description, "inputSchema": info.input_schema}
                )
    monkeypatch.setattr(erpnext_tool_pin, "TOOL_DIGESTS", pins)
