"""A turn's text across tool rounds (orchestrator/executive.py): each round's
text starts a new paragraph, so the model's words before a tool call and its
answer never run together ("...stock.Yes. ITEM-1..."). Found by the ERPNext
evals against a live bench (2026-10-19). A short one-line note before a tool
call is the model's own between calls and stays out of the reply ("Need the
company name.", pilot, 2026-10-29); longer text before a call is kept."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator.executive import Executive, _round_separator
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session

from ._agent_loop_fakes import FakeStream, FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock


class StreamingFake(FakeStream):
    """Streams each text block of the final message as a text delta, as the
    API does, before the final message."""

    def __init__(self, final_msg: FinalMsg) -> None:
        super().__init__(final_msg)
        self._deltas = [
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=b.text))
            for b in final_msg.content
            if getattr(b, "type", "") == "text"
        ]

    async def __anext__(self) -> Any:
        if not self._deltas:
            raise StopAsyncIteration
        return self._deltas.pop(0)


class StreamingProvider(ScriptedProvider):
    def messages_stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        return StreamingFake(self._final_msgs.pop(0))


def _reply(rounds: list[FinalMsg]) -> str:
    provider = StreamingProvider(rounds)
    text: list[str] = []

    async def go() -> None:
        with (
            patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
            patch("openexecutive.orchestrator.executive.audit_log", lambda *_a, **_k: None),
            set_session(Session()),
        ):
            async for item in Executive()._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "x"}],
                model="claude-test", workspace_mode="team", principal_role_tag="", turn_id="t-1",
            ):
                if isinstance(item, str) and item != Executive._THINKING:
                    text.append(item)

    asyncio.run(go())
    return "".join(text)


@pytest.mark.parametrize(("tail", "text", "separator"), [
    ("stock.", "Yes.", "\n\n"),
    ("stock.\n", "Yes.", "\n"),
    ("stock.", "\nYes.", "\n"),
    ("stock.\n\n", "Yes.", ""),
    ("stock.", "\n\nYes.", ""),
])
def test_a_new_round_starts_a_new_paragraph(tail: str, text: str, separator: str) -> None:
    assert _round_separator(tail, text) == separator


def test_a_turn_keeps_every_round_in_its_own_paragraph() -> None:
    before = "I will check actual stock in every warehouse first, then compare it with the reorder level."
    reply = _reply([
        FinalMsg([TextBlock(before), ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([TextBlock("Yes. ITEM-1 has 12 in Stores.")], "end_turn"),
    ])
    assert reply == before + "\n\nYes. ITEM-1 has 12 in Stores."


def test_a_short_note_before_a_tool_call_is_not_part_of_the_reply() -> None:
    reply = _reply([
        FinalMsg([TextBlock("Check actual stock."), ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([TextBlock("Yes. ITEM-1 has 12 in Stores.")], "end_turn"),
    ])
    assert reply == "Yes. ITEM-1 has 12 in Stores."


def test_one_round_is_untouched_and_a_silent_round_adds_nothing() -> None:
    assert _reply([FinalMsg([TextBlock("ITEM-1 has 12.")], "end_turn")]) == "ITEM-1 has 12."
    # A round that only calls a tool writes nothing, so no break before the answer.
    reply = _reply([
        FinalMsg([ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([TextBlock("ITEM-1 has 12.")], "end_turn"),
    ])
    assert reply == "ITEM-1 has 12."


def test_a_claim_check_retry_starts_one_new_paragraph(monkeypatch: pytest.MonkeyPatch) -> None:
    # The retry round's break came twice: the loop's own "\n\n" before the
    # retry, then _round_separator's, which never saw it (PR #562 review).
    from openexecutive.orchestrator import claim_check

    async def unbacked(model: str, turn: str) -> dict[str, Any]:
        return {"unbacked": True, "claim": "Done"}

    monkeypatch.setenv("CLAIM_CHECK_ENABLED", "true")
    monkeypatch.setattr(claim_check, "_call_model", unbacked)
    reply = _reply([
        FinalMsg([TextBlock("Done.")], "end_turn"),
        FinalMsg([TextBlock("Actually I haven't removed it yet.")], "end_turn"),
    ])
    assert reply == "Done.\n\nActually I haven't removed it yet."


def test_a_collected_reply_is_rewritten_not_corrected(monkeypatch: pytest.MonkeyPatch) -> None:
    # An email reply is read whole, so a claim-check retry while
    # Executive.chat collects replaces the first reply instead of trailing a
    # "Correction:" after its sign-off.
    from openexecutive.orchestrator import claim_check
    from openexecutive.orchestrator import executive as executive_mod

    async def unbacked(model: str, turn: str) -> dict[str, Any]:
        return {"unbacked": True, "claim": "Added Avery"}

    monkeypatch.setenv("CLAIM_CHECK_ENABLED", "true")
    monkeypatch.setattr(claim_check, "_call_model", unbacked)
    provider = StreamingProvider([
        FinalMsg([TextBlock("I've added Avery. — OE")], "end_turn"),
        FinalMsg([TextBlock("\n\nAvery is on this week's plan as a reviewer. — OE")], "end_turn"),
    ])
    out: list[Any] = []

    async def go() -> None:
        token = executive_mod._COLLECTING.set(True)
        try:
            with (
                patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
                patch("openexecutive.orchestrator.executive.audit_log", lambda *_a, **_k: None),
                set_session(Session()),
            ):
                async for item in Executive()._stream_agent_loop(
                    system_blocks=[], messages=[{"role": "user", "content": "x"}],
                    model="claude-test", workspace_mode="team", principal_role_tag="", turn_id="t-1",
                ):
                    out.append(item)
        finally:
            executive_mod._COLLECTING.reset(token)

    asyncio.run(go())
    marker = out.index({"type": "rewrite"})
    after = "".join(i for i in out[marker:] if isinstance(i, str) and i != Executive._THINKING)
    assert "I've added Avery" not in after and "on this week's plan" in after
    nudge = provider.calls[1]["messages"][-1]["content"][0]["text"]
    assert "write your whole reply again" in nudge
    assert claim_check.nudge("x") != claim_check.nudge("x", rewrite=True)


# --- A round after tool results that ends with no text (eval runs: an empty
# reply, or one cut off at "...after every failed review:") ---

def _reply_and_calls(rounds: list[FinalMsg]) -> tuple[str, list[dict[str, Any]]]:
    provider = StreamingProvider(rounds)
    text: list[str] = []

    async def go() -> None:
        with (
            patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
            patch("openexecutive.orchestrator.executive.audit_log", lambda *_a, **_k: None),
            set_session(Session()),
        ):
            async for item in Executive()._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "x"}],
                model="claude-test", workspace_mode="team", principal_role_tag="", turn_id="t-1",
            ):
                if isinstance(item, str) and item != Executive._THINKING:
                    text.append(item)

    asyncio.run(go())
    return "".join(text), provider.calls


def _last_user_text(call: dict[str, Any]) -> str:
    last = call["messages"][-1]
    content = last["content"]
    return content if isinstance(content, str) else " ".join(b.get("text", "") for b in content if isinstance(b, dict))


def test_an_empty_round_after_tools_is_asked_once_more() -> None:
    from openexecutive.orchestrator.executive import EMPTY_ROUND_NOTE

    reply, calls = _reply_and_calls([
        FinalMsg([ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([], "end_turn"),
        FinalMsg([TextBlock("Metro pays 55 days late; the lowest week holds.")], "end_turn"),
    ])
    assert reply == "Metro pays 55 days late; the lowest week holds."
    assert len(calls) == 3 and EMPTY_ROUND_NOTE in _last_user_text(calls[2])


def test_a_reply_cut_off_before_its_last_call_is_continued_not_repeated() -> None:
    from openexecutive.orchestrator.executive import EMPTY_ROUND_CONTINUE_NOTE

    before = "We keep failing because most late deliveries are back orders. The minutes record the same decision:"
    reply, calls = _reply_and_calls([
        FinalMsg([TextBlock(before), ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([], "end_turn"),
        FinalMsg([TextBlock("order earlier from the second supplier.")], "end_turn"),
    ])
    assert reply == before + "\n\norder earlier from the second supplier."
    assert EMPTY_ROUND_CONTINUE_NOTE in _last_user_text(calls[2])


def test_an_empty_round_is_asked_once_only_and_never_without_tools() -> None:
    reply, calls = _reply_and_calls([
        FinalMsg([ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([], "end_turn"),
        FinalMsg([], "end_turn"),
    ])
    assert reply == "" and len(calls) == 3
    # A whole reply before a trailing call isn't asked to go on.
    # Past the short-note length, so it streams as the reply.
    whole = "Metro pays 55 days late on average, so its receipts move out by about eight weeks; the lowest week still holds at $41,200."
    reply, calls = _reply_and_calls([
        FinalMsg([TextBlock(whole), ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([], "end_turn"),
    ])
    assert reply == whole and len(calls) == 2
    # A first round with no text is asked once more too.
    reply, calls = _reply_and_calls([FinalMsg([], "end_turn"), FinalMsg([TextBlock("ITEM-1 has 12.")], "end_turn")])
    assert reply == "ITEM-1 has 12." and len(calls) == 2


def test_an_empty_round_at_the_output_limit_is_asked_once_more() -> None:
    reply, calls = _reply_and_calls([
        FinalMsg([ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([], "max_tokens"),
        FinalMsg([TextBlock("ITEM-1 has 12.")], "end_turn"),
    ])
    assert reply == "ITEM-1 has 12." and len(calls) == 3


def test_a_refusal_with_nothing_written_says_so_and_is_never_retried() -> None:
    from openexecutive.orchestrator.executive import REFUSED_REPLY

    reply, calls = _reply_and_calls([
        FinalMsg([ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
        FinalMsg([], "refusal"),
    ])
    assert reply == REFUSED_REPLY and len(calls) == 2


def test_an_empty_turn_logs_why_without_text(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="openexecutive.orchestrator.executive"):
        _reply_and_calls([
            FinalMsg([ToolUseBlock("tu1", "no_such_tool", {})], "tool_use"),
            FinalMsg([], "end_turn"),
            FinalMsg([], "end_turn"),
        ])
    assert "turn ended with no reply: stop_reason=end_turn iter=3/15 empty_retried=True" in caplog.text

