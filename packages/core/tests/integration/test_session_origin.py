"""Where a conversation was asked (F1, OpenExecutive PR 3): a host page's
first turn records `origin` and `origin_ref` once, from the checked page, and
`GET /sessions` shows them."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api import host_limit
from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes import sessions as sessions_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile

HOST = {"x-caller-surface": "host"}


@pytest.fixture(autouse=True)
def _reset_route_state() -> Iterator[None]:
    chat_route._sessions.clear()
    chat_route._host_sessions.clear()
    host_limit.reset()
    yield
    host_limit.reset()


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ERPNEXT_HOST_PAGE_TURNS", "true")
    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda **_k: "")

    async def _no_title(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_title, "generate_session_title", _no_title)

    class _Executive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "ok"

    monkeypatch.setattr(exec_mod, "Executive", _Executive)
    # One rostered caller, so GET /sessions lists their conversations.
    from openexecutive.people import demo as people_demo

    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _request: 7)
    monkeypatch.setattr(sessions_route, "_resolve_caller_person_id", lambda _request: 7)
    monkeypatch.setattr(people_demo, "demo_state", lambda _pid: False)
    return db_path


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_route.router)
    app.include_router(sessions_route.router)
    return TestClient(app)


def _session_id(body: str) -> str:
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    return str(next(e["session_id"] for e in events if e.get("type") == "done"))


def _listed(client: TestClient, session_id: str) -> dict[str, Any]:
    return next(s for s in client.get("/sessions").json() if s["session_id"] == session_id)


@pytest.mark.parametrize(
    ("page", "ref"),
    [
        ({"doctype": "Sales Order", "name": "SO-0001"}, {"doctype": "Sales Order", "name": "SO-0001"}),
        ({"report": "General Ledger", "filters": [["company", "=", "Acme"]]}, {"report": "General Ledger"}),
        ({"doctype": "Customer", "sort": "modified desc"}, {"doctype": "Customer"}),
        ({"screen": "/"}, None),
    ],
)
def test_a_panel_conversation_records_where_it_was_asked(db: Path, page: dict[str, Any], ref: Any) -> None:
    client = _client()
    session_id = _session_id(client.post("/chat", json={"message": "hi", "erpnext_page": page}, headers=HOST).text)
    item = _listed(client, session_id)
    assert item["origin"] == "erpnext_panel"
    assert item.get("origin_ref") == ref
    assert client.get(f"/sessions/{session_id}").json().get("origin") == "erpnext_panel"


def test_it_is_set_once_as_the_conversation_starts(db: Path) -> None:
    client = _client()
    first = {"doctype": "Item", "name": "Bolt"}
    session_id = _session_id(client.post("/chat", json={"message": "a", "erpnext_page": first}, headers=HOST).text)
    client.post(
        "/chat",
        json={"message": "b", "session_id": session_id, "erpnext_page": {"report": "Stock Ledger"}},
        headers=HOST,
    )
    assert _listed(client, session_id)["origin_ref"] == first
    # And a conversation started without the page never gains one later.
    plain = _session_id(client.post("/chat", json={"message": "c"}, headers=HOST).text)
    client.post("/chat", json={"message": "d", "session_id": plain, "erpnext_page": first}, headers=HOST)
    assert "origin" not in _listed(client, plain)


def test_no_origin_without_the_switch_or_from_the_persons_own_page(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client()
    own = _session_id(client.post("/chat", json={"message": "mine"}).text)
    monkeypatch.setenv("ERPNEXT_HOST_PAGE_TURNS", "false")
    off = _session_id(client.post("/chat", json={"message": "x", "erpnext_page": {"doctype": "Item"}}, headers=HOST).text)
    assert "origin" not in _listed(client, own)
    assert "origin" not in _listed(client, off)


def test_a_stored_ref_of_another_shape_is_never_shown(db: Path) -> None:
    session_store.create_session("odd", "t", "now", db_path=db)
    with episodic._get_conn(db) as conn:
        conn.execute(
            "UPDATE sessions SET origin = 'erpnext_panel', origin_ref = ? WHERE session_id = 'odd'",
            (json.dumps({"doctype": "Item", "filters": "x"}),),
        )
    meta = session_store.get_session_metadata("odd", db_path=db)
    assert meta is not None and meta["origin"] == "erpnext_panel" and "origin_ref" not in meta
    with pytest.raises(ValueError):
        session_store.set_origin("odd", "somewhere_else", None, db_path=db)
