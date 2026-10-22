"""GET /today's Home rules for the caller, and GET /alerts/{id}/card.

The rules themselves are tested in test_home_rules.py; this checks the route
works them out for whoever asks: their lanes and actions, Needs you's count
with the cards other endpoints list, and a card page only its own person
sees.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import PRIVATE_ALERT_TAG
from openexecutive.briefing import home_rules
from openexecutive.briefing import narrative as briefing_narrative
from openexecutive.people import store as people_store

from .test_today_route import _make_client, _setup_isolated_db


@pytest.fixture(autouse=True)
def _no_narrative(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(**_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _fake)


@pytest.fixture()
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    db = tmp_path / "home.db"
    _setup_isolated_db(db, monkeypatch)
    monkeypatch.setattr(home_rules, "_WAITING_CARDS", {})
    principal = people_store.upsert_person(full_name="Pat Principal", email="pat@example.com", is_principal=True)
    dana = people_store.upsert_person(full_name="Dana Kim", email="dana@example.com")
    unrouted = alert_store.insert_alert(
        source="system", external_id="unrouted", severity="high",
        headline="Approve the Q3 budget", body="Needs a decision.", db_path=db,
    )
    danas = alert_store.insert_alert(
        source="system", external_id="danas", severity="high",
        headline="Chase the supplier", body="It has gone quiet.", routed_to_person_id=dana, db_path=db,
    )
    return {"db": db, "principal": principal, "dana": dana, "unrouted": unrouted, "danas": danas}


def _as(email: str) -> dict[str, str]:
    return {"x-caller-email": email}


def test_the_principal_gets_their_lanes_actions_and_counts(board: dict[str, Any]) -> None:
    body = _make_client().get("/today", headers=_as("pat@example.com")).json()
    lanes = {p["alert_id"]: p["lane"] for p in body["proposals"]}
    assert lanes == {board["unrouted"]: "needs_you", board["danas"]: "team"}
    by_id = {p["alert_id"]: p for p in body["proposals"]}
    assert by_id[board["unrouted"]]["card_kind"] == "proposal"
    assert by_id[board["unrouted"]]["actions"][0]["key"] == "approve"
    # The principal may approve a teammate's card too.
    assert "approve" in [a["key"] for a in by_id[board["danas"]]["actions"]]
    assert body["needs_you_count"] == 1
    assert body["summary"][0] == {
        "text": "1 thing needs you", "count": 1, "target": {"kind": "needs_you", "panel": None, "href": None},
    }
    assert [t["key"] for t in body["tiles"]] == ["departments", "people", "team"]
    assert body["dismiss_older"]["alert_ids"] == []
    assert len(body["snooze_choices"]) >= 2
    assert body["quiet"] is False and body["quiet_banner"] is False


def test_a_teammate_sees_their_own_card_and_no_approve_on_others(board: dict[str, Any]) -> None:
    body = _make_client().get("/today", headers=_as("dana@example.com")).json()
    by_id = {p["alert_id"]: p for p in body["proposals"]}
    assert by_id[board["danas"]]["lane"] == "needs_you"
    assert by_id[board["unrouted"]]["lane"] == "team"
    assert "approve" not in [a["key"] for a in by_id[board["unrouted"]]["actions"]]
    assert body["needs_you_count"] == 1


def test_cards_from_other_endpoints_count_toward_needs_you(board: dict[str, Any]) -> None:
    home_rules.register_waiting_cards("replies", lambda person: 2 if person.full_name == "Pat Principal" else 0)
    body = _make_client().get("/today", headers=_as("pat@example.com")).json()
    assert body["needs_you_count"] == 3
    assert body["summary"][0]["text"] == "3 things need you"
    assert _make_client().get("/today", headers=_as("dana@example.com")).json()["needs_you_count"] == 1


def test_snooze_choices_use_the_callers_own_zone(board: dict[str, Any]) -> None:
    from openexecutive.people.notifications import NotificationSettings, save_notification_settings

    save_notification_settings(board["dana"], NotificationSettings(timezone="Asia/Tokyo"))
    body = _make_client().get("/today", headers=_as("dana@example.com")).json()
    assert all(c["until"].endswith("+09:00") for c in body["snooze_choices"])
    snoozes = [a for a in body["proposals"][0]["actions"] if a["key"] == "snooze"]
    assert [a["until"] for a in snoozes] == [c["until"] for c in body["snooze_choices"]]


def test_the_clients_own_zone_comes_first_and_a_bad_one_is_ignored(board: dict[str, Any]) -> None:
    from openexecutive.people.notifications import NotificationSettings, save_notification_settings

    save_notification_settings(board["dana"], NotificationSettings(timezone="Asia/Tokyo"))
    client = _make_client()
    prague = client.get("/today", params={"tz": "Europe/Prague"}, headers=_as("dana@example.com")).json()
    assert all(c["until"][-6:] in ("+01:00", "+02:00") for c in prague["snooze_choices"])
    bad = client.get("/today", params={"tz": "../etc"}, headers=_as("dana@example.com")).json()
    assert all(c["until"].endswith("+09:00") for c in bad["snooze_choices"])
    card = client.get(f"/alerts/{board['danas']}/card", params={"tz": "Europe/Prague"}, headers=_as("dana@example.com"))
    snoozes = [a for a in card.json()["proposal"]["actions"] if a["key"] == "snooze"]
    assert snoozes and all(a["until"][-6:] in ("+01:00", "+02:00") for a in snoozes)


def test_the_card_page_answers_only_the_cards_own_person(board: dict[str, Any]) -> None:
    client = _make_client()
    mine = client.get(f"/alerts/{board['danas']}/card", headers=_as("dana@example.com"))
    assert mine.status_code == 200
    card = mine.json()
    assert card["proposal"]["alert_id"] == board["danas"]
    assert card["proposal"]["lane"] == "needs_you"
    assert [p["full_name"] for p in card["people"]] == ["Dana Kim"]
    # The principal may open any card; a teammate only their own.
    assert client.get(f"/alerts/{board['danas']}/card", headers=_as("pat@example.com")).status_code == 200
    assert client.get(f"/alerts/{board['unrouted']}/card", headers=_as("dana@example.com")).status_code == 404
    assert client.get(f"/alerts/{board['unrouted']}/card", headers=_as("stranger@example.com")).status_code == 404
    assert client.get("/alerts/999999/card", headers=_as("pat@example.com")).status_code == 404


def test_a_card_no_longer_waiting_is_not_on_its_page(board: dict[str, Any]) -> None:
    alert_store.set_status(board["danas"], "dismissed", db_path=board["db"])
    assert _make_client().get(f"/alerts/{board['danas']}/card", headers=_as("dana@example.com")).status_code == 404


def test_the_page_does_not_reveal_a_private_alert_to_a_teammate(board: dict[str, Any]) -> None:
    private = alert_store.insert_alert(
        source="system", external_id="private", severity="high", headline="Contact's mail",
        body="Private.", topic_tags=[PRIVATE_ALERT_TAG], routed_to_person_id=board["dana"], db_path=board["db"],
    )
    client = _make_client()
    assert client.get(f"/alerts/{private}/card", headers=_as("pat@example.com")).status_code == 200
    assert client.get(f"/alerts/{private}/card", headers=_as("dana@example.com")).status_code == 404
