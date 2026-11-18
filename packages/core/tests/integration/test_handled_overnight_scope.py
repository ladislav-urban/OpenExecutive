"""Home's "handled overnight" rows (GET /today, /morning-brief, the MCP
briefing) follow the caller's own Home: the principal reads every row; anyone
else only rows about a card their Home keeps, never one about a card routed to
someone else, nor a row about no card."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.api.caller import Caller

from .principal_gate_support import EMAILS, app_client, call, make_roster


@pytest.fixture
def cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from openexecutive.api.routes import today as today_route
    from openexecutive.briefing import brief_state, narrative_cache
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
    alert_ids = {
        who: alerts_store.insert_alert(
            source="test", external_id=f"handled-{who}", severity="medium",
            headline=f"Card for {who}", body="", routed_to_person_id=ids[who],
        )
        for who in ("principal", "teammate", "demo")
    }
    now = datetime.now(UTC).isoformat()

    def _handled(_since: Any, limit: int = 20, include_private: bool = False) -> list[dict[str, Any]]:
        rows = [
            {"kind": "closed", "summary": f"Closed the card for {who}", "at": now, "alert_id": alert_id,
             "event_type": "alert_review", "details": {"headline": f"Card for {who}", "new_status": "resolved"}}
            for who, alert_id in alert_ids.items()
        ]
        rows.append({"kind": "watching", "summary": "Started watching acme.example", "at": now,
                     "alert_id": None, "event_type": "watch", "details": {"slug": "acme"}})
        return rows

    monkeypatch.setattr(brief_state, "handled_since", _handled)
    return {**ids, **{f"card_{who}": i for who, i in alert_ids.items()}}


def _rows(body: dict[str, Any]) -> set[str]:
    return {r["summary"] for r in body["handled_overnight"]}


EVERY = {"Closed the card for principal", "Closed the card for teammate", "Closed the card for demo",
         "Started watching acme.example"}
EXPECTED = {
    "principal": EVERY,
    "teammate": {"Closed the card for teammate"},
    "demo": {"Closed the card for demo"},
    "stranger": set(),
}


@pytest.mark.parametrize("path", ["/today", "/morning-brief"])
@pytest.mark.parametrize("who", sorted(EXPECTED))
def test_each_home_holds_only_the_callers_handled_rows(
    cards: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str, path: str
) -> None:
    client = app_client(monkeypatch, signed=True)
    r = call(client, True, who, "GET", path)
    assert r.status_code == 200, r.text
    assert _rows(r.json()) == EXPECTED[who]


@pytest.mark.parametrize("who", ["principal", "teammate", "demo"])
def test_the_mcp_briefing_holds_the_same_rows(cards: dict[str, int], who: str) -> None:
    from openexecutive.mcp_server import server as mcp_server

    async def _go() -> str:
        with patch.object(mcp_server, "_who", return_value=Caller("user", EMAILS[who])):
            return str(await mcp_server.today_briefing())

    assert _rows(json.loads(asyncio.run(_go()))) == EXPECTED[who]
