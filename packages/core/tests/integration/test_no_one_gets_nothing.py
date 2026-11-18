"""A caller who resolves to no one on the roster (a signed-in email on no People
entry, a call with no caller while signed callers are on, a suspended entry)
gets an empty Home from GET /today and /morning-brief, with the shared fixed
header line, and an empty roster from GET /people. The principal, local login,
active teammates and demo people read as before.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.briefing.narrative import QUIET_PRINCIPAL
from openexecutive.memory import episodic

from .principal_gate_support import app_client, call, make_roster

CARD = "Approve the Q3 budget"
EMPTY = ("proposals", "people", "departments", "in_flight", "awaiting", "handled_overnight", "done_for_you")


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
    alerts_store.insert_alert(
        source="test", external_id="live", severity="high", headline=CARD, body="Needs a decision.",
    )
    episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(), channel="email",
        channel_ref="someone@co.example", intent_text="Follow up on the budget",
        created_by_person_id=ids["principal"],
    )
    return ids


def _as(monkeypatch: pytest.MonkeyPatch, ids: dict[str, int], who: str) -> tuple[Any, str]:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    client = app_client(monkeypatch, signed=True)
    if who == "suspended":
        assert people_store.suspend_person(ids["teammate"], by="principal")
        people_registry.invalidate()
        return client, "teammate"
    return client, who


@pytest.mark.parametrize("path", ["/today", "/morning-brief"])
@pytest.mark.parametrize("who", ["stranger", "unsigned", "suspended"])
def test_a_caller_who_resolves_to_no_one_gets_an_empty_home(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str, path: str
) -> None:
    client, as_who = _as(monkeypatch, ids, who)
    r = call(client, True, as_who, "GET", path)
    assert r.status_code == 200, r.text
    body = r.json()
    for key in EMPTY:
        assert body[key] == [], key
    assert body["narrative"] == QUIET_PRINCIPAL
    assert CARD not in r.text and "Olivia" not in r.text


@pytest.mark.parametrize("who", ["principal", "operator", "teammate", "demo"])
def test_everyone_else_still_gets_their_home(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client, as_who = _as(monkeypatch, ids, who)
    body = call(client, True, as_who, "GET", "/today").json()
    assert CARD in {p["headline"] for p in body["proposals"]}
    assert body["people"]


@pytest.mark.parametrize("who", ["stranger", "unsigned", "suspended"])
def test_a_caller_who_resolves_to_no_one_gets_no_roster(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client, as_who = _as(monkeypatch, ids, who)
    r = call(client, True, as_who, "GET", "/people")
    assert r.status_code == 200, r.text
    assert r.json() == []


@pytest.mark.parametrize("who", ["principal", "operator", "teammate", "demo"])
def test_everyone_else_still_gets_the_roster(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client, as_who = _as(monkeypatch, ids, who)
    r = call(client, True, as_who, "GET", "/people")
    assert r.status_code == 200, r.text
    assert r.json()


def test_local_login_gets_the_roster_with_no_principal_on_it(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    assert people_store.archive_person(ids["principal"])
    people_registry.invalidate()
    client = app_client(monkeypatch, signed=False)
    assert call(client, False, "unsigned", "GET", "/people").json()
    assert call(client, False, "stranger", "GET", "/people").json() == []
