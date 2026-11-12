"""A demo person's turn reads and answers only (the owner, Nov 8, 2026, on
SenteLabsAI/OpenExecutive#706): anything else it asks for is refused outright,
never put on an Approve card, so a stranger trying the demo can't fill the
principal's queue or word a request for them to approve. The gateway refuses
it too, not only the dispatch."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.orchestrator import department_gate, levels
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people.demo import DEMO_REFUSAL
from tests.unit.test_mcp_gateway_google_owner import OWN, _call, _gateway, _ran


@pytest.fixture
def demo_turn() -> Iterator[Session]:
    session = Session(from_web_chat=True, web_caller_signed_in=True, caller_person_id=7, demo_caller=True)
    token = current_session.set(session)
    yield session
    current_session.reset(token)


@pytest.fixture
def no_card(monkeypatch: pytest.MonkeyPatch) -> None:
    def card(*_a: object, **_kw: object) -> int:
        raise AssertionError("a demo turn left an Approve card")

    monkeypatch.setattr(department_gate, "hold", card)


@pytest.mark.usefixtures("demo_turn", "no_card")
def test_the_gateway_refuses_a_demo_turns_write_at_every_level() -> None:
    for relaxed in (False, True):
        gateway, session_call = _gateway({"d1": OWN})
        with patch.object(levels, "relaxed", return_value=relaxed):
            out = json.loads(_call(gateway, "modify_doc_text", {"document_id": "d1", "text": "Q3"}))
        assert DEMO_REFUSAL in out["error"], relaxed
        assert not _ran(session_call, "modify_doc_text")


@pytest.mark.usefixtures("demo_turn", "no_card")
def test_the_gateway_still_lets_a_demo_turn_read() -> None:
    gateway, session_call = _gateway({"d1": OWN})
    out = json.loads(_call(gateway, "search_drive_files", {"query": "budget"}))
    assert "error" not in out and _ran(session_call, "search_drive_files")


def test_a_verified_teammates_write_is_not_refused_as_a_demo() -> None:
    session = Session(from_web_chat=True, web_caller_signed_in=True, caller_person_id=8)
    token = current_session.set(session)
    try:
        gateway, _ = _gateway({"d1": OWN})
        out = _call(gateway, "modify_doc_text", {"document_id": "d1", "text": "Q3"})
        assert DEMO_REFUSAL not in out
    finally:
        current_session.reset(token)


@pytest.mark.usefixtures("no_card")
def test_a_demo_turns_held_step_is_refused_not_carded(demo_turn: Session) -> None:
    out = json.loads(levels.hold("message_person", {"text": "hi"}, mcp=False, reason=levels.NOT_OWNER, session=demo_turn))
    assert DEMO_REFUSAL in out["error"] and "status" not in out
    assert DEMO_REFUSAL in json.loads(levels.not_carriable_error("run_script", levels.OUTSIDE))["error"]
    blocked = json.dumps({"error": "blocked"})
    assert DEMO_REFUSAL in json.loads(levels.card_or(blocked, "send_gmail_message", {}, reason=levels.NEW_PERSON))["error"]


@pytest.mark.usefixtures("demo_turn", "no_card")
def test_a_demo_persons_department_step_is_refused_not_carded(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = AsyncMock(return_value="sent")
    monkeypatch.setattr(
        department_gate, "decide",
        lambda *_a, **_kw: department_gate.Verdict(run=False, approver_person_id=1, reason="outside its range"),
    )
    person = SimpleNamespace(id=7, demo=True)
    out = asyncio.run(department_gate._gated(person, "message_person", inner, {"text": "hi"}, mcp=False, session_id="s"))  # type: ignore[arg-type]
    assert DEMO_REFUSAL in json.loads(out)["error"]
    inner.assert_not_awaited()


def test_a_teammates_held_step_still_goes_on_a_card(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session(from_web_chat=True, web_caller_signed_in=True, caller_person_id=8)
    monkeypatch.setattr(department_gate, "hold", lambda *_a, **_kw: 42)
    out = json.loads(levels.hold("message_person", {"text": "hi"}, mcp=False, reason=levels.NOT_OWNER, session=session))
    assert out["status"] == "waiting_for_approval" and out["decision_instance_id"] == 42
