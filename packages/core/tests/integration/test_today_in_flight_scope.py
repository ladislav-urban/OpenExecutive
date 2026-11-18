"""What GET /today's "in flight" list shows each caller.

The principal sees every pending action. Anyone else sees only the pending
actions their own turns queued (``created_by_person_id``), never a nudge or
an action the system queued on its own; a caller who resolves to no one, or
a call with no caller while signed callers are on, sees none.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.memory import episodic

from .principal_gate_support import app_client, call, make_roster

PRINCIPAL_ACTION = "Follow up with the bank on the loan"
TEAMMATE_ACTION = "Send Ben's supplier recap"
DEMO_ACTION = "Send Dana the trial checklist"
SYSTEM_ACTION = "Weekly check-in with the sales team"
NUDGE = "Remind the team about the board pack"
TEAMMATE_NUDGE = "Nudge Ben about the expense report"

EVERY_ROW = {PRINCIPAL_ACTION, TEAMMATE_ACTION, DEMO_ACTION, SYSTEM_ACTION, NUDGE, TEAMMATE_NUDGE}


def _pending(text: str, by: int | None, *, kind: str = "ad_hoc") -> None:
    episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        channel="email", channel_ref="someone@co.example", intent_text=text, kind=kind,
        created_by_person_id=by,
        scope_key=f"nudge:test:{text}" if kind == "proactive_nudge" else None,
    )


@pytest.fixture
def ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from openexecutive.api.routes import today as today_route
    from openexecutive.briefing import narrative_cache
    from openexecutive.people import insights_cache
    from openexecutive.workflows import persistence as wf_persistence

    ids = make_roster(tmp_path, monkeypatch)
    db = Path("./episodic_memory.db").resolve()
    for module in (wf_persistence, insights_cache, narrative_cache):
        monkeypatch.setattr(module, "DB_PATH", db)
    alerts_store.initialize_db()
    wf_persistence.initialize_runs_db(db)
    insights_cache.initialize_db(db)
    narrative_cache.initialize_db(db)
    monkeypatch.setattr(today_route, "_regen_briefing_narrative", AsyncMock())
    monkeypatch.setattr(today_route, "_regen_stale_insights", AsyncMock())
    _pending(PRINCIPAL_ACTION, ids["principal"])
    _pending(TEAMMATE_ACTION, ids["teammate"])
    _pending(DEMO_ACTION, ids["demo"])
    _pending(SYSTEM_ACTION, None)
    _pending(NUDGE, None, kind="proactive_nudge")
    _pending(TEAMMATE_NUDGE, ids["teammate"], kind="proactive_nudge")
    return ids


def _in_flight(monkeypatch: pytest.MonkeyPatch, signed: bool, who: str, path: str = "/today") -> set[str]:
    client = app_client(monkeypatch, signed=signed)
    r = call(client, signed, who, "GET", path)
    assert r.status_code == 200, r.text
    return {i["intent"] for i in r.json()["in_flight"]} & EVERY_ROW


@pytest.mark.parametrize("signed", [False, True])
def test_the_principal_sees_everything_in_flight(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    assert _in_flight(monkeypatch, signed, "principal") == EVERY_ROW


def test_local_login_sees_everything_in_flight(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _in_flight(monkeypatch, True, "operator") == EVERY_ROW


@pytest.mark.parametrize("signed", [False, True])
def test_a_teammate_sees_only_what_they_queued(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    assert _in_flight(monkeypatch, signed, "teammate") == {TEAMMATE_ACTION}


@pytest.mark.parametrize("signed", [False, True])
def test_a_demo_person_sees_only_what_they_queued(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    assert _in_flight(monkeypatch, signed, "demo") == {DEMO_ACTION}


@pytest.mark.parametrize("signed", [False, True])
def test_an_email_on_no_people_entry_sees_nothing_in_flight(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    assert _in_flight(monkeypatch, signed, "stranger") == set()


def test_a_call_with_no_caller_sees_nothing_in_flight_when_callers_are_signed(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _in_flight(monkeypatch, True, "unsigned") == set()


def test_a_suspended_teammate_sees_at_most_what_they_queued(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    assert people_store.suspend_person(ids["teammate"], by="principal")
    people_registry.invalidate()
    assert _in_flight(monkeypatch, False, "teammate") <= {TEAMMATE_ACTION}


@pytest.mark.parametrize("who", ["teammate", "stranger"])
def test_the_deprecated_alias_scopes_in_flight_the_same_way(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    expected = {TEAMMATE_ACTION} if who == "teammate" else set()
    assert _in_flight(monkeypatch, True, who, "/morning-brief") == expected


def test_the_server_reads_still_see_everything_in_flight(ids: dict[str, int]) -> None:
    from openexecutive.api.routes import today as today_route

    assert {i.intent for i in today_route._build_today().in_flight} == EVERY_ROW
