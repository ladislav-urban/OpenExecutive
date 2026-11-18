"""With no principal on the roster, the member reads (GET /departments, /goals,
/memories/decisions, /memories/initiatives) still let through whoever
principal_only lets through as the principal: local login and an open-mode
call that names no one. Anyone else still needs an active, non-demo People
entry (api.principal_gate.caller_is_member)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .principal_gate_support import app_client, call, make_roster

READS = ["/departments", "/goals", "/memories/decisions", "/memories/initiatives"]


@pytest.fixture
def no_principal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from openexecutive.api.routes import episodic as episodic_route
    from openexecutive.departments import goals
    from openexecutive.departments import store as dept_store
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    ids = make_roster(tmp_path, monkeypatch)
    assert people_store.archive_person(ids["principal"])
    people_registry.invalidate()
    assert people_store.find_principal_person() is None
    ran: list[str] = []

    def _rec(name: str) -> Any:
        def _f(*_a: Any, **_kw: Any) -> list[Any]:
            ran.append(name)
            return []

        return _f

    monkeypatch.setattr(goals, "list_goal_views", _rec("goals"))
    monkeypatch.setattr(dept_store, "list_departments", _rec("departments"))
    monkeypatch.setattr(episodic_route, "list_decisions", _rec("decisions"))
    monkeypatch.setattr(episodic_route, "list_initiatives", _rec("initiatives"))
    return ran


def _statuses(monkeypatch: pytest.MonkeyPatch, signed: bool, who: str) -> list[int]:
    client = app_client(monkeypatch, signed=signed)
    return [call(client, signed, who, "GET", path).status_code for path in READS]


def test_local_login_reads_with_no_principal_on_the_roster(
    no_principal: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _statuses(monkeypatch, True, "operator") == [200] * len(READS)
    assert no_principal == ["departments", "goals", "decisions", "initiatives"]


def test_an_open_mode_call_that_names_no_one_reads_with_no_principal(
    no_principal: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _statuses(monkeypatch, False, "unsigned") == [200] * len(READS)


def test_an_active_teammate_still_reads(no_principal: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert _statuses(monkeypatch, True, "teammate") == [200] * len(READS)


@pytest.mark.parametrize(("signed", "who"), [(True, "stranger"), (True, "unsigned"), (True, "demo"),
                                             (False, "stranger")])
def test_everyone_else_is_still_refused(
    no_principal: list[str], monkeypatch: pytest.MonkeyPatch, signed: bool, who: str
) -> None:
    assert _statuses(monkeypatch, signed, who) == [403] * len(READS)
    assert no_principal == []
