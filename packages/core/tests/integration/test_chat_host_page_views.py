"""ERPNext's show tool on a host page's turn and the `erpnext_views` event
(F1, oe_connector's docs/panel-f1-plan.md, OpenExecutive PR 2)."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api import host_limit
from openexecutive.api.chat_events import EVENT_TYPES, ChatStreamEventSchema
from openexecutive.api.routes import chat as chat_route
from openexecutive.integrations import erpnext_drafts
from openexecutive.integrations.erpnext_panel import note_erpnext_call
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator import mcp_gateway
from openexecutive.orchestrator.erpnext_caller import ASSERTION_ARG
from openexecutive.orchestrator.schedule_tools import current_session, host_allows_erpnext_read
from openexecutive.prompts.connected_systems import ERPNEXT_SHOW_TOOL

HOST = {"x-caller-surface": "host"}
PAGE = {"doctype": "Sales Order"}
VIEW = {"kind": "list", "doctype": "Sales Order", "sort": {"field": "base_grand_total", "order": "desc"}}


@pytest.fixture(autouse=True)
def _reset_route_state() -> Iterator[None]:
    chat_route._sessions.clear()
    chat_route._host_sessions.clear()
    host_limit.reset()
    yield
    host_limit.reset()


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    return db_path


@pytest.fixture()
def shows(monkeypatch: pytest.MonkeyPatch, temp_db: Path) -> list[dict[str, Any]]:
    """Each turn's script: what the stand-in Executive calls through the
    gateway's note (as a real ERPNext call would), and what it saw."""
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
    script: list[dict[str, Any]] = []

    class _Executive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, *, session: Any, page_context_block: str = "", **_kwargs: Any) -> AsyncIterator[str]:
            step = script.pop(0) if script else {}
            step["seen"] = {
                "show_allowed": host_allows_erpnext_read(ERPNEXT_SHOW_TOOL, session),
                "show_refused": mcp_gateway._show_outside_panel(ERPNEXT_SHOW_TOOL),
                "block": page_context_block,
            }
            for name, arguments, failed in step.get("calls", []):
                note_erpnext_call(name, arguments, failed=failed)
            yield "ok"

    monkeypatch.setattr(exec_mod, "Executive", _Executive)
    return script


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


def _events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def _show(view: dict[str, Any], *, failed: bool = False) -> tuple[str, dict[str, Any], bool]:
    return (ERPNEXT_SHOW_TOOL, {"view": view, ASSERTION_ARG: "signed.jwt"}, failed)


def test_the_event_is_in_the_stream_schema() -> None:
    assert "erpnext_views" in EVENT_TYPES
    assert EVENT_TYPES.index("erpnext_views") < EVENT_TYPES.index("done")
    parsed = ChatStreamEventSchema.model_validate({"type": "erpnext_views", "views": [VIEW], "call": None})
    assert parsed.root.type == "erpnext_views"


def test_a_host_page_turn_gets_the_show_tool_and_its_views_before_done(shows: list[dict[str, Any]]) -> None:
    step: dict[str, Any] = {
        "calls": [
            ("erpnext__list_records", {"doctype": "Sales Order", ASSERTION_ARG: "signed.jwt"}, False),
            _show(VIEW),
            _show({"kind": "report", "report": "Refused"}, failed=True),
        ]
    }
    shows.append(step)
    events = _events(_client().post("/chat", json={"message": "largest?", "erpnext_page": PAGE}, headers=HOST).text)
    assert step["seen"]["show_allowed"] is True and step["seen"]["show_refused"] is False
    assert "erpnext__show_in_erpnext" in step["seen"]["block"]
    types = [e["type"] for e in events]
    assert types.index("erpnext_views") < types.index("done")
    views = next(e for e in events if e["type"] == "erpnext_views")
    assert views["views"] == [VIEW]
    assert views["call"] == {"tool": "list_records", "arguments": {"doctype": "Sales Order"}}
    assert ASSERTION_ARG not in json.dumps(views)


def test_a_host_turn_without_the_page_has_no_show_tool_and_no_event(shows: list[dict[str, Any]]) -> None:
    step: dict[str, Any] = {}
    shows.append(step)
    events = _events(_client().post("/chat", json={"message": "hi"}, headers=HOST).text)
    assert step["seen"]["show_allowed"] is False and step["seen"]["show_refused"] is True
    assert "erpnext_views" not in [e["type"] for e in events]


def test_the_next_turn_does_not_repeat_the_last_turns_views(shows: list[dict[str, Any]]) -> None:
    client = _client()
    shows.extend([{"calls": [_show(VIEW)]}, {}])
    first = _events(client.post("/chat", json={"message": "a", "erpnext_page": PAGE}, headers=HOST).text)
    session_id = next(e["session_id"] for e in first if e["type"] == "done")
    second = _events(
        client.post("/chat", json={"message": "b", "session_id": session_id, "erpnext_page": PAGE}, headers=HOST).text
    )
    views = next(e for e in second if e["type"] == "erpnext_views")
    assert views["views"] == [] and views["call"] is None


def test_a_shared_chat_never_gets_the_event(shows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    first = _events(client.post("/chat", json={"message": "a"}, headers=HOST).text)
    session_id = next(e["session_id"] for e in first if e["type"] == "done")
    monkeypatch.setattr(chat_route, "chat_people", lambda *_a, **_k: [(1, "Ada", True), (2, "Bo", False)])
    shows.extend([{}, {"calls": [_show(VIEW)]}])
    events = _events(
        client.post("/chat", json={"message": "b", "session_id": session_id, "erpnext_page": PAGE}, headers=HOST).text
    )
    assert "erpnext_views" not in [e["type"] for e in events]


def test_the_show_tool_stays_off_the_persons_own_page() -> None:
    own = SimpleNamespace(from_host=False, host_page=True)
    token = current_session.set(own)
    try:
        assert host_allows_erpnext_read(ERPNEXT_SHOW_TOOL, own) is False
        assert mcp_gateway._show_outside_panel(ERPNEXT_SHOW_TOOL) is True
    finally:
        current_session.reset(token)


def test_a_host_turn_is_refused_a_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erpnext_drafts, "drafts_on", lambda: True)
    host = SimpleNamespace(from_host=True, host_page=True, unattended=False)
    refusal = erpnext_drafts.offer_refusal(host)
    assert refusal is not None and "another app's page" in refusal
    assert erpnext_drafts.offered(host) is False
