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
    "answer_check_context": "e47289b5bb6c6d13adf87286a8faacb55014aaca18b752ae25a424f482db59b6",
    "audit_drafts": "086c6aaa377a06f3104d176d57cd9201ff14ab9e0f510a059e373df67d992149",
    "confirm_figure": "7e92126de1a33bced94e4b247220bac4fddaf8c26f0ec141fd612011cf34102b",
    "customer_payment_record": "fc960953b41f8a2cfc639e1d3277b38e348883a089bda0f5d60707ea18f7d4ea",
    "describe_doctype": "857eb11de056c01ca1a85be1552e22e04068b63bf37ccdf7de15edfc99cc13a3",
    "describe_report": "702e1c25ef62f819c2efe0421270b83779f882ae709067cc2189696e32ff4f7c",
    "draft_check": "d203c32661b2c09d54815cca812e9e447243a121e4e9ce0487d08d35eed9921d",
    "draft_comment": "7990cbcbade4a1aee9d8e8afab2a91df82d9d700fcbf109389758053af5b43b5",
    "draft_dunning": "e02463385959089f32f6d145348e87b69b1e67e4e72b1eaf4f30e689e005c45c",
    "draft_material_request": "4397bbc6bef11249ee0943c62cb354f536cef005e4438597c5742a405538ca4d",
    "draft_purchase_order": "e184ec2763b3c2de7bda7851ec656519943f9162bfa181769e1c440e9043b53e",
    "draft_quotation": "9418aa69f719a5399d7c714fa338f579851b882364f321f344c7369793b08bbf",
    "draft_todo": "1f1c2e65ba1a4bbba66cd74d40a9701d819bd580e7110f18db16f21ec268a1ed",
    "draft_update": "e1508efc4843b8d36ea5f2fb45c5e8c2ddc337bb3dcd470fa93485f333ce2b9f",
    "get_record": "4d486bf8e9cc6afce155b722b3565f4908563b4580838722a3ee7d28841c3698",
    "list_records": "11b5293628e6c26937a4b8c568231f1bec8e41b977de0a05675fa492aa34f387",
    "read_attachment": "e2890a5ed9875146b5af0ee563f24ba76c0b390cea1bee9481a40a762f14773d",
    "run_report": "43fc20d9c48e80bd64df2c16530ad84d6d30083fc6972d0f51cecd1bd218acb4",
    "search_catalog": "dbe9c1e72588824ebed3dcbfc01e0d90b39d4ad82ab0168d9cc6c0756b0f137c",
    "show_in_erpnext": "dff36d8d7403b07ceeef7e195ef117c5dee9f5adf8a42467418e5738a0d696d6",
    "supplier_bank_changed": "5cbdd446fb7c1b3da9fed53cc0a23930e019a5a6cbc9ad175bba6e9c8355f505",
    "supplier_invoices": "f11300d594d4c9dd89e0f02e6fc23e1bda522fef3259ba3fb62321b5b83b0875",
    "supplier_order": "e4f891661b8fad016d322c8dead3f550affb761cfb8555ede7a77b316be61970",
    "supplier_orders": "abfa94e11de532f9cf1531c221dd4f8e8120816c215fb4b2780fd2edc338c1f8",
}
# oe_connector's protocol.TOOLS_DIGEST for the definitions pinned above.
TOOLS_DIGEST = "6cb3653333f63e68558e067da36426a56c5385d12bad13a2647f8047f3fde38a"

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
