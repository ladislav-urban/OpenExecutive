"""The narrow agent that answers a supplier's own agent over A2A.

Design "Next", item 3 (oe_connector's ``docs/erpnext-integration.md``). The
route (``integrations.erpnext_a2a``) has already proved which supplier is
asking; this answers it, with no person approving the reply. It is not the
Executive: it is offered only oe_connector's three supplier tools, from this
install's pinned copy of their definitions
(``erpnext_tool_pin.SUPPLIER_TOOL_DEFINITIONS``), and nothing else: no
specialists, memory, standing facts, knowledge base, mail, briefing or other
MCP servers. It runs with no session, so nothing it reads or says reaches
episodic, peer or department memory, or any conversation.

The supplier's message and every tool result reach the model as untrusted
content (``content_trust.wrap_untrusted``), and its system prompt
(``prompts.supplier_agent``) says it speaks for the company to that supplier
only. The supplier the calls are signed as is the route's, never the model's:
the caller runs this inside ``erpnext_caller.supplier_run``.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from openexecutive.orchestrator.content_trust import wrap_untrusted
from openexecutive.orchestrator.erpnext_tool_pin import ERPNEXT_PREFIX, SUPPLIER_TOOL_DEFINITIONS
from openexecutive.prompts.supplier_agent import SUPPLIER_AGENT_PROMPT

logger = logging.getLogger(__name__)

# Model rounds per exchange: the last one may not call a tool.
MAX_ROUNDS = 5
MAX_TOKENS = 1024
MAX_REPLY_CHARS = 4000
# What one tool result may put in front of the model (oe_connector caps rows
# at 100, which fits).
MAX_TOOL_RESULT_CHARS = 30_000
NO_ANSWER = "I can't answer that right now. Please contact your buyer at the company."

# Calls one supplier tool (its gateway name) with these arguments; returns the
# result text the gateway gave.
ToolCall = Callable[[str, dict[str, Any]], Awaitable[str]]


@dataclass
class SupplierAnswer:
    reply: str
    # The supplier tools called, in order (gateway names).
    tools: list[str] = field(default_factory=list)


def offered_tools() -> list[dict[str, Any]]:
    """The three supplier tools as the model is offered them."""
    return [
        {
            "name": ERPNEXT_PREFIX + definition["name"],
            "description": definition["description"],
            "input_schema": definition["inputSchema"],
        }
        for definition in SUPPLIER_TOOL_DEFINITIONS
    ]


def user_turn(supplier: str, message: str) -> str:
    """Who is asking (the route's word, from the registered key) and what
    its agent sent (untrusted)."""
    return (
        f"The supplier asking: {supplier}. Its registered key signed this request.\n\n"
        + wrap_untrusted(message, source="a2a", author=supplier, author_verified=True)
    )


def _block_dict(block: Any) -> dict[str, Any]:
    kind = getattr(block, "type", "")
    if kind == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    dump = getattr(block, "model_dump", None)
    # A thinking block goes back as it came, signature and all.
    return dump(exclude_none=True) if callable(dump) else {"type": kind}


def _text_of(response: Any) -> str:
    return "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text").strip()


async def answer(supplier: str, message: str, call: ToolCall, *, model: str | None = None) -> SupplierAnswer:
    """Answer ``supplier``'s agent's ``message``, calling the supplier tools
    through ``call``. Raises on a failed model call; the caller says so."""
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.config import get_settings
    from openexecutive.providers import get_provider

    model = model or get_settings().default_model
    tools = offered_tools()
    names = {tool["name"] for tool in tools}
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_turn(supplier, message)}]
    called: list[str] = []
    for round_no in range(MAX_ROUNDS):
        last = round_no == MAX_ROUNDS - 1
        response = await get_provider(model).messages_create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SUPPLIER_AGENT_PROMPT,
            tools=tools,
            # The last round answers with what it has.
            **({"tool_choice": {"type": "none"}} if last else {}),
            messages=messages,
        )
        log_model_usage(response, model=model, actor="erpnext_a2a", iteration=round_no)
        uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        if not uses or last:
            reply = _text_of(response)
            return SupplierAnswer((reply or NO_ANSWER)[:MAX_REPLY_CHARS], called)
        messages.append({"role": "assistant", "content": [_block_dict(b) for b in response.content]})
        results: list[dict[str, Any]] = []
        for use in uses:
            if use.name not in names or not isinstance(use.input, dict):
                text = f"{str(use.name)[:100]} is not available here."
            else:
                called.append(use.name)
                text = await call(use.name, dict(use.input))
            results.append({
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": wrap_untrusted(text[:MAX_TOOL_RESULT_CHARS], source="erpnext", author="erpnext"),
            })
        messages.append({"role": "user", "content": results})
    return SupplierAnswer(NO_ANSWER, called)
