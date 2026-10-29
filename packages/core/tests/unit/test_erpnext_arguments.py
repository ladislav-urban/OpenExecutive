"""An ERPNext call's arguments, read by the tool's pinned schema
(mcp_gateway._erpnext_arguments), and the argument shapes the prompt gives."""
from __future__ import annotations

from openexecutive.orchestrator.mcp_gateway import _erpnext_arguments

LIST_RECORDS = {
    "type": "object",
    "properties": {
        "doctype": {"type": "string"},
        "fields": {"type": "array", "items": {"type": "string"}},
        "filters": {"anyOf": [{"type": "object"}, {"type": "array", "items": {"type": "array"}}]},
        "limit": {"type": "integer", "minimum": 1},
    },
}


def test_quoted_objects_lists_and_numbers_become_what_the_schema_types() -> None:
    out = _erpnext_arguments(
        {
            "doctype": "Sales Invoice",
            "filters": '{"customer": "Harbor", "docstatus": 1}',
            "fields": "name, customer,outstanding_amount",
            "limit": "200",
        },
        LIST_RECORDS,
    )
    assert out == {
        "doctype": "Sales Invoice",
        "filters": {"customer": "Harbor", "docstatus": 1},
        "fields": ["name", "customer", "outstanding_amount"],
        "limit": 200,
    }
    listed = _erpnext_arguments({"filters": '[["docstatus", "=", 1]]', "fields": '["name"]'}, LIST_RECORDS)
    assert listed == {"filters": [["docstatus", "=", 1]], "fields": ["name"]}


def test_nothing_else_changes() -> None:
    same = {"doctype": "Item", "filters": "{not json", "name": "x", "limit": "ten"}
    assert _erpnext_arguments({"limit": "²"}, LIST_RECORDS) == {"limit": "²"}  # not an ASCII number
    assert _erpnext_arguments(same, LIST_RECORDS) == same  # no rename, no drop
    # A string the schema types as a string stays one.
    assert _erpnext_arguments({"doctype": '{"a": 1}'}, LIST_RECORDS) == {"doctype": '{"a": 1}'}
    # A list where only an object is allowed is passed as it came.
    only_object = {"properties": {"filters": {"type": "object"}}}
    assert _erpnext_arguments({"filters": "[1, 2]"}, only_object) == {"filters": "[1, 2]"}
    # No schema (a tool not verified yet), no change.
    assert _erpnext_arguments({"filters": '{"a": 1}'}, None) == {"filters": '{"a": 1}'}


def test_the_prompts_argument_shapes_follow_the_pin() -> None:
    from openexecutive.orchestrator import erpnext_tool_pin
    from openexecutive.prompts import connected_systems as cs

    # A pin change (a reworded or reshaped tool) must revisit these lines.
    assert cs.ERPNEXT_ARGS_WRITTEN_FOR == erpnext_tool_pin.TOOLS_DIGEST
    for tool in cs.PINNED_ERPNEXT_TOOLS:
        assert f"`{tool.removeprefix('erpnext__')}`" in cs._ERPNEXT_ARGS, tool
