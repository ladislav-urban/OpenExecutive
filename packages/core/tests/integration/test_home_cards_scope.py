"""Which cards each caller's Home shows (GET /today, /morning-brief, the MCP
briefing) and counts (GET /badges).

The principal sees every card. Anyone else sees a card routed to them, one
routed to no one (the shared board), and a card backed by a decision only when
it is theirs by GET /decisions/{id}'s rule (its approver, the person it was
routed to, or the owner of the conversation it came from). A card routed to
the principal or to another person is never on their Home.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.api.caller import Caller
from openexecutive.memory import decision_ledger

from .principal_gate_support import EMAILS, app_client, call, make_roster

SHARED = "Approve the Q3 budget"
PRINCIPALS = "Sign the lease"
BENS = "Order Ben's laptop"
DANAS = "Dana's trial quote"
DECISION_FOR_PRINCIPAL = "Lunch with Acme"
DECISION_FOR_BEN = "Ben's supplier call"
ALL = {SHARED, PRINCIPALS, BENS, DANAS, DECISION_FOR_PRINCIPAL, DECISION_FOR_BEN}


def _decision_card(headline: str, approver: int) -> None:
    instance = decision_ledger.create_decision_instance(
        decision_class="meeting_scheduling", department="", originating_session_id=None,
        proposed_payload={"title": headline}, idempotency_key=None, gate_mode="propose",
        approver_person_id=approver, confidence=None,
    )
    alerts_store.insert_alert(
        source="test", external_id=f"card-{instance}", severity="high", headline=headline, body="",
        topic_tags=[f"{decision_ledger.DECISION_INSTANCE_TAG_PREFIX}{instance}"],
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
    for headline, routed in ((SHARED, None), (PRINCIPALS, ids["principal"]), (BENS, ids["teammate"]),
                             (DANAS, ids["demo"])):
        alerts_store.insert_alert(
            source="test", external_id=headline, severity="high", headline=headline,
            body="Needs a decision.", routed_to_person_id=routed,
        )
    _decision_card(DECISION_FOR_PRINCIPAL, ids["principal"])
    _decision_card(DECISION_FOR_BEN, ids["teammate"])
    return ids


EXPECTED = {
    "principal": ALL,
    "teammate": {SHARED, BENS, DECISION_FOR_BEN},
    "demo": {SHARED, DANAS},
}


def _headlines(body: dict[str, Any]) -> set[str]:
    return {p["headline"] for p in body["proposals"]} & ALL


@pytest.mark.parametrize("path", ["/today", "/morning-brief"])
@pytest.mark.parametrize("who", sorted(EXPECTED))
def test_each_home_holds_only_the_callers_cards(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str, path: str
) -> None:
    client = app_client(monkeypatch, signed=True)
    r = call(client, True, who, "GET", path)
    assert r.status_code == 200, r.text
    assert _headlines(r.json()) == EXPECTED[who]


@pytest.mark.parametrize("who", sorted(EXPECTED))
def test_the_mcp_briefing_holds_the_same_cards(ids: dict[str, int], who: str) -> None:
    from openexecutive.mcp_server import server as mcp_server

    async def _go() -> str:
        with patch.object(mcp_server, "_who", return_value=Caller("user", EMAILS[who])):
            return str(await mcp_server.today_briefing())

    assert _headlines(json.loads(asyncio.run(_go()))) == EXPECTED[who]


@pytest.mark.parametrize("who", sorted(EXPECTED))
def test_the_home_badge_counts_the_same_cards(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client = app_client(monkeypatch, signed=True)
    today = call(client, True, who, "GET", "/today").json()
    badges = call(client, True, who, "GET", "/badges").json()["counts"]
    assert badges["home"] == today["needs_you_count"]
