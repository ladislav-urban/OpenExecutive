"""The chat route and the answer check (orchestrator.answer_check): the checked
reply replaces what streamed, on the stream and in what is saved, and a reply
cut off by a timeout is checked before it is saved."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator import answer_check, answer_review

RESULT = {
    "report_name": "Accounts Receivable Summary",
    "columns": [{"fieldname": "outstanding", "fieldtype": "Currency"}],
    "rows": [{"outstanding": 12500.0}, {"outstanding": 35700.0}],
    "total_row": {"outstanding": 48200.0},
    "truncated": False,
}


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    return db_path


@pytest.fixture()
def patched_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder
    from openexecutive.utils import session_title

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda **_k: "")

    async def _no_title(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_title, "generate_session_title", _no_title)


def _events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def _saved_reply(events: list[dict[str, Any]], db: Path) -> str:
    session_id = next(e["session_id"] for e in events if e.get("type") == "done")
    (reply,) = [m for m in session_store.load_messages(session_id, db_path=db) if m["role"] == "assistant"]
    return str(reply["content"])


def _executive(monkeypatch: pytest.MonkeyPatch, stream: Any) -> None:
    from openexecutive.orchestrator import executive as exec_mod

    class _Fake:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        stream_chat = stream

    monkeypatch.setattr(exec_mod, "Executive", _Fake)


def _read_erpnext() -> None:
    # What the gateway does after an ERPNext read, inside the route's turn.
    answer_check.note_erpnext_result(
        "erpnext__run_report", {"report_name": "Accounts Receivable Summary"}, json.dumps(RESULT), failed=False,
    )


def test_the_checked_reply_replaces_what_streamed_and_is_saved(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stream(self: Any, *, session: Any, **_kw: Any) -> AsyncIterator[Any]:
        yield "North owes $12,900."
        yield {"type": "reply_replaced", "content": "North owes $12,500.", "session_id": session.session_id}

    _executive(monkeypatch, stream)
    app = FastAPI()
    app.include_router(chat_route.router)
    events = _events(TestClient(app).post("/chat", json={"message": "Who owes us?"}).text)
    kinds = [e["type"] for e in events]
    assert kinds.index("reply_replaced") < kinds.index("done")
    assert next(e for e in events if e["type"] == "reply_replaced")["content"] == "North owes $12,500."
    assert _saved_reply(events, temp_db) == "North owes $12,500."


def test_a_timed_out_reply_is_checked_before_it_is_saved(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_STREAM_TIMEOUT_S", "1")

    async def no_context(_turn: Any) -> dict[str, Any]:
        return {}

    async def cannot_tell(**kw: Any) -> list[Any]:
        return [answer_review.CANNOT_TELL] * len(kw["flagged"])

    async def no_rewrite(*_a: Any) -> None:
        return None

    monkeypatch.setattr(answer_check, "_context", no_context)
    monkeypatch.setattr(answer_review, "review", cannot_tell)
    monkeypatch.setattr(answer_check, "_rewrite", no_rewrite)

    async def stream(self: Any, **_kw: Any) -> AsyncIterator[Any]:
        _read_erpnext()
        yield "Receivables are $48,200. North owes $9,999 of it."
        await asyncio.sleep(30)
        yield "never"

    _executive(monkeypatch, stream)
    app = FastAPI()
    app.include_router(chat_route.router)
    events = _events(TestClient(app).post("/chat", json={"message": "Who owes us?"}).text)
    replaced = [e for e in events if e["type"] == "reply_replaced"]
    assert [e["content"] for e in replaced] == ["Receivables are $48,200."]
    assert any(e["type"] == "error" for e in events)  # still a timeout
    assert _saved_reply(events, temp_db) == "Receivables are $48,200."
