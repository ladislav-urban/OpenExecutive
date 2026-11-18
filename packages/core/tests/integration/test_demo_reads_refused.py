"""Reads a demo person must not make (api.demo_gate.refuse_demo_reader): the
team's goals, the practice's clients, past advice, the workflow designer's
definitions, and (principal-only already) the eval runs. A demo registrant
gets 403 before any work; the principal and a teammate read on as before."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .principal_gate_support import app_client, call, make_roster

READS = [
    "/goals",
    "/clients/cockpit",
    "/memories/advice",
    "/workflows/custom",
    "/workflows/custom/some_flow",
]


@pytest.fixture
def reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The roster, and what each read would compute stubbed to record it ran."""
    make_roster(tmp_path, monkeypatch)
    from openexecutive.api.routes import episodic as episodic_route
    from openexecutive.api.routes import workflows as wf_route
    from openexecutive.clients import cockpit
    from openexecutive.departments import goals

    ran: list[str] = []

    def _rec(name: str, result: Any) -> Any:
        def _f(*_a: Any, **_kw: Any) -> Any:
            ran.append(name)
            return result

        return _f

    monkeypatch.setattr(goals, "list_goal_views", _rec("goals", []))
    monkeypatch.setattr(cockpit, "practice_overview", _rec("cockpit", []))
    monkeypatch.setattr(episodic_route, "list_advice", _rec("advice", []))
    monkeypatch.setattr(wf_route, "list_definitions", _rec("custom", []))
    monkeypatch.setattr(wf_route, "get_definition", _rec("custom_item", None))
    return ran


@pytest.mark.parametrize("signed", [False, True])
def test_a_demo_registrant_is_refused(
    reads: list[str], monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    from openexecutive.api.demo_gate import DEMO_REFUSED

    client = app_client(monkeypatch, signed=signed)
    for path in [*READS, "/evals/runs", "/evals/runs/some-run"]:
        r = call(client, signed, "demo", "GET", path)
        assert r.status_code == 403, (path, r.text)
        assert r.json()["detail"] in (DEMO_REFUSED, "Only the principal can use evals.")
    assert reads == []


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("who", ["principal", "teammate"])
def test_the_principal_and_a_teammate_read_on(
    reads: list[str], monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    client = app_client(monkeypatch, signed=signed)
    for path in READS:
        r = call(client, signed, who, "GET", path)
        # The item is unknown here: 404, past the gate.
        expected = 404 if path.endswith("some_flow") else 200
        assert r.status_code == expected, (path, r.text)
    assert reads == ["goals", "cockpit", "advice", "custom", "custom_item"]


def test_a_roster_that_cannot_be_read_refuses(
    reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.people import store as people_store

    client = app_client(monkeypatch, signed=False)

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(people_store, "find_person_by_email", _boom)
    r = call(client, False, "teammate", "GET", "/memories/advice")
    assert r.status_code == 503
    assert reads == []
