"""The MCP resource oe://today/briefing gives each caller what their own
GET /today gives them: the same departments, in-flight list, cards and people.
A caller who resolves to no one is refused the resource (and gets an empty
Home over HTTP)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.api.caller import Caller
from openexecutive.memory import episodic

from .principal_gate_support import EMAILS, app_client, call, make_roster


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
    for headline, routed in (("Approve the Q3 budget", None), ("Order Ben's laptop", ids["teammate"]),
                             ("Sign the lease", ids["principal"]), ("Dana's trial quote", ids["demo"])):
        alerts_store.insert_alert(
            source="test", external_id=headline, severity="high", headline=headline,
            body="Needs a decision.", routed_to_person_id=routed,
        )
    run_at = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    for text, by in (("Principal follow-up", ids["principal"]), ("Ben's follow-up", ids["teammate"]),
                     ("Dana's follow-up", ids["demo"]), ("A system check-in", None)):
        episodic.insert_scheduled_action(
            run_at=run_at, channel="email", channel_ref="someone@co.example", intent_text=text,
            created_by_person_id=by,
        )
    return ids


def _shape(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "departments": sorted(d["slug"] for d in body["departments"]),
        "in_flight": sorted(i["intent"] for i in body["in_flight"]),
        "proposals": sorted(p["headline"] for p in body["proposals"]),
        "people": sorted((p["id"], p["full_name"]) for p in body["people"]),
    }


def _mcp(who: Caller) -> dict[str, Any]:
    from openexecutive.mcp_server import server as mcp_server

    async def _go() -> str:
        with patch.object(mcp_server, "_who", return_value=who):
            return str(await mcp_server.today_briefing())

    return dict(json.loads(asyncio.run(_go())))


@pytest.mark.parametrize("who", ["principal", "teammate", "demo"])
def test_the_briefing_equals_the_callers_own_today(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client = app_client(monkeypatch, signed=False)
    http = call(client, False, who, "GET", "/today")
    assert http.status_code == 200, http.text
    mcp = _mcp(Caller("user", EMAILS[who]))
    assert _shape(mcp) == _shape(http.json())
    if who != "principal":
        assert "Principal follow-up" not in {i["intent"] for i in mcp["in_flight"]}
        assert "Sign the lease" not in {p["headline"] for p in mcp["proposals"]}


def test_a_demo_person_gets_no_departments_and_only_their_own_follow_ups(
    ids: dict[str, int],
) -> None:
    mcp = _mcp(Caller("user", EMAILS["demo"]))
    assert mcp["departments"] == []
    assert {i["intent"] for i in mcp["in_flight"]} == {"Dana's follow-up"}


@pytest.mark.parametrize("who", [Caller("user", EMAILS["stranger"]), Caller("service")])
def test_a_caller_who_resolves_to_no_one_is_refused(ids: dict[str, int], who: Caller) -> None:
    from openexecutive.mcp_server import server as mcp_server

    with pytest.raises(mcp_server.CallerRefused):
        _mcp(who)
