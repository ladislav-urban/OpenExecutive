"""Who opens a card on GET /decisions/{id} (and its approve and reject).

The principal sees any card. Anyone else sees a card only when they are its
approver, it was routed to them (its companion alert), or it came from a
conversation of theirs; everyone else gets 404, as for an id that does not
exist. The class rules (private, approver-only) still apply on top. A demo
person opens, approves and rejects a card that is theirs.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openexecutive.alerts import store as alerts_store
from openexecutive.api.routes import decisions as decisions_route
from openexecutive.memory import decision_ledger, session_store

from .principal_gate_support import app_client, call, make_roster

EVERYONE = ("principal", "operator", "teammate", "demo", "stranger", "unsigned")


def _card(approver: int | None, *, cls: str = decisions_route._CALENDAR_CLASS,
          session: str | None = None, title: str = "Lunch with Acme") -> int:
    return decision_ledger.create_decision_instance(
        decision_class=cls, department="", originating_session_id=session,
        proposed_payload={"title": title}, idempotency_key=None, gate_mode="propose",
        approver_person_id=approver, confidence=None,
    )


@pytest.fixture
def cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    ids = make_roster(tmp_path, monkeypatch)
    alerts_store.initialize_db()
    session_store.create_session(
        "ben-chat", "Ben's chat", datetime.now(UTC).isoformat(), caller_person_id=ids["teammate"],
        db_path=Path("./episodic_memory.db").resolve(),
    )
    routed = _card(ids["principal"], title="Routed to Ben")
    alerts_store.insert_alert(
        source=decision_ledger.DECISION_ALERT_SOURCE,
        external_id=decision_ledger.decision_alert_external_id(routed),
        severity="medium", headline="Routed to Ben", body="", routed_to_person_id=ids["teammate"],
    )
    return {
        "ids": ids,
        "principals": _card(ids["principal"], title="The principal's own"),
        "bens": _card(ids["teammate"], title="Ben approves"),
        "routed": routed,
        "from_ben": _card(ids["principal"], session="ben-chat", title="From Ben's chat"),
        "danas": _card(ids["demo"], cls="app_action", title="Dana's own"),
    }


SEES = {
    "principal": {"principals", "bens", "routed", "from_ben"},
    "operator": {"principals", "bens", "routed", "from_ben"},
    "teammate": {"bens", "routed", "from_ben"},
    "demo": {"danas"},
    "stranger": set(),
    "unsigned": set(),
}


@pytest.mark.parametrize("who", EVERYONE)
def test_each_caller_opens_only_the_cards_that_are_theirs(
    cards: dict[str, Any], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client = app_client(monkeypatch, signed=True)
    for name in ("principals", "bens", "routed", "from_ben", "danas"):
        r = call(client, True, who, "GET", f"/decisions/{cards[name]}")
        expected = 200 if name in SEES[who] else 404
        assert r.status_code == expected, (who, name, r.text)
        if expected == 404:
            assert r.json()["detail"] == "Decision instance not found"


@pytest.mark.parametrize("who", ["stranger", "unsigned", "demo"])
def test_no_one_else_approves_or_rejects_the_principals_card(
    cards: dict[str, Any], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    client = app_client(monkeypatch, signed=True)
    card = cards["principals"]
    for action in ("approve", "reject", "cancel"):
        r = call(client, True, who, "POST", f"/decisions/{card}/{action}", {})
        assert r.status_code in (403, 404), (action, r.text)
        if who != "demo":
            # The demo write gate may answer first; anyone else is told it
            # doesn't exist.
            assert r.status_code == 404, (action, r.text)
    instance = decision_ledger.get_decision_instance(card)
    assert instance is not None and instance.status == decision_ledger.STATUS_PROPOSED


def _stub_spec(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    async def _approve(instance: Any, _body: Any, _request: Any, resolver: int | None) -> Any:
        assert decision_ledger.mark_resolved(
            instance.id, decision_ledger.STATUS_APPROVED_UNCHANGED, resolver_person_id=resolver,
        )
        return decision_ledger.get_decision_instance(instance.id)

    approve = AsyncMock(side_effect=_approve)
    spec = SimpleNamespace(name="app_action", approve=approve, alert_source=None, after_reject=None, cancel=None)
    monkeypatch.setattr(decisions_route, "_spec_for", lambda _instance: spec)
    return approve


def test_a_demo_person_opens_approves_and_rejects_their_own_card(
    cards: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    approve = _stub_spec(monkeypatch)
    client = app_client(monkeypatch, signed=True)
    card = cards["danas"]
    assert call(client, True, "demo", "GET", f"/decisions/{card}").status_code == 200
    r = call(client, True, "demo", "POST", f"/decisions/{card}/approve", {})
    assert r.status_code == 200, r.text
    approve.assert_awaited_once()
    other = _card(cards["ids"]["demo"], cls="app_action", title="Dana's second")
    r = call(client, True, "demo", "POST", f"/decisions/{other}/reject", {})
    assert r.status_code == 200, r.text
    instance = decision_ledger.get_decision_instance(other)
    assert instance is not None and instance.status == decision_ledger.STATUS_REJECTED


def test_a_teammate_answers_the_card_they_approve(
    cards: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_spec(monkeypatch)
    client = app_client(monkeypatch, signed=True)
    r = call(client, True, "teammate", "POST", f"/decisions/{cards['bens']}/reject", {})
    assert r.status_code == 200, r.text
    # Routed to them or from their chat: they may open it, not decide it.
    r = call(client, True, "teammate", "POST", f"/decisions/{cards['routed']}/approve", {})
    assert r.status_code == 403, r.text
