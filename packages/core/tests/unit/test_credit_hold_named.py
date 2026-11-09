"""A credit hold names who it is for (integrations/erpnext_events.py,
alerts.models.named_people): the ERPNext users oe_connector checked may read
it, matched to active team members on People. The alert stays private: it
reaches only those people and the principal, on their own Home, the alert's
own route and their own private chat, never a broadcast, a department or
anyone's brief. A tag the triage model writes never names anyone."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import (
    PRIVATE_ALERT_TAG,
    AlertChannel,
    AlertEvent,
    AlertSeverity,
    TriageDecision,
    named_for,
    named_people,
)
from openexecutive.integrations import erpnext_events


@pytest.fixture
def roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from openexecutive.memory import episodic
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    db = tmp_path / "named.db"
    for module in (alert_store, people_store, episodic):
        monkeypatch.setattr(module, "DB_PATH", db)
    alert_store.initialize_db(db)
    people_store.initialize_db(db)
    principal = people_store.upsert_person(full_name="Pat Principal", is_principal=True, email="pat@co.com")
    sara = people_store.upsert_person(full_name="Sara Sales", email="sara@co.com")
    people_store.set_person_emails(sara, ["s.sales@co.com"])
    ben = people_store.upsert_person(full_name="Ben Credit", email="ben@co.com")
    gone = people_store.upsert_person(full_name="Gone Person", email="gone@co.com")
    people_store.archive_person(gone)
    other = people_store.upsert_person(full_name="Olga Other", email="olga@co.com")
    people_registry.invalidate()
    return SimpleNamespace(db=db, principal=principal, sara=sara, ben=ben, gone=gone, other=other)


def _event(**over: Any) -> dict[str, Any]:
    return {"kind": "credit_hold", "key": "Aurora:SO-0001", "title": "SO-0001 is over its credit limit", **over}


def test_only_a_credit_hold_carries_its_readers_at_most_five_well_formed() -> None:
    users = ["A@co.com", "a@co.com", "not an address", 7, "b@co.com", "c@co.com", "d@co.com", "e@co.com", "f@co.com"]
    events = erpnext_events.parse_events({"events": [
        _event(for_users=users),
        {"kind": "overdue_receivable", "key": "SINV-1", "title": "Overdue", "for_users": ["a@co.com"]},
    ]})
    assert events[0]["for_users"] == ["a@co.com", "b@co.com", "c@co.com", "d@co.com", "e@co.com"]
    assert events[1]["for_users"] == []


def test_readers_match_active_team_members_by_email_or_alias(roster: SimpleNamespace) -> None:
    ids = erpnext_events.named_person_ids(
        ["s.sales@co.com", "ben@co.com", "gone@co.com", "pat@co.com", "stranger@x.com"],
    )
    # The archived person, the principal (theirs anyway) and anyone not on People get nothing.
    assert ids == [roster.sara, roster.ben]


def _stub_triage(monkeypatch: pytest.MonkeyPatch, tags: list[str]) -> None:
    from openexecutive.agents import triage as triage_module

    async def fake_triage(self: Any, event: Any, **_: Any) -> TriageDecision:
        return TriageDecision(
            alert=True, severity=AlertSeverity.HIGH, channels=[AlertChannel.PERSISTED, AlertChannel.COMPANY_BROADCAST],
            headline="Credit hold", body="SO-0001 is over the limit.", topic_tags=tags, dedup_key="k",
        )

    monkeypatch.setattr(triage_module.TriageAgent, "triage", fake_triage)


def _run(event: AlertEvent, monkeypatch: pytest.MonkeyPatch, db: Path) -> tuple[Any, list[Any], list[Any]]:
    from openexecutive.alerts import dispatcher, pipeline

    dispatched: list[Any] = []
    pushed: list[Any] = []

    async def _dispatch(alert: Any, channels: Any, **_: Any) -> None:
        dispatched.append(list(channels))

    async def _push(alert: Any, ids: list[int]) -> int:
        pushed.append(list(ids))
        return len(ids)

    monkeypatch.setattr(dispatcher, "dispatch_all", _dispatch)
    monkeypatch.setattr(dispatcher, "push_to_named", _push)
    _, alert_id = asyncio.run(pipeline.evaluate_and_dispatch(event, db_path=db))
    return alert_store.get_alert(alert_id, db_path=db), dispatched, pushed


def test_a_named_alert_stays_private_and_is_for_the_named_people_too(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_triage(monkeypatch, ["finance", f"named:{roster.other}"])
    alert, dispatched, pushed = _run(
        AlertEvent(source="erpnext", external_id="e1", private=True, named_person_ids=[roster.sara, roster.ben]),
        monkeypatch, roster.db,
    )
    assert PRIVATE_ALERT_TAG in alert.topic_tags
    # Triage's own "named:" tag is dropped; only the event's list counts.
    assert named_people(alert) == {roster.sara, roster.ben}
    # Still the principal's card to act on; the named people only see it.
    assert alert.routed_to_person_id == roster.principal
    # Stored only: no broadcast, no live push; the named people get their own chat.
    assert dispatched == [[AlertChannel.PERSISTED]]
    assert pushed == [[roster.sara, roster.ben]]


def test_a_triage_written_named_tag_never_names_anyone(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_triage(monkeypatch, ["finance", f"named:{roster.other}"])
    private, _, pushed = _run(AlertEvent(source="erpnext", external_id="e2", private=True), monkeypatch, roster.db)
    shared, _, _ = _run(AlertEvent(source="erpnext", external_id="e3"), monkeypatch, roster.db)
    assert named_people(private) == frozenset() and pushed == []
    assert not any(str(t).startswith("named:") for t in [*private.topic_tags, *shared.topic_tags])


def test_only_a_private_erpnext_alert_can_name_anyone() -> None:
    tags = [PRIVATE_ALERT_TAG, "named:4"]
    assert named_for(SimpleNamespace(source="erpnext", topic_tags=tags), 4)
    assert not named_for(SimpleNamespace(source="email", topic_tags=tags), 4)
    assert not named_for(SimpleNamespace(source="erpnext", topic_tags=["named:4"]), 4)
    assert not named_for(SimpleNamespace(source="erpnext", topic_tags=tags), None)


def _named_alert(roster: SimpleNamespace) -> int:
    alert_id = alert_store.insert_alert(
        source="erpnext", external_id="hold", severity="high", headline="SO-0001 is over its credit limit",
        body="b", topic_tags=["finance", PRIVATE_ALERT_TAG, f"named:{roster.sara}"],
        routed_to_person_id=roster.principal, db_path=roster.db,
    )
    assert alert_id is not None
    return alert_id


def test_home_shows_it_to_the_named_person_and_no_other_teammate(roster: SimpleNamespace) -> None:
    from datetime import UTC, datetime

    from openexecutive.api.routes.today import _board_alerts
    from openexecutive.orchestrator.artifact_records import viewer_for_person

    alert_id = _named_alert(roster)
    now = datetime.now(UTC)

    def ids(person: int, principal: bool = False) -> list[int]:
        return [a.id for a in _board_alerts(now, include_private=principal, viewer=viewer_for_person(person))]

    assert alert_id in ids(roster.sara)
    assert alert_id not in ids(roster.other)
    assert alert_id in ids(roster.principal, principal=True)
    # The briefs and the reflection read the board with no viewer.
    assert alert_id not in [a.id for a in _board_alerts(now, include_private=False, viewer=None)]


def test_a_named_person_may_look_but_not_act(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from openexecutive.api.routes import alerts as alerts_route
    from openexecutive.briefing import home_rules

    alert_id = _named_alert(roster)
    # The alert routes (ack, snooze, approve, answer, reopen) stay the principal's.
    monkeypatch.setattr(alerts_route, "_caller_is_principal", lambda _r: False)
    monkeypatch.setattr(alerts_route, "_caller_viewer", lambda _r: None)
    with pytest.raises(HTTPException) as refused:
        alerts_route._visible_alert(alert_id, object())  # type: ignore[arg-type]
    assert refused.value.status_code == 404
    # On their Home the card offers only what a look-only caller gets.
    class _Card(SimpleNamespace):
        def __getattr__(self, _name: str) -> Any:  # any field Home doesn't set here
            return None

    card = _Card(
        category="action", topic_tags=["finance", PRIVATE_ALERT_TAG, f"named:{roster.sara}"],
        decision_instance_id=None, review_verdict=None, routed_to_person_id=roster.principal,
        recommended_move="", failed_run_id=None, suggested_workflow=None, artifact_format="markdown",
        artifact_url=None, alert_id=alert_id, headline="h", record=None, roster_request=None,
        training=None, held_action=None, card_kind="proposal", body="b",
    )
    monkeypatch.setattr(home_rules, "card_kind", lambda _p: "proposal")
    keys = {a.key for a in home_rules.card_actions(
        card, lane_="team", caller_is_principal=False, people_by_id={}, snoozes=[],
        caller_is_demo=True, caller_id=roster.sara,
    )}
    assert not keys & {"approve", "approve_with_edits", "dismiss", "snooze"}


def test_a_different_set_of_readers_is_a_card_of_its_own(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_triage(monkeypatch, ["finance"])
    first, _, _ = _run(
        AlertEvent(source="erpnext", external_id="m1", private=True, named_person_ids=[roster.sara], dedup_hint="erpnext:credit_hold:k"),
        monkeypatch, roster.db,
    )
    second, _, pushed = _run(
        AlertEvent(source="erpnext", external_id="m2", private=True, named_person_ids=[roster.ben], dedup_hint="erpnext:credit_hold:k"),
        monkeypatch, roster.db,
    )
    assert second is not None and second.id != first.id
    assert named_people(second) == {roster.ben} and pushed == [[roster.ben]]


def test_a_demo_person_is_never_named(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.people import store as people_store

    with people_store._get_conn() as conn:
        conn.execute("UPDATE people SET directory_demo = 1 WHERE id = ?", (roster.ben,))
    assert erpnext_events.named_person_ids(["sara@co.com", "ben@co.com"]) == [roster.sara]


def test_the_push_goes_to_each_named_persons_own_chat_only(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.alerts import dispatcher
    from openexecutive.scheduler import runner

    alert = alert_store.get_alert(_named_alert(roster), db_path=roster.db)
    sent: list[tuple[str, bool]] = []

    async def _deliver(person: Any, text: str, *, label: str = "Update", chat_only: bool = False) -> Any:
        from openexecutive.audit.erpnext_reads import DELIVERING_ERPNEXT

        # Marked as ERPNext's figures, so the chat counts as having read ERPNext.
        assert DELIVERING_ERPNEXT.get() is True
        sent.append((person.email, chat_only))
        return runner.PrincipalDelivery(True, "slack_dm", "delivered", "slack_dm")

    monkeypatch.setattr(runner, "deliver_to_person", _deliver)
    monkeypatch.setattr("openexecutive.people.channel.is_within_availability", lambda *_a, **_k: True)
    # Someone the alert doesn't name is never sent it, whatever the caller passes.
    assert asyncio.run(dispatcher.push_to_named(alert, [roster.sara, roster.other])) == 1
    assert sent == [("sara@co.com", True)]
