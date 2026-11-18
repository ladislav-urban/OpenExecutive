"""A collected reply's claim-check rewrite is kept whole (eval erpnext_075 and
064, Nov 18, 2026).

``Executive.chat`` collects a reply (the ERPNext panel, chat apps, email).
When the claim check flags it, the loop yields ``{"type": "rewrite"}`` and the
model writes the whole reply again; ``chat`` drops what it had. That retry
went through ``repeats.new_line``, the streaming web chat's one-short-line
trim, so 075's six-row table came back as 294 chars ending at a bare header.
In 064 the answer check changed a sentence and ``reply_replaced`` carried
``stream_chat``'s ``full_response``: the first draft plus the stray line,
which session history and the audit row kept too.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator import answer_check, claim_check
from openexecutive.orchestrator import executive as executive_mod
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.session import Session
from openexecutive.utils import repeats

from ._agent_loop_fakes import FinalMsg, TextBlock
from .test_executive_memory_text import _no_audit_writes  # noqa: F401
from .test_reply_rounds import StreamingProvider

ROWS = [
    ("Pinecrest Supply", "123.40", "Oct 2"),
    ("Harbor Freight Co", "4,656.00", "Oct 9"),
    ("Metro Builders", "4,700.00", "Sep 28"),
    ("Riverbend Foods", "2,900.00", "Oct 14"),
    ("Brightline Media", "600.00", "Oct 21"),
    ("Northwind Supplies", "12,500.00", "Oct 30"),
]
TABLE = "| Customer | Owed | Was due |\n|---|---|---|\n" + "\n".join(
    f"| {name} | {owed} | {due} |" for name, owed, due in ROWS
)
# The first reply: complete, with the figure correction that tripped the old
# check, and a real change claim no call backs.
FIRST = (
    "Six customers are past due:\n\n" + TABLE + "\n\n"
    "The ERP now shows Pinecrest owed 123.40, not the 67 I gave earlier. "
    "I've noted a follow-up for each of them."
)
# The rewrite: the whole reply again, saying only what is true.
REWRITE = (
    "Six customers are past due:\n\n" + TABLE + "\n\n"
    "The ERP now shows Pinecrest owed 123.40, not the 67 I gave earlier. "
    "I couldn't note the follow-ups from here; say if you want them as tasks."
)
CLAIM = "I've noted a follow-up for each of them"


@pytest.fixture()
def turn(monkeypatch: pytest.MonkeyPatch, _no_audit_writes: None) -> dict[str, Any]:  # noqa: F811
    """The claim check on and flagging the first reply, the audit rows kept
    here, and every post-turn pass stubbed."""
    rows: list[dict[str, Any]] = []
    checks: list[str] = []

    async def flag(model: str, text: str) -> dict[str, Any]:
        checks.append(text)
        return {"unbacked": True, "claim": CLAIM}

    async def no_title(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setenv("CLAIM_CHECK_ENABLED", "true")
    monkeypatch.setattr(claim_check, "_call_model", flag)
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.audit_log",
        lambda event_type, summary, **kw: rows.append({"event_type": event_type, **kw}),
    )
    monkeypatch.setattr("openexecutive.utils.session_title.generate_session_title", no_title)
    monkeypatch.setattr("openexecutive.memory.honcho_client.sync_turn", lambda *a, **k: None)
    monkeypatch.setattr("openexecutive.memory.episodic.schedule_extraction", lambda *a, **k: None)
    monkeypatch.setattr("openexecutive.attunement.open_loops.schedule_open_loop_pass", lambda *a, **k: None)
    return {"rows": rows, "checks": checks}


def _provider() -> StreamingProvider:
    return StreamingProvider([
        FinalMsg([TextBlock(FIRST)], "end_turn"),
        FinalMsg([TextBlock(REWRITE)], "end_turn"),
    ])


def _response_rows(rows: list[dict[str, Any]]) -> list[str]:
    return [r["full"]["response"] for r in rows if r["event_type"] == "chat_turn"]


def test_chat_keeps_the_whole_rewrite_with_its_table_rows(turn: dict[str, Any]) -> None:
    # 075: the rewrite came back as 294 chars ending at the table's header.
    session = Session(session_id="s-075", company_profile=CompanyProfile())
    provider = _provider()

    async def same(text: str, _turn: Any, **_kw: Any) -> str:
        return text

    with (
        patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
        patch.object(executive_mod.answer_check, "check_reply", new=same),
    ):
        out = asyncio.run(Executive().chat("Who owes us?", session, peer_memory_context=""))

    assert len(provider.calls) == 2, "the claim check asked for one more round"
    assert "write your whole reply again" in provider.calls[1]["messages"][-1]["content"][0]["text"]
    assert out == REWRITE
    assert len(out) > repeats.MAX_LINE_CHARS
    for name, owed, _due in ROWS:
        assert f"| {name} | {owed} |" in out
    assert CLAIM not in out
    # History and the audit row keep the rewrite, never the first draft.
    assert session.get_recent_history()[-1]["content"] == REWRITE
    assert _response_rows(turn["rows"]) == [REWRITE]


def test_a_checked_rewrite_replaces_the_reply_not_the_first_draft(turn: dict[str, Any]) -> None:
    # 064: the answer check changed a sentence, and reply_replaced carried the
    # first draft plus the stray trimmed line.
    session = Session(session_id="s-064", company_profile=CompanyProfile())
    seen: list[str] = []

    async def corrected(text: str, _turn: Any, **_kw: Any) -> str:
        seen.append(text)
        return text.replace("| Oct 2 |", "| Oct 3 |")

    replaced: list[dict[str, Any]] = []

    async def go() -> str:
        executive = Executive()
        stream_chat = executive.stream_chat

        async def watched(**kw: Any) -> Any:
            async for item in stream_chat(**kw):
                if isinstance(item, dict) and item.get("type") == "reply_replaced":
                    replaced.append(item)
                yield item

        executive.stream_chat = watched  # type: ignore[method-assign]
        return await executive.chat("Who owes us?", session, peer_memory_context="")

    with (
        patch("openexecutive.orchestrator.executive.get_provider", return_value=_provider()),
        patch.object(executive_mod.answer_check, "check_reply", new=corrected),
    ):
        out = asyncio.run(go())

    want = REWRITE.replace("| Oct 2 |", "| Oct 3 |")
    assert seen == [REWRITE], "the answer check reads the rewrite alone"
    assert [r["content"] for r in replaced] == [want]
    assert out == want
    assert CLAIM not in out and out.count("| Customer |") == 1
    assert session.get_recent_history()[-1]["content"] == want
    assert _response_rows(turn["rows"]) == [want]


def test_a_rewrite_that_writes_nothing_keeps_the_first_reply(turn: dict[str, Any]) -> None:
    session = Session(session_id="s-empty", company_profile=CompanyProfile())
    provider = StreamingProvider([
        FinalMsg([TextBlock(FIRST)], "end_turn"),
        FinalMsg([], "end_turn"),
        FinalMsg([], "end_turn"),
    ])

    async def same(text: str, _turn: Any, **_kw: Any) -> str:
        return text

    with (
        patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
        patch.object(executive_mod.answer_check, "check_reply", new=same),
    ):
        out = asyncio.run(Executive().chat("Who owes us?", session, peer_memory_context=""))

    assert out == FIRST
    assert session.get_recent_history()[-1]["content"] == FIRST


def test_the_streaming_web_chat_still_adds_only_a_short_line(turn: dict[str, Any]) -> None:
    # Not collected: the first reply was already shown, so the retry adds one
    # short line of what it didn't say, and no table comes twice.
    session = Session(session_id="s-web", company_profile=CompanyProfile())
    items: list[Any] = []

    async def same(text: str, _turn: Any, **_kw: Any) -> str:
        return text

    async def go() -> None:
        with answer_check.collect(answer_check.AnswerCheckTurn()):
            async for item in Executive().stream_chat(user_message="Who owes us?", session=session):
                items.append(item)

    with (
        patch("openexecutive.orchestrator.executive.get_provider", return_value=_provider()),
        patch.object(executive_mod.answer_check, "check_reply", new=same),
    ):
        asyncio.run(go())

    assert {"type": "rewrite"} not in items
    text = "".join(i for i in items if isinstance(i, str) and i != Executive._THINKING)
    assert text.startswith(FIRST)
    added = text[len(FIRST):].strip()
    assert added == "I couldn't note the follow-ups from here; say if you want them as tasks."
    assert len(added) <= repeats.MAX_LINE_CHARS
    assert text.count("| Customer |") == 1


def test_the_committee_reviews_the_rewritten_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator.committee_reviewers import Critique

    reviewed: list[str] = []
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.audit_log",
        lambda event_type, summary, **kw: rows.append({"event_type": event_type, **kw}),
    )
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None, raising=False)

    async def draft_loop(*_a: Any, **_kw: Any) -> Any:
        yield "First draft, flagged."
        yield {"type": "rewrite"}
        yield "Second draft, whole."

    async def review(_self: Any, *args: Any, **kwargs: Any) -> list[Critique]:
        reviewed.extend(str(a) for a in (*args, *kwargs.values()) if isinstance(a, str))
        return [Critique("quality_judge", "low", "ok", "none")]

    class _Stream:
        async def __aenter__(self) -> _Stream:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        def __aiter__(self) -> _Stream:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

        async def get_final_message(self) -> None:
            return None

    class _Provider:
        def messages_stream(self, **_kw: Any) -> _Stream:
            return _Stream()

    async def go() -> None:
        async for _ in Executive().stream_chat_with_committee(
            user_message="Hi", session=Session(session_id="s-c", company_profile=CompanyProfile()),
        ):
            pass

    with (
        patch.object(Executive, "_stream_agent_loop", new=draft_loop),
        patch("openexecutive.orchestrator.committee.Committee.review", new=review),
        patch("openexecutive.orchestrator.executive.get_provider", return_value=_Provider()),
    ):
        asyncio.run(go())

    assert "Second draft, whole." in reviewed and "First draft, flagged." not in "".join(reviewed)
    drafts = [r["full"].get("draft") for r in rows if r["event_type"] == "chat_turn"]
    assert drafts == ["Second draft, whole."]
