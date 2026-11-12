"""The chat loop's last round answers with what it has, and a short note the
model writes before a tool call never reaches the reply (pilot, Oct 29, 2026:
"I was unable to complete the analysis" after a real answer, and "Need
details." opening one)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from openexecutive.orchestrator import executive as executive_module
from openexecutive.orchestrator.session import Session

TOOL = "list_people"


class _Rounds:
    """A provider whose rounds stream ``text`` then end as ``stop``."""

    def __init__(self, rounds: list[tuple[str, str]]) -> None:
        self._rounds = list(rounds)
        self.calls: list[dict[str, Any]] = []

    def messages_stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text, stop = self._rounds.pop(0)
        content: list[Any] = [SimpleNamespace(type="text", text=text)] if text else []
        if stop == "tool_use":
            content.append(SimpleNamespace(type="tool_use", id=f"tu-{len(self.calls)}", name=TOOL, input={}))
        final = SimpleNamespace(content=content, stop_reason=stop, usage=None)
        pieces = [text[i:i + 7] for i in range(0, len(text), 7)] if text else []
        events = [
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=piece))
            for piece in pieces
        ]

        class _Stream:
            async def __aenter__(self) -> _Stream:
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            def __aiter__(self) -> _Stream:
                return self

            async def __anext__(self) -> Any:
                if not events:
                    raise StopAsyncIteration
                return events.pop(0)

            async def get_final_message(self) -> Any:
                return final

        return _Stream()


def _drive(rounds: list[tuple[str, str]], max_iterations: int = 15) -> tuple[str, _Rounds]:
    from openexecutive.orchestrator.schedule_tools import set_session

    provider = _Rounds(rounds)
    handler = AsyncMock(return_value=json.dumps({"people": []}))
    out: list[str] = []

    async def _go() -> None:
        with (
            patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
            patch("openexecutive.orchestrator.executive.audit_log", lambda *a, **k: None),
            patch.dict(executive_module._ALL_SKILL_HANDLERS, {TOOL: handler}),
            set_session(Session(session_id="t-limits")),
        ):
            async for item in executive_module.Executive(mcp_gateway=None)._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": "x"}],
                model="claude-test",
                workspace_mode="team",
                principal_role_tag="",
                turn_id="t-limits",
                max_iterations=max_iterations,
            ):
                if isinstance(item, str):
                    out.append(item)

    asyncio.run(_go())
    return "".join(out), provider


def test_a_short_note_before_a_tool_call_stays_out_of_the_reply() -> None:
    reply, _ = _drive([("Need details.", "tool_use"), ("Here is the answer.", "end_turn")])
    assert "Need details." not in reply
    assert "Here is the answer." in reply


def test_longer_text_before_a_tool_call_still_streams() -> None:
    long = "I will look at the open invoices first, then the credit limit, to see what releases the hold."
    reply, _ = _drive([(long, "tool_use"), ("Done.", "end_turn")])
    assert long in reply and "Done." in reply


def test_the_last_round_answers_without_tools() -> None:
    reply, provider = _drive(
        [("", "tool_use"), ("", "tool_use"), ("From what I found: two invoices are open.", "end_turn")],
        max_iterations=3,
    )
    assert "unable to complete" not in reply
    assert "two invoices are open" in reply
    assert provider.calls[-1].get("tool_choice") == {"type": "none"}
    assert all("tool_choice" not in call for call in provider.calls[:-1])
    told = provider.calls[-1]["messages"][-1]["content"][-1]
    assert told == {"type": "text", "text": executive_module.LAST_ROUND_NOTE}
