"""What the Executive sends into a chat app on its own is part of the chat.

A brief, the evening digest or a workflow's report used to go out without
being saved, so the person's reply ("can you fix it?") reached a turn with
nothing before it, and after a quiet stretch it even opened a new
conversation. ``record_delivery`` saves it where the reply will land.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from openexecutive.integrations.conversations import record_delivery
from openexecutive.memory import conversations, session_store
from openexecutive.memory.episodic import _get_conn, initialize_db
from openexecutive.orchestrator import schedule_tools
from openexecutive.orchestrator.session import CONTINUED_PLACEHOLDER, Session
from openexecutive.people.models import Person
from openexecutive.scheduler import runner

OWNER = 7
BASE = "telegram:4242"
DIGEST = "Done for you\n\n- Your Monday prep came back empty."


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    initialize_db(path)
    return path


def _turn(db: Path, sid: str, at: datetime) -> None:
    session_store.create_session(sid, "Telegram Dana", at.isoformat(), caller_person_id=OWNER, db_path=db)
    with _get_conn(db) as conn:
        for role, text in (("user", "Q"), ("assistant", "A")):
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (sid, role, text, at.isoformat()),
            )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (at.isoformat(), sid))


def _roles(db: Path, sid: str) -> list[tuple[str, str]]:
    return [(m["role"], m["content"]) for m in session_store.load_messages(sid, db_path=db)]


def test_a_delivery_joins_the_open_conversation(db: Path) -> None:
    _turn(db, BASE, datetime.now(UTC) - timedelta(minutes=30))
    sid = record_delivery(BASE, DIGEST, owner_person_id=OWNER, title="Evening digest", db_path=db)
    assert sid == BASE
    assert _roles(db, BASE)[-1] == ("assistant", DIGEST)
    # A reply now lands in the same conversation, after the digest.
    assert conversations.current_conversation(BASE, db_path=db) == BASE


def test_after_a_quiet_stretch_the_delivery_opens_the_conversation_the_reply_joins(db: Path) -> None:
    _turn(db, BASE, datetime.now(UTC) - timedelta(hours=5))
    sid = record_delivery(BASE, DIGEST, owner_person_id=OWNER, title="Evening digest", db_path=db)
    assert sid == "telegram:4242~2"
    assert _roles(db, sid) == [("assistant", DIGEST)]
    meta = session_store.get_session_metadata(sid, db_path=db)
    assert meta is not None and meta["title"] == "Evening digest"
    # An hour later the reply is still within the gap of the digest.
    later = datetime.now(UTC) + timedelta(hours=1)
    assert conversations.current_conversation(BASE, now=later, db_path=db) == sid


def test_the_reply_sees_a_conversation_that_opens_with_the_delivery() -> None:
    session = Session(session_id="telegram:4242~2")
    session.conversation_history = [
        {"role": "assistant", "content": DIGEST},
        {"role": "user", "content": "Can you fix it?"},
        {"role": "assistant", "content": "Yes."},
    ]
    history = session.get_recent_history()
    assert history[0] == {"role": "user", "content": CONTINUED_PLACEHOLDER}
    assert history[1]["content"] == DIGEST


def test_a_web_chat_still_drops_a_leading_reply() -> None:
    session = Session(session_id="4f1c2a")
    session.conversation_history = [
        {"role": "assistant", "content": "orphan"},
        {"role": "user", "content": "Hi"},
    ]
    assert session.get_recent_history() == [{"role": "user", "content": "Hi"}]


def test_only_chat_app_streams_and_real_text_are_saved(db: Path) -> None:
    assert record_delivery("4f1c2a", DIGEST, owner_person_id=OWNER, title="x", db_path=db) is None
    assert record_delivery(BASE, "  ", owner_person_id=OWNER, title="x", db_path=db) is None


def _principal() -> Person:
    return Person(id=OWNER, full_name="Dana", is_principal=True, telegram_chat_id="4242")


def _run_send(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> list[tuple[Any, ...]]:
    saved: list[tuple[Any, ...]] = []

    async def fake_send(_input: dict[str, Any]) -> str:
        return json.dumps(result)

    def fake_record(base_id: str, text: str, **kw: Any) -> str:
        saved.append((base_id, text, kw["owner_person_id"], kw["title"]))
        return base_id

    monkeypatch.setattr(schedule_tools, "handle_send_telegram_message", fake_send)
    monkeypatch.setattr("openexecutive.integrations.conversations.record_delivery", fake_record)
    delivery = asyncio.run(runner._send_on_plan(_principal(), ["telegram"], DIGEST, label="Evening digest"))
    assert delivery.ok
    return saved


def test_a_brief_sent_to_telegram_is_saved_into_the_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = _run_send(monkeypatch, {"status": "sent", "chat_id": 4242})
    assert saved == [(BASE, DIGEST, OWNER, "Evening digest")]


def test_a_held_message_is_saved_only_when_it_goes_out(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_send(monkeypatch, {"status": "held"}) == []


def test_a_held_message_is_saved_when_its_release_sends_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # people.held_messages releases through deliver_to_person, which sends on
    # the same plan, so the released block is saved into the chat then.
    saved: list[tuple[Any, ...]] = []

    async def fake_send(_input: dict[str, Any]) -> str:
        return json.dumps({"status": "sent", "chat_id": 4242})

    async def no_email() -> bool:
        return False

    def fake_record(base_id: str, text: str, **kw: Any) -> str:
        saved.append((base_id, text, kw["title"]))
        return base_id

    monkeypatch.setattr(schedule_tools, "handle_send_telegram_message", fake_send)
    monkeypatch.setattr(runner, "ensure_email_ready", no_email)
    monkeypatch.setattr("openexecutive.integrations.conversations.record_delivery", fake_record)
    from openexecutive.people.held_messages import HEADING

    delivery = asyncio.run(runner.deliver_to_person(_principal(), "While you were away: ...", label=HEADING))
    assert delivery.ok and delivery.channel == "telegram"
    assert saved == [(BASE, "While you were away: ...", HEADING)]


@pytest.mark.parametrize("erp", [True, False])
def test_erpnext_figures_sent_on_a_schedule_mark_the_chat(monkeypatch: pytest.MonkeyPatch, erp: bool) -> None:
    # The brief's ERP numbers or a scheduled metric: nothing in the chat
    # called ERPNext, so the delivery itself marks it (audit.erpnext_reads).
    from openexecutive.audit import erpnext_reads

    marked: list[tuple[str, set[str]]] = []
    monkeypatch.setattr(erpnext_reads, "mark_carried", lambda sid, src: marked.append((sid, src)) or True)
    token = erpnext_reads.DELIVERING_ERPNEXT.set(erp)
    try:
        saved = _run_send(monkeypatch, {"status": "sent", "chat_id": 4242})
    finally:
        erpnext_reads.DELIVERING_ERPNEXT.reset(token)
    assert saved == [(BASE, DIGEST, OWNER, "Evening digest")]
    assert marked == ([(BASE, {"scheduled"})] if erp else [])


def test_a_scheduled_metric_runs_with_the_mark_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from openexecutive.audit.erpnext_reads import DELIVERING_ERPNEXT
    from openexecutive.briefing import erp_metrics
    from openexecutive.memory import episodic

    path = tmp_path / "metric.db"
    initialize_db(path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    seen: list[bool] = []

    async def fake_run(action: Any, now: Any, deliver: Any) -> None:
        seen.append(DELIVERING_ERPNEXT.get())

    monkeypatch.setattr(erp_metrics, "run", fake_run)
    action_id = episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="__internal__", channel_ref="metric",
        intent_text="metric", kind="erpnext_metric",
    )
    action = episodic.get_scheduled_action(action_id)
    assert action is not None
    asyncio.run(runner._execute_action(action, None))
    assert seen == [True] and DELIVERING_ERPNEXT.get() is False
