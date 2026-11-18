"""Who reads the departments and goals (GET /departments, /departments/{slug},
/goals): the principal and an active, non-demo teammate. A demo person, a
signed-in email on no People entry, a call with no caller while signed
callers are on, a suspended teammate and a caller the roster can't be read
for get 403 before any work (api.principal_gate.member_only)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .principal_gate_support import app_client, call, make_roster

READS = ["/departments", "/departments/sales", "/goals"]


@pytest.fixture
def reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, int], list[str]]:
    from openexecutive.departments import goals
    from openexecutive.departments import store as dept_store

    ids = make_roster(tmp_path, monkeypatch)
    ran: list[str] = []

    def _rec(name: str, result: Any) -> Any:
        def _f(*_a: Any, **_kw: Any) -> Any:
            ran.append(name)
            return result

        return _f

    monkeypatch.setattr(goals, "list_goal_views", _rec("goals", []))
    monkeypatch.setattr(dept_store, "list_departments", _rec("departments", []))
    monkeypatch.setattr(dept_store, "get_department", _rec("department", None))
    return ids, ran


def _as(monkeypatch: pytest.MonkeyPatch, ids: dict[str, int], who: str) -> str:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    if who == "suspended":
        assert people_store.suspend_person(ids["teammate"], by="principal")
        people_registry.invalidate()
        return "teammate"
    if who == "unreadable":
        def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("database is locked")

        monkeypatch.setattr(people_store, "get_person", _boom)
        return "teammate"
    return who


@pytest.mark.parametrize("who", ["principal", "operator", "teammate"])
def test_the_principal_and_an_active_teammate_read_on(
    reads: tuple[dict[str, int], list[str]], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    ids, ran = reads
    client = app_client(monkeypatch, signed=True)
    as_who = _as(monkeypatch, ids, who)
    for path in READS:
        r = call(client, True, as_who, "GET", path)
        # The department is unknown here: 404, past the gate.
        assert r.status_code == (404 if path.endswith("/sales") else 200), (path, r.text)
    assert ran == ["departments", "department", "goals"]


@pytest.mark.parametrize("who", ["demo", "stranger", "unsigned", "suspended", "unreadable"])
def test_everyone_else_is_refused_before_any_work(
    reads: tuple[dict[str, int], list[str]], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    ids, ran = reads
    client = app_client(monkeypatch, signed=True)
    as_who = _as(monkeypatch, ids, who)
    for path in READS:
        r = call(client, True, as_who, "GET", path)
        assert r.status_code == 403, (path, r.text)
    assert ran == []


def test_a_stranger_is_refused_in_open_mode_too(
    reads: tuple[dict[str, int], list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ids, ran = reads
    client = app_client(monkeypatch, signed=False)
    for path in READS:
        assert call(client, False, "stranger", "GET", path).status_code == 403, path
    assert ran == []
