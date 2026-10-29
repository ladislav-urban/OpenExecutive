"""The pinned digests of oe_connector's tool definitions.

Design "Next", item 5 (oe_connector's ``docs/erpnext-integration.md``). What
the model is told an ``erpnext__*`` tool does is its description and input
schema as ``search_tools`` returns them, straight from oe_connector. Someone
who can change the app on the bench could change what a tool claims to do, or
add an instruction to a description, with no visible sign. So this install
pins each tool's digest, and the gateway drops at discovery any ``erpnext__*``
tool whose definition doesn't match (``mcp_gateway``): it is never shown, and
a call to it is refused.

**The digest** is oe_connector's ``protocol.definition_digest``: SHA-256 of
``{name, description, inputSchema}`` as ``tools/list`` serves them, spelled as
``erpnext_caller.canonical_json`` (sorted keys, no spaces, UTF-8); ``name`` is
oe_connector's own (``run_report``, not ``erpnext__run_report``). The title
and annotations are left out. ``tools_digest`` over every pin is oe_connector's
``TOOLS_DIGEST``; the tests check both against its vector.

**Changing a tool.** A release of oe_connector that changes a definition
updates its ``TOOLS_DIGEST`` and this pin in the same pair of PRs; ``bench
oe-connector-tool-digests`` prints both. Until then the changed tool is
dropped here, and the gateway's log says which and why.
"""
from __future__ import annotations

import hashlib
from typing import Any

from openexecutive.orchestrator.erpnext_caller import canonical_json

ERPNEXT_PREFIX = "erpnext__"

# `bench oe-connector-tool-digests`, oe_connector at TOOLS_DIGEST below.
TOOL_DIGESTS: dict[str, str] = {
    "audit_drafts": "086c6aaa377a06f3104d176d57cd9201ff14ab9e0f510a059e373df67d992149",
    "describe_doctype": "857eb11de056c01ca1a85be1552e22e04068b63bf37ccdf7de15edfc99cc13a3",
    "describe_report": "1fa2a5e43466dac51346394bb4aa205f71171fe253e73681c7dc0835b0053753",
    "draft_check": "c97ed34120d93ce74bc8097e89416c98d19f59988ecd6fd812d36dc3e475e795",
    "draft_material_request": "30e1ed9c99e8c1b8823bcb977dbc7198916092c9fbc36198859e213439ac4d9e",
    "draft_purchase_order": "04132a8a2ffc64c3ad021756e2083a223fd74db977ba6bf379213fa68d0ee965",
    "draft_quotation": "9fca468a1a9b942281037d47451a81693bdded098ee1d12b9bf16135df47f2ee",
    "draft_todo": "4afb6078ba4a01690ffdad47fed610e0afd2112a8b6b00e19773a901e418b19b",
    "get_record": "4d486bf8e9cc6afce155b722b3565f4908563b4580838722a3ee7d28841c3698",
    "list_records": "11b5293628e6c26937a4b8c568231f1bec8e41b977de0a05675fa492aa34f387",
    "run_report": "43fc20d9c48e80bd64df2c16530ad84d6d30083fc6972d0f51cecd1bd218acb4",
    "search_catalog": "dbe9c1e72588824ebed3dcbfc01e0d90b39d4ad82ab0168d9cc6c0756b0f137c",
    "show_in_erpnext": "452e566099dc6f060ab030200314885afc0d248e84560854ef131a3424a4a115",
    "supplier_invoices": "f11300d594d4c9dd89e0f02e6fc23e1bda522fef3259ba3fb62321b5b83b0875",
    "supplier_order": "e4f891661b8fad016d322c8dead3f550affb761cfb8555ede7a77b316be61970",
    "supplier_orders": "abfa94e11de532f9cf1531c221dd4f8e8120816c215fb4b2780fd2edc338c1f8",
}
# oe_connector's protocol.TOOLS_DIGEST for the definitions pinned above.
TOOLS_DIGEST = "c4260ee11e7833ee721b9ae13f9f102ae94a21526e02d09658f6b7d795d880ff"

# The supplier tools as oe_connector serves them (its mcp/protocol.py
# SUPPLIER_TOOLS), offered to the A2A agent from this copy rather than from
# what the server sends; a test holds each to its digest above.
_SUPPLIER_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
SUPPLIER_TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "supplier_invoices",
        "description": "For a supplier's agent only: the submitted Purchase Invoices from that supplier, "
        "with their bill numbers, due dates, totals and what is still unpaid.",
        "inputSchema": {
            "type": "object",
            "properties": {"unpaid_only": {"type": "boolean"}, "limit": _SUPPLIER_LIMIT},
            "additionalProperties": False,
        },
    },
    {
        "name": "supplier_order",
        "description": "For a supplier's agent only: one submitted Purchase Order to that supplier, with "
        "its lines' items, quantities ordered and received, rates and dates.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The Purchase Order's number"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "supplier_orders",
        "description": "For a supplier's agent only: the submitted Purchase Orders to that supplier, open "
        "ones unless include_done, with dates, status, total and how much is received and billed.",
        "inputSchema": {
            "type": "object",
            "properties": {"include_done": {"type": "boolean"}, "limit": _SUPPLIER_LIMIT},
            "additionalProperties": False,
        },
    },
)


def definition_digest(definition: dict[str, Any]) -> str:
    """SHA-256 of one served definition's name, description and input schema."""
    pinned = {key: definition.get(key) for key in ("name", "description", "inputSchema")}
    return hashlib.sha256(canonical_json(pinned).encode("utf-8")).hexdigest()


def tools_digest(digests: dict[str, str]) -> str:
    """One digest over every tool's: SHA-256 of ``{name: digest}``."""
    return hashlib.sha256(canonical_json(digests).encode("utf-8")).hexdigest()


def mismatch(gateway_name: str, description: str, input_schema: Any) -> str | None:
    """Why the gateway must drop this ``erpnext__*`` tool as ``search_tools``
    returned it, or None when it matches its pin."""
    name = gateway_name[len(ERPNEXT_PREFIX):] if gateway_name.startswith(ERPNEXT_PREFIX) else gateway_name
    expected = TOOL_DIGESTS.get(name)
    if expected is None:
        return "not pinned"
    got = definition_digest({"name": name, "description": description, "inputSchema": input_schema})
    if got != expected:
        return f"digest {got[:12]}… is not the pinned {expected[:12]}…"
    return None
