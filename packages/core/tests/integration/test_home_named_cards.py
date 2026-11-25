"""A private ERPNext alert that names its readers (``named:<id>``, routed to
the principal: a credit hold for the people oe_connector checked) is on each
named person's own Home, in its count and its briefing, and on nobody else's
but the principal's. Visibility only: its page and its approval stay the
principal's, as before. A ``named:`` tag counts only on a private ERPNext
alert, read back from the store.

Also here: a card raised from a teammate's own conversation (approved by the
principal) stays on that teammate's Home, and a caller who is no one sees
none of these.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.alerts.models import PRIVATE_ALERT_TAG
from openexecutive.api.caller import Caller
from openexecutive.memory import decision_ledger, session_store

from .principal_gate_support import EMAILS, app_client, call, make_roster

HOLD = "Hold: SO-0001 for Ben"
DEMO_HOLD = "Hold: SO-0002 for Dana"
BARE = "Low stock: ITEM-1"
NOT_ERP = "A private note naming Ben"
RAISED = "Ben's supplier quote"
ALL = {HOLD, DEMO_HOLD, BARE, NOT_ERP, RAISED}


def _named_hold(headline: str, ref: str, named: int, principal: int) -> int:
    return alerts_store.insert_alert(
        source="erpnext", external_id=f"erpnext:credit_hold:Acme:{ref}:2026-11", severity="high",
        headline=headline, body="Over its credit limit.",
        topic_tags=["finance", PRIVATE_ALERT_TAG, f"named:{named}"],
        dedup_key=f"private:erpnext:credit_hold:Acme:{ref}:named:{named}",
        routed_to_person_id=principal,
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
    principal, ben, dana = ids["principal"], ids["teammate"], ids["demo"]
    ids["hold"] = _named_hold(HOLD, "SO-0001", ben, principal)
    ids["demo_hold"] = _named_hold(DEMO_HOLD, "SO-0002", dana, principal)
    # A named: tag on a card that isn't private, and on a private card that
    # isn't from ERPNext: neither names anyone.
    alerts_store.insert_alert(
        source="erpnext", external_id="erpnext:low_stock:ITEM-1:2026-11", severity="high",
        headline=BARE, body="The principal's card.", topic_tags=["ops", f"named:{ben}"],
        dedup_key="erpnext:low_stock:ITEM-1", routed_to_person_id=principal,
    )
    alerts_store.insert_alert(
        source="test", external_id="private-note", severity="high", headline=NOT_ERP,
        body="The principal's own.", topic_tags=[PRIVATE_ALERT_TAG, f"named:{ben}"],
        routed_to_person_id=principal,
    )
    # A card from Ben's own conversation, for the principal to approve.
    session_store.create_session("ben-chat", "Quote", datetime.now(UTC).isoformat(), caller_person_id=ben, db_path=db)
    instance = decision_ledger.create_decision_instance(
        decision_class="meeting_scheduling", department="", originating_session_id="ben-chat",
        proposed_payload={"title": RAISED}, idempotency_key=None, gate_mode="propose",
        approver_person_id=principal, confidence=None,
    )
    alerts_store.insert_alert(
        source="test", external_id=f"card-{instance}", severity="high", headline=RAISED, body="",
        topic_tags=[f"{decision_ledger.DECISION_INSTANCE_TAG_PREFIX}{instance}"],
    )
    return ids


EXPECTED = {
    "principal": ALL,
    "teammate": {HOLD, RAISED},
    "demo": {DEMO_HOLD},
    "stranger": set(),
}


def _headlines(body: dict[str, Any]) -> set[str]:
    return {p["headline"] for p in body.get("proposals") or []} & ALL


@pytest.mark.parametrize("path", ["/today", "/morning-brief"])
@pytest.mark.parametrize("who", sorted(EXPECTED))
def test_a_named_card_is_on_the_named_persons_home_only(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str, path: str
) -> None:
    client = app_client(monkeypatch, signed=True)
    r = call(client, True, who, "GET", path)
    if who == "stranger":
        assert r.status_code in (200, 403), r.text
        if r.status_code == 403:
            return
    assert r.status_code == 200, r.text
    assert _headlines(r.json()) == EXPECTED[who]


@pytest.mark.parametrize("who", ["principal", "teammate", "demo"])
def test_the_mcp_briefing_holds_the_same_named_cards(ids: dict[str, int], who: str) -> None:
    from openexecutive.mcp_server import server as mcp_server

    async def _go() -> str:
        with patch.object(mcp_server, "_who", return_value=Caller("user", EMAILS[who])):
            return str(await mcp_server.today_briefing())

    assert _headlines(json.loads(asyncio.run(_go()))) == EXPECTED[who]


@pytest.mark.parametrize("who", ["principal", "teammate", "demo"])
def test_the_home_badge_counts_the_named_card(ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, who: str) -> None:
    client = app_client(monkeypatch, signed=True)
    today = call(client, True, who, "GET", "/today").json()
    badges = call(client, True, who, "GET", "/badges").json()["counts"]
    assert badges["home"] == today["needs_you_count"]


def test_the_named_card_is_to_look_at_only(ids: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    # Its page and its approval stay the principal's, as before (the page's
    # look-only opening for the named person is a separate, planned change).
    client = app_client(monkeypatch, signed=True)
    for who in ("teammate", "demo"):
        assert call(client, True, who, "GET", f"/alerts/{ids['hold']}/card").status_code == 404
        assert call(client, True, who, "POST", f"/alerts/{ids['hold']}/approve", {}).status_code == 404
    assert call(client, True, "principal", "GET", f"/alerts/{ids['hold']}/card").status_code == 200


def test_no_one_and_no_tag_open_a_named_card() -> None:
    from openexecutive.api.routes.today import _card_for_caller, _named_for_caller

    # No People id (an A2A supplier, a caller who resolves to no one): closed.
    assert _card_for_caller(1, None, None, {}, named=True) is False
    assert _named_for_caller([], None) == set()
