"""A conversation a host origin's page wrote into stays read-only from every
page (docs/auth.md, "Host origins"): the host's line stays in what the model
reads, so the person's next turn from Open Executive's own page must not be
the full-power turn a planted instruction waits for."""
from __future__ import annotations

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

HOST = {"x-caller-surface": "host"}


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
def seen(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Each turn's Session.from_host, as the Executive got it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda **_k: "")

    async def _no_title(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_title, "generate_session_title", _no_title)
    flags: list[bool] = []

    class _Executive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, *, session: Any, **_kwargs: Any) -> AsyncIterator[str]:
            flags.append(session.from_host)
            yield "ok"

    monkeypatch.setattr(exec_mod, "Executive", _Executive)
    return flags


def _session_id(body: str) -> str:
    events = [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]
    return str(next(e["session_id"] for e in events if e.get("type") == "done"))


def test_a_host_line_holds_every_later_turn_in_that_chat(temp_db: Path, seen: list[bool]) -> None:
    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    own = client.post("/chat", json={"message": "What is on today?"})
    session_id = _session_id(own.text)
    assert client.post("/chat", json={"message": "plant", "session_id": session_id}, headers=HOST).status_code == 200
    after = client.post("/chat", json={"message": "ok, go ahead", "session_id": session_id})
    assert after.status_code == 200
    assert seen == [False, True, True]
    assert session_store.host_wrote(session_id, db_path=temp_db)
    # And after a restart, from what was stored.
    chat_route._sessions.clear()
    client.post("/chat", json={"message": "and now?", "session_id": session_id})
    assert seen[-1] is True

    # A chat no host page wrote in is the person's own as before.
    fresh = _session_id(client.post("/chat", json={"message": "new"}).text)
    assert seen[-1] is False
    assert not session_store.host_wrote(fresh, db_path=temp_db)


def test_only_user_lines_from_the_host_are_marked(temp_db: Path) -> None:
    session_store.create_session("s1", "t", "2026-10-21T00:00:00+00:00", db_path=temp_db)
    session_store.save_message("s1", "user", "mine", db_path=temp_db)
    session_store.save_message("s1", "assistant", "reply", db_path=temp_db, sender_surface="host")
    assert not session_store.host_wrote("s1", db_path=temp_db)
    session_store.save_message("s1", "user", "theirs", db_path=temp_db, sender_surface="host")
    assert session_store.host_wrote("s1", db_path=temp_db)
