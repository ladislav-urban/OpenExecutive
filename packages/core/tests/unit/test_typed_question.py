"""Only what the person typed decides the answer check's projection test.

``answer_check`` lets figures no ERPNext result holds stand under
``PROJECTION_NOTE`` when the question asks for a projection
(``_PROJECTION_Q``). Matched against the turn's whole "question", an
outsider could word it: an inbound email's subject in a card's headline
("Updated forecast for Q1"), a file named "Q1 forecast.pdf", a forward, a
file's text. So each surface passes ``typed_question``, the raw text the
person typed there, or None where there is none, and only that is tested.
"""
from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route

from .test_chat_route_memory_text import _reset_route_state, captured, client  # noqa: F401
from .test_executive_memory_text import _no_audit_writes, _provider  # noqa: F401

PACKAGE = Path(__file__).resolve().parents[2] / "openexecutive"
HEADLINE = "Updated forecast for Q1"
TYPED = "What if Metro pays as late as it usually does?"


@pytest.fixture(autouse=True)
def _no_session_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new chat's title is a model call: never made from these tests."""

    async def no_title(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("openexecutive.utils.session_title.generate_session_title", no_title)


def _calls(path: Path, names: set[str]) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in names:
                out.append(node)
    return out


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


# --------------------------------------------------------------- the Executive


def _drive_check(entry: str, **kwargs: Any) -> list[dict[str, Any]]:
    """One turn through ``entry``; the keyword arguments every answer check
    of it received."""
    from openexecutive.orchestrator import executive as executive_mod
    from openexecutive.orchestrator.session import Session

    checks: list[dict[str, Any]] = []

    async def check(text: str, turn: Any, **kw: Any) -> str:
        checks.append(kw)
        return text

    exec_ = executive_mod.Executive()

    async def run() -> None:
        session = Session(session_id=f"t-typed-{entry}")
        if entry == "chat":
            await exec_.chat("Inbound: Updated forecast for Q1", session, peer_memory_context="", **kwargs)
            return
        async for _ in getattr(exec_, entry)(
            "Inbound: Updated forecast for Q1", session, peer_memory_context="", **kwargs
        ):
            pass

    with (
        patch("openexecutive.orchestrator.executive.get_provider", return_value=_provider()),
        patch.object(executive_mod.answer_check, "check_reply", new=check),
        patch("openexecutive.memory.honcho_client.sync_turn", new=lambda *a, **k: None),
        patch("openexecutive.memory.episodic.schedule_extraction", new=lambda *a, **k: None),
        patch("openexecutive.attunement.open_loops.schedule_open_loop_pass", new=lambda *a, **k: None),
    ):
        asyncio.run(run())
    return checks


@pytest.mark.parametrize("entry", ["stream_chat", "stream_chat_with_committee", "chat"])
def test_every_entry_point_hands_the_typed_question_to_the_check(entry: str) -> None:
    checks = _drive_check(entry, memory_text=HEADLINE, typed_question=TYPED)
    assert checks and all(c["typed_question"] == TYPED for c in checks)
    # The turn's question stays the correction model's context.
    assert all(c["question"] == HEADLINE for c in checks)


@pytest.mark.parametrize("entry", ["stream_chat", "stream_chat_with_committee", "chat"])
def test_a_hand_off_headline_alone_is_no_typed_question(entry: str) -> None:
    checks = _drive_check(entry, memory_text=HEADLINE)
    assert checks and all(c["typed_question"] is None for c in checks)


@pytest.mark.parametrize(
    "path", ["orchestrator/executive.py", "api/routes/chat.py"],
)
def test_every_check_reply_call_passes_typed_question(path: str) -> None:
    calls = _calls(PACKAGE / path, {"check_reply", "_checked_stopped_draft"})
    assert calls
    for call in calls:
        assert _kw(call, "typed_question") is not None, f"{path}:{call.lineno}"


# Every call that takes a turn's typed question, at any layer.
ENTRY = {
    "chat", "stream_chat", "stream_chat_with_committee", "start_background_turn", "_run_chat_turn",
    "_upload_turn", "check_reply", "_checked_stopped_draft", "_handle_message", "_process_and_reply",
}
# The only modules that may name typed_question at all: the check itself,
# the Executive, and the surfaces with text the person typed.
ALLOWED = [
    "api/routes/chat.py",
    "evals/runner.py",
    "integrations/discord_bot.py",
    "integrations/erpnext_panel.py",
    "integrations/google_chat.py",
    "integrations/slack_bot.py",
    "integrations/telegram_bot.py",
    "orchestrator/answer_check.py",
    "orchestrator/executive.py",
]


def _names_typed_question(path: Path) -> bool:
    """Whether a module passes typed_question by keyword, or names it as a
    string (a kwargs dict's key or subscript, for a ``**`` splat)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "typed_question":
            return True
        if isinstance(node, ast.Constant) and node.value == "typed_question":
            return True
    return False


def _module(path: Path) -> str:
    return str(path.relative_to(PACKAGE)).replace("\\", "/")


def test_only_the_surfaces_with_typed_text_pass_one() -> None:
    """Email, the MCP server, the scheduler, the CLI, replay and the card
    hand-offs (``start_background_turn``) have no text the person typed:
    they never pass ``typed_question``, so it is None and never relaxes."""
    person_less = [
        "integrations/email_poller.py", "mcp_server/server.py", "scheduler/runner.py", "cli.py",
        "memory/replay.py", "api/routes/alerts.py",
    ]
    for path in person_less:
        calls = _calls(PACKAGE / path, ENTRY)
        assert calls, path
        assert all(_kw(c, "typed_question") is None for c in calls), path
        assert all(not any(k.arg is None for k in c.keywords) for c in calls), f"{path}: a ** splat"
    # Anywhere in the package, by keyword or through a ** splat's key.
    naming = sorted(_module(p) for p in PACKAGE.rglob("*.py") if _names_typed_question(p))
    assert naming == ALLOWED


def test_only_the_executive_and_the_chat_route_give_the_check_a_typed_question() -> None:
    passing = sorted(
        _module(p)
        for p in PACKAGE.rglob("*.py")
        if any(
            _kw(c, "typed_question") is not None or any(k.arg is None for k in c.keywords)
            for c in _calls(p, {"check_reply"})
        )
    )
    assert passing == ["api/routes/chat.py", "orchestrator/executive.py"]


def test_the_panel_passes_the_signed_message_without_its_page_block() -> None:
    [call] = [
        c for c in _calls(PACKAGE / "integrations/erpnext_panel.py", {"chat"}) if _kw(c, "typed_question")
    ]
    typed = _kw(call, "typed_question")
    assert isinstance(typed, ast.Name) and typed.id == "message"


def test_evals_pass_the_raw_query_not_the_page_block() -> None:
    [call] = _calls(PACKAGE / "evals/runner.py", {"chat"})
    assert ast.unparse(_kw(call, "typed_question")) == "scenario['query']"  # type: ignore[arg-type]
    assert ast.unparse(_kw(call, "user_message")) == "query"  # type: ignore[arg-type]


# --------------------------------------------------------------- web chat


def test_chat_without_typed_passes_none(client: TestClient, captured: dict[str, Any]) -> None:  # noqa: F811
    _ = client.post("/chat", json={"message": TYPED}).text
    assert captured["stream_chat"]["typed_question"] is None


@pytest.mark.parametrize(("committee", "entry"), [(False, "stream_chat"), (True, "stream_chat_with_committee")])
def test_a_typed_message_is_the_typed_question(
    client: TestClient, captured: dict[str, Any], committee: bool, entry: str,  # noqa: F811
) -> None:
    _ = client.post("/chat", json={"message": f"  {TYPED} ", "typed": True, "committee_review": committee}).text
    assert captured[entry]["typed_question"] == TYPED


def test_a_hand_off_headline_sent_as_typed_is_ignored(client: TestClient, captured: dict[str, Any]) -> None:  # noqa: F811
    seed = f"Let's discuss this card:\n\n{HEADLINE}"
    _ = client.post("/chat", json={"message": seed, "memory_text": HEADLINE, "typed": True}).text
    assert captured["stream_chat"]["typed_question"] is None


def test_typed_is_dropped_with_a_memory_line_or_a_host_card() -> None:
    assert chat_route._typed_question(TYPED, typed=True, memory_text=None, card_id=None) == TYPED
    assert chat_route._typed_question(TYPED, typed=False, memory_text=None, card_id=None) is None
    assert chat_route._typed_question(HEADLINE, typed=True, memory_text="x", card_id=None) is None
    assert chat_route._typed_question(HEADLINE, typed=True, memory_text=None, card_id=4) is None
    assert chat_route._typed_question("   ", typed=True, memory_text=None, card_id=None) is None


def _fake_files(monkeypatch: pytest.MonkeyPatch) -> None:
    async def output(filename: str, data: bytes, content_type: str, **_kw: Any) -> tuple[str, list[Any]]:
        return f"[{filename}] What if the forecast for Q1 holds?", []

    monkeypatch.setattr(chat_route, "build_attachment_output", output)


@pytest.mark.parametrize(("typed", "expected"), [(None, None), ("true", "totals?")])
def test_an_upload_named_like_a_forecast_never_relaxes(
    client: TestClient, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    typed: str | None, expected: str | None,
) -> None:
    _fake_files(monkeypatch)
    data = {"message": "totals?", **({"typed": typed} if typed else {})}
    _ = client.post(
        "/chat/upload", data=data, files=[("files", ("Q1 forecast.pdf", b"%PDF", "application/pdf"))],
    ).text
    kwargs = captured["stream_chat"]
    assert kwargs["typed_question"] == expected
    assert "Q1 forecast.pdf" in kwargs["memory_text"]


CLIENT_TURN = "ct-typed-stop"


def _stopping_executive(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """An Executive whose turn the person stops after its first words, and
    the answer check the route then runs on what was shown."""
    from openexecutive.orchestrator import answer_check
    from openexecutive.orchestrator import executive as exec_mod

    checks: list[dict[str, Any]] = []

    class _Exec:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_k: Any) -> None:
            pass

        async def stream_chat(self, **_k: Any) -> AsyncIterator[str]:
            yield "Week 1: $41,130."
            # The person's Stop (whoever owns the turn here).
            chat_route._active_stops[CLIENT_TURN].event.set()
            await asyncio.sleep(30)
            yield "never sent"

    async def check(text: str, turn: Any, **kw: Any) -> str:
        checks.append(kw)
        return text

    monkeypatch.setattr(exec_mod, "Executive", _Exec)
    monkeypatch.setattr(answer_check, "check_reply", check)
    chat_route._active_stops.clear()
    return checks


@pytest.mark.parametrize(("typed", "expected"), [(False, None), (True, "totals?")])
def test_a_stopped_upload_turn_checks_with_the_typed_words_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, typed: bool, expected: str | None,  # noqa: F811
) -> None:
    checks = _stopping_executive(monkeypatch)
    _fake_files(monkeypatch)
    data = {"message": "totals?", "client_turn_id": CLIENT_TURN, **({"typed": "true"} if typed else {})}
    body = client.post(
        "/chat/upload", data=data, files=[("files", ("Q1 forecast.pdf", b"%PDF", "application/pdf"))],
    ).text
    assert '"stopped"' in body
    assert [c["typed_question"] for c in checks] == [expected]
    # The model's context still has the turn's message, file text and all.
    assert "forecast for Q1" in checks[0]["question"]


def test_a_stopped_hand_off_turn_never_relaxes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    checks = _stopping_executive(monkeypatch)
    body = client.post(
        "/chat", json={"message": HEADLINE, "memory_text": HEADLINE, "typed": True, "client_turn_id": CLIENT_TURN},
    ).text
    assert '"stopped"' in body
    assert [c["typed_question"] for c in checks] == [None]


# --------------------------------------------------------------- Telegram


def _tg(**extra: Any) -> dict[str, Any]:
    return {"message_id": 7, "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, **extra}


def test_telegram_typed_text() -> None:
    from openexecutive.integrations.telegram_bot import _typed_text

    assert _typed_text(_tg(text=TYPED)) == TYPED
    assert _typed_text(_tg(text=f"/ask {TYPED}")) == TYPED
    assert _typed_text(_tg(caption="totals?", document={"file_id": "f"})) == "totals?"
    # A file's text and a voice note's words are never the typed question.
    assert _typed_text(_tg(document={"file_id": "f", "file_name": "Q1 forecast.pdf"})) is None
    assert _typed_text(_tg(voice={"file_id": "v"})) is None
    # A forward (current and older fields), an automatic forward, an inline
    # bot's message and /start's link payload are not typed here.
    for extra in (
        {"forward_origin": {"type": "user"}}, {"forward_from": {"id": 9}}, {"forward_from_chat": {"id": -1}},
        {"forward_sender_name": "Someone"}, {"forward_date": 1700000000},
        {"is_automatic_forward": True}, {"via_bot": {"id": 5}},
    ):
        assert _typed_text(_tg(text=HEADLINE, **extra)) is None, extra
    assert _typed_text(_tg(text="/start forecast_q1")) is None
    assert _typed_text(_tg(text="/start@ExecBot forecast")) is None


def test_telegram_webhook_and_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from fastapi import FastAPI

    from openexecutive.integrations import telegram_bot

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAH" + "x" * 32)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    process = AsyncMock()
    app = FastAPI()
    app.include_router(telegram_bot.router)
    with (
        patch("openexecutive.people.store.find_person_by_telegram_chat_id", return_value=object()),
        patch.object(telegram_bot, "_process_and_reply", new=process),
    ):
        post = TestClient(app).post
        assert post("/webhook/telegram", json={"message": _tg(text=TYPED)}).status_code == 200
        assert process.await_args.kwargs["typed_question"] == TYPED
        forwarded = _tg(text=HEADLINE, forward_origin={"type": "hidden_user", "sender_user_name": "x"})
        assert post("/webhook/telegram", json={"message": forwarded}).status_code == 200
        assert process.await_args.kwargs["typed_question"] is None
        # A held message replayed later has no typed question.
        held = SimpleNamespace(payload={"chat_id": 42, "message_text": TYPED, "sender_name": "Dana"})
        assert asyncio.run(telegram_bot._replay_held(held, None)) is True
        assert "typed_question" not in process.await_args.kwargs


# --------------------------------------------------------------- Discord


def test_discord_typed_text() -> None:
    import discord

    from openexecutive.integrations.discord_bot import _typed_text

    # discord.py's own references: a reply's type is named "default".
    reply = discord.MessageReference(message_id=6, channel_id=1, type=discord.MessageReferenceType.reply)
    forward = discord.MessageReference(message_id=6, channel_id=1, type=discord.MessageReferenceType.forward)
    assert _typed_text(SimpleNamespace(content=f"<@123> {TYPED}", reference=None)) == TYPED
    assert _typed_text(SimpleNamespace(content="totals?", reference=reply)) == "totals?"
    assert _typed_text(SimpleNamespace(content=HEADLINE, reference=forward)) is None
    assert _typed_text(SimpleNamespace(content="", reference=forward, message_snapshots=[object()])) is None
    assert _typed_text(SimpleNamespace(content="", reference=None, attachments=[object()])) is None


def test_discord_passes_typed_text_and_the_slash_prompt_and_replays_none() -> None:
    calls = _calls(PACKAGE / "integrations/discord_bot.py", {"_handle_message"})
    passed = sorted(ast.unparse(v) for c in calls if (v := _kw(c, "typed_question")) is not None)
    assert passed == ["_typed_text(message)", "cleaned"]
    # The held-message replay passes none.
    assert sum(1 for c in calls if _kw(c, "typed_question") is None) == 1


# --------------------------------------------------------------- Slack


def test_slack_typed_text() -> None:
    from openexecutive.integrations.slack_bot import _typed_text

    assert _typed_text({"text": f"<@UBOT> {TYPED}"}) == TYPED
    # A file-only message (its note stands in for words) and a voice note.
    assert _typed_text({"text": "", "files": [{"name": "Q1 forecast.pdf"}]}) is None
    assert _typed_text({"text": "", "files": [{"name": "audio.m4a", "subtype": "slack_audio"}]}) is None
    # A shared message keeps its comment; its original is in attachments.
    shared = {"text": "totals?", "attachments": [{"is_share": True, "text": HEADLINE}]}
    assert _typed_text(shared) == "totals?"


# --------------------------------------------------------------- Google Chat


def test_google_chat_typed_text() -> None:
    from openexecutive.integrations.google_chat import _typed_text

    assert _typed_text({"text": f"<users/123> {TYPED}"}) == TYPED
    assert _typed_text({"text": "", "attachment": [{"contentName": "Q1 forecast.pdf"}]}) is None
    reply = {"quoteType": "REPLY", "quotedMessageSnapshot": {"text": HEADLINE}}
    assert _typed_text({"text": "totals?", "quotedMessageMetadata": reply}) == "totals?"
    forward = {"quoteType": "FORWARD", "quotedMessageSnapshot": {"text": HEADLINE}}
    assert _typed_text({"text": HEADLINE, "quotedMessageMetadata": forward}) is None


def test_google_chat_webhook_passes_the_typed_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from fastapi import FastAPI

    from openexecutive.integrations import google_chat

    monkeypatch.setattr(google_chat, "verify_google_jwt", lambda token, project: {})
    settings = SimpleNamespace(
        google_chat_project_number="123",
        google_chat_service_account_file="/fake/key.json",
        google_chat_service_account_email=None,
    )
    process = AsyncMock()
    app = FastAPI()
    app.include_router(google_chat.router)

    def body(message: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "MESSAGE",
            "space": {"name": "spaces/D", "type": "DM"},
            "user": {"displayName": "Dana", "email": "dana@example.com"},
            "message": {"name": "spaces/D/messages/1", "thread": {"name": "spaces/D/threads/T"}, **message},
        }

    with (
        patch.object(google_chat, "_process_and_reply", new=process),
        patch("openexecutive.integrations.google_chat.get_settings", return_value=settings),
    ):
        post = TestClient(app).post
        headers = {"Authorization": "Bearer x"}
        assert post("/webhook/google-chat", json=body({"text": TYPED}), headers=headers).status_code == 200
        assert process.await_args.kwargs["typed_question"] == TYPED
        file_only = body({"text": "", "attachment": [{"contentName": "Q1 forecast.pdf", "name": "a/1"}]})
        assert post("/webhook/google-chat", json=file_only, headers=headers).status_code == 200
        assert process.await_args.kwargs["message_text"].startswith("(Attached files:")
        assert process.await_args.kwargs["typed_question"] is None
