"""Who gets the departments on GET /today and /morning-brief.

The departments carry titles, goal counts and the off-track and at-risk goals
with their owners, which GET /departments and /goals give only to the principal
and the active team. Home follows the same rule: anyone else (a demo person, a
signed-in email on no People entry, a call with no caller while signed callers
are on, a suspended teammate) gets none.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openexecutive.alerts import store as alerts_store

from .principal_gate_support import app_client, call, make_roster


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from openexecutive.api.routes import today as today_route
    from openexecutive.briefing import narrative_cache
    from openexecutive.departments import registry as dept_registry
    from openexecutive.departments import store as dept_store
    from openexecutive.people import insights_cache
    from openexecutive.workflows import persistence as wf_persistence

    ids = make_roster(tmp_path, monkeypatch)
    db = Path("./episodic_memory.db").resolve()
    for module in (wf_persistence, insights_cache, narrative_cache, dept_store):
        monkeypatch.setattr(module, "DB_PATH", db)
    alerts_store.initialize_db()
    wf_persistence.initialize_runs_db(db)
    insights_cache.initialize_db(db)
    narrative_cache.initialize_db(db)
    dept_store.initialize_db(db)
    dept_store.seed_default_departments(db_path=db)
    dept_registry.invalidate()
    monkeypatch.setattr(today_route, "_regen_briefing_narrative", AsyncMock())
    monkeypatch.setattr(today_route, "_regen_stale_insights", AsyncMock())
    return ids


def _departments(monkeypatch: pytest.MonkeyPatch, ids: dict[str, int], who: str, path: str) -> list[object]:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    as_who = who
    if who == "suspended":
        assert people_store.suspend_person(ids["teammate"], by="principal")
        people_registry.invalidate()
        as_who = "teammate"
    client = app_client(monkeypatch, signed=True)
    r = call(client, True, as_who, "GET", path)
    assert r.status_code == 200, r.text
    return list(r.json()["departments"])


@pytest.mark.parametrize("path", ["/today", "/morning-brief"])
@pytest.mark.parametrize("who", ["principal", "operator", "teammate"])
def test_the_principal_and_the_team_get_the_departments(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str, path: str
) -> None:
    assert _departments(monkeypatch, ids, who, path)


@pytest.mark.parametrize("path", ["/today", "/morning-brief"])
@pytest.mark.parametrize("who", ["demo", "stranger", "unsigned", "suspended"])
def test_anyone_else_gets_no_departments(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str, path: str
) -> None:
    assert _departments(monkeypatch, ids, who, path) == []
