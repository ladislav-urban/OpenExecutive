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
        "text": "1 waits for your decision", "count": 1, "target": {"kind": "needs_you", "panel": None, "href": None},
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
    assert body["summary"][0]["text"] == "3 wait for your decision"
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


def test_an_erp_alerts_card_carries_its_record_and_open(board: dict[str, Any]) -> None:
    # As a demo bundle loads it: source erpnext, the event's ref as dedup_key.
    erp = alert_store.insert_alert(
        source="erpnext", external_id="demo-overdue-ACC-SINV-2026-00042", severity="high",
        headline="ACC-SINV-2026-00042 is 41 days overdue", body="Metro owes $9,140.",
        dedup_key="erpnext:overdue_receivable:ACC-SINV-2026-00042", db_path=board["db"],
    )
    body = _make_client().get("/today", headers=_as("pat@example.com")).json()
    by_id = {p["alert_id"]: p for p in body["proposals"]}
    card = by_id[erp]
    assert card["record"] == {"doctype": "Sales Invoice", "name": "ACC-SINV-2026-00042"}
    opened = [a for a in card["actions"] if a["key"] == "open_record"]
    assert opened == [{**opened[0], "label": "Open", "primary": False, "href": None}]
    # Every other card: no record, no Open.
    other = by_id[board["unrouted"]]
    assert other["record"] is None and "open_record" not in [a["key"] for a in other["actions"]]


def _bundle_and_live(db: Path, kind: str, ref: str) -> tuple[int, int]:
    """The demo bundle's card for everyone, and the live event's alert for
    the same rule and document, as the pipeline stores it (private)."""
    shared = alert_store.insert_alert(
        source="erpnext", external_id=f"demo-{kind}-{ref}", severity="medium",
        headline=f"Bundle: {ref}", body="The bundle's copy.", dedup_key=f"erpnext:{kind}:{ref}", db_path=db,
    )
    live = alert_store.insert_alert(
        source="erpnext", external_id=f"erpnext:{kind}:{ref}:2026-11", severity="medium",
        headline=f"Live: {ref}", body="The principal's live figures.", topic_tags=["stock", PRIVATE_ALERT_TAG],
        dedup_key=f"private:erpnext:{kind}:{ref}", db_path=db,
    )
    return shared, live


def test_a_live_erpnext_alert_and_the_bundles_card_are_one_card_for_the_principal(board: dict[str, Any]) -> None:
    shared, live = _bundle_and_live(board["db"], "stock_below_reorder", "SKU-001@Stores - AC")
    other_shared, _ = _bundle_and_live(board["db"], "overdue_receivable", "ACC-SINV-2026-00042")
    lonely = alert_store.insert_alert(
        source="erpnext", external_id="demo-reorder-SKU-002", severity="medium", headline="Only the bundle's",
        body="No live twin.", dedup_key="erpnext:stock_below_reorder:SKU-002@Stores - AC", db_path=board["db"],
    )
    client = _make_client()
    mine = {p["alert_id"] for p in client.get("/today", headers=_as("pat@example.com")).json()["proposals"]}
    assert live in mine and shared not in mine and other_shared not in mine
    assert lonely in mine
    # A teammate never sees the private one, so keeps the bundle's card.
    theirs = {p["alert_id"] for p in client.get("/today", headers=_as("dana@example.com")).json()["proposals"]}
    assert shared in theirs and other_shared in theirs and live not in theirs
    text = str(client.get("/today", headers=_as("dana@example.com")).json())
    assert "live figures" not in text and "Live: " not in text
    # Left off Home, the shared card still opens on its own page, for anyone it waits for.
    assert client.get(f"/alerts/{shared}/card", headers=_as("pat@example.com")).status_code == 200
    assert client.get(f"/alerts/{live}/card", headers=_as("pat@example.com")).status_code == 200


def test_the_bundles_card_comes_back_once_the_live_alert_is_closed(board: dict[str, Any]) -> None:
    shared, live = _bundle_and_live(board["db"], "stock_below_reorder", "SKU-001@Stores - AC")
    alert_store.set_status(live, "resolved", db_path=board["db"])
    mine = {p["alert_id"] for p in _make_client().get("/today", headers=_as("pat@example.com")).json()["proposals"]}
    assert shared in mine and live not in mine


def test_only_erpnext_twins_fold(board: dict[str, Any]) -> None:
    shared = alert_store.insert_alert(
        source="system", external_id="s", severity="high", headline="Shared", body="b",
        dedup_key="erpnext:stock_below_reorder:X", db_path=board["db"],
    )
    alert_store.insert_alert(
        source="system", external_id="p", severity="high", headline="Private", body="b",
        topic_tags=[PRIVATE_ALERT_TAG], dedup_key="private:erpnext:stock_below_reorder:X", db_path=board["db"],
    )
    mine = {p["alert_id"] for p in _make_client().get("/today", headers=_as("pat@example.com")).json()["proposals"]}
    assert shared in mine
