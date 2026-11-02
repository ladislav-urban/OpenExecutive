"""Home's rules (briefing.home_rules), as GET /today gives them to every client.

The lanes, the older-than offer, the summary line, the handled grouping, what
each card offers, the snooze times, the tiles and the quiet day. The summary,
handled and snooze cases are the web app's former tests of the same rules
(scripts/briefingSummary.test.mjs, handled.test.mjs, snooze.test.mjs).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from openexecutive.briefing import home_rules as hr

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)  # a Tuesday


def proposal(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "alert_id": 1,
        "headline": "Renew the Acme contract",
        "body": "Renew the Acme contract before it lapses.",
        "routed_to_person_id": None,
        "suggested_action": "Draft the renewal.",
        "created_at": (NOW - timedelta(days=1)).isoformat(),
        "topic_tags": [],
        "category": "action",
        "decision_instance_id": None,
        "failed_run_id": None,
        "roster_request": None,
        "last_seen_at": None,
        "review_verdict": "",
        "recommended_move": "",
        "suggested_workflow": "",
        "artifact_format": None,
        "artifact_url": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def person(pid: int, name: str, **over: Any) -> SimpleNamespace:
    base = {"id": pid, "full_name": name, "status": "clear", "overdue": False, "awaiting_count": 0}
    base.update(over)
    return SimpleNamespace(**base)


def dept(**over: Any) -> SimpleNamespace:
    base = {"goal_count": 1, "awaiting_count": 0, "at_risk_count": 0, "off_track_count": 0}
    base.update(over)
    return SimpleNamespace(**base)


def keys(actions: list[hr.CardAction]) -> list[str]:
    return [a.key for a in actions]


def offered(p: SimpleNamespace, *, lane: hr.Lane = "needs_you", principal: bool = True,
            people: dict[int, Any] | None = None) -> list[hr.CardAction]:
    snoozes = hr.snooze_choices(NOW, UTC)
    return hr.card_actions(p, lane_=lane, caller_is_principal=principal, people_by_id=people or {}, snoozes=snoozes)


# --------------------------------------------------------------------------- #
# Lanes and kinds
# --------------------------------------------------------------------------- #

def test_lanes_split_mine_the_team_and_monitoring() -> None:
    def lane(p: SimpleNamespace, caller: int | None = 2, principal: bool = False, solo: bool = False) -> str:
        return hr.lane(p, caller_id=caller, caller_is_principal=principal, solo=solo)

    assert lane(proposal(category="monitoring", routed_to_person_id=2)) == "monitoring"
    assert lane(proposal(routed_to_person_id=2)) == "needs_you"
    assert lane(proposal(routed_to_person_id=3)) == "team"
    # Unrouted cards are the principal's.
    assert lane(proposal()) == "team"
    assert lane(proposal(), principal=True) == "needs_you"
    # Solo, and a caller nobody resolved, see every card as their own.
    assert lane(proposal(routed_to_person_id=3), solo=True) == "needs_you"
    assert lane(proposal(routed_to_person_id=3), caller=None) == "needs_you"


def test_card_kinds() -> None:
    assert hr.card_kind(proposal()) == "proposal"
    assert hr.card_kind(proposal(roster_request=object())) == "roster_request"
    assert hr.card_kind(proposal(decision_instance_id=4, topic_tags=[hr.LEAD_TRAINING_TAG])) == "lead_training"
    assert hr.card_kind(proposal(decision_instance_id=4, topic_tags=[hr.HELD_ACTION_TAG])) == "held_action"
    # The tag alone, without a decision, is an ordinary proposal.
    assert hr.card_kind(proposal(topic_tags=[hr.HELD_ACTION_TAG])) == "proposal"


# --------------------------------------------------------------------------- #
# What a card offers
# --------------------------------------------------------------------------- #

def test_a_plain_proposal_leads_with_approve() -> None:
    actions = offered(proposal())
    assert keys(actions) == [
        "approve", "discuss", "approve_with_edits", "snooze", "snooze", "snooze", "dismiss",
    ]
    assert actions[0].primary and actions[0].label == "Approve"
    assert [a.primary for a in actions[1:]] == [False] * 6
    discuss = actions[1]
    assert discuss.prompt and discuss.prompt.startswith("Tell me about this proposal:\n\nRenew the Acme contract")
    assert "If I approve, you will:\nDraft the renewal." in discuss.prompt
    assert discuss.memory_text == 'Tell me about this proposal.\n"Renew the Acme contract"'


def test_a_teammates_card_offers_approve_to_the_principal_only() -> None:
    assert "approve" in keys(offered(proposal(routed_to_person_id=3), lane="team", principal=True))
    teammate = offered(proposal(routed_to_person_id=3), lane="team", principal=False)
    assert "approve" not in keys(teammate) and "approve_with_edits" not in keys(teammate)
    assert keys(teammate)[0] == "discuss"


def test_a_failed_run_leads_with_run_again() -> None:
    actions = offered(proposal(failed_run_id="run 7"))
    assert keys(actions)[:3] == ["run_again", "discuss", "open_run"]
    assert actions[2].href == "/jobs/runs/run%207"
    assert "approve" not in keys(actions)


def test_recommended_moves_and_a_stale_card() -> None:
    people = {3: person(3, "Dana Kim")}
    nudge = offered(proposal(routed_to_person_id=3, recommended_move="nudge"), people=people)
    assert nudge[0].key == "nudge" and nudge[0].label == "Nudge Dana"
    assert nudge[0].prompt and nudge[0].prompt.startswith("Nudge Dana Kim about this item — it has gone quiet:")
    assert nudge[0].memory_text == 'Nudge Dana Kim about this item.\n"Renew the Acme contract"'
    assert nudge[-1].key == "open_person" and nudge[-1].href == "/people/3" and nudge[-1].label == "Open Dana's page"
    workflow = offered(proposal(recommended_move="suggest_workflow", suggested_workflow="renewal_prep"))
    assert workflow[0].label == "Run renewal prep" and workflow[0].href == "/jobs/renewal_prep"
    stale = offered(proposal(review_verdict="likely_stale"))
    assert stale[0].key == "dismiss" and stale[0].primary
    assert "dismiss" not in keys(stale[1:])


def test_a_decision_card_has_no_edit_and_its_own_discuss() -> None:
    actions = offered(proposal(decision_instance_id=9))
    assert "approve_with_edits" not in keys(actions)
    assert actions[1].prompt and actions[1].prompt.startswith("Let's talk through this meeting I've proposed:")


def test_monitoring_and_artifact_cards() -> None:
    monitoring = offered(proposal(category="monitoring"), lane="monitoring")
    assert keys(monitoring) == ["discuss", "snooze", "snooze", "snooze", "dismiss"]
    assert monitoring[-1].label == "Dismiss signal"
    assert monitoring[0].prompt and monitoring[0].prompt.startswith("Help me understand this signal we're monitoring:")
    artifact = offered(proposal(alert_id=5, topic_tags=["artifact"], artifact_format="link",
                                artifact_url="https://docs.example.com/x"))
    assert keys(artifact) == ["open_document", "discuss", "dismiss", "open_in_app"]
    assert artifact[0].href == "/artifacts/alert%3A5"
    assert artifact[2].label == "Mark reviewed"
    assert artifact[3].label == "Open in app (docs.example.com)" and artifact[3].external


def test_other_card_kinds_offer_nothing_here() -> None:
    assert offered(proposal(roster_request=object())) == []
    assert offered(proposal(decision_instance_id=4, topic_tags=[hr.HELD_ACTION_TAG])) == []


def test_a_long_subject_is_clipped_in_memory() -> None:
    line = hr.memory_line("Tell me about this proposal", "word " * 200)
    assert line.endswith('…"') and len(line.split("\n")[1]) == 302


# --------------------------------------------------------------------------- #
# Snooze
# --------------------------------------------------------------------------- #

def _local(zone: ZoneInfo, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=zone)


@pytest.mark.parametrize("zone", [ZoneInfo("UTC"), ZoneInfo("America/Los_Angeles"), ZoneInfo("Asia/Tokyo")])
def test_a_weekday_afternoon_offers_later_today_tomorrow_and_monday(zone: ZoneInfo) -> None:
    choices = hr.snooze_choices(_local(zone, 13, 13, 20), zone)  # Tue 13:20 there
    assert [datetime.fromisoformat(c.until) for c in choices] == [
        _local(zone, 13, 16, 30), _local(zone, 14, 9), _local(zone, 19, 9),
    ]
    assert [c.label for c in choices] == [
        "Snooze until 4:30 PM", "Snooze until tomorrow 9:00 AM", "Snooze until Monday 9:00 AM",
    ]
    assert [c.short for c in choices] == ["4:30 PM", "tomorrow 9:00 AM", "Monday 9:00 AM"]


def test_late_in_the_day_drops_later_today() -> None:
    zone = ZoneInfo("Europe/Prague")
    choices = hr.snooze_choices(_local(zone, 13, 22), zone)
    assert len(choices) == 2
    assert datetime.fromisoformat(choices[0].until) == _local(zone, 14, 9)


def test_the_choices_follow_the_callers_zone_not_utc() -> None:
    # 23:00 UTC Tuesday is 16:00 Tuesday in Los Angeles: later today is still on.
    zone = ZoneInfo("America/Los_Angeles")
    choices = hr.snooze_choices(datetime(2026, 10, 13, 23, 0, tzinfo=UTC), zone)
    assert datetime.fromisoformat(choices[0].until) == _local(zone, 13, 19)


def test_monday_points_a_week_on_and_sunday_skips_it() -> None:
    zone = ZoneInfo("UTC")
    monday = hr.snooze_choices(_local(zone, 12, 10), zone)
    assert datetime.fromisoformat(monday[-1].until) == _local(zone, 19, 9)
    sunday = hr.snooze_choices(_local(zone, 18, 10), zone)
    assert all("Monday" not in c.label for c in sunday)
    assert datetime.fromisoformat(sunday[-1].until) == _local(zone, 19, 9)


def test_later_today_rounds_up_to_the_quarter_hour_and_past_the_hour() -> None:
    zone = ZoneInfo("UTC")
    choices = hr.snooze_choices(_local(zone, 13, 9, 50), zone)
    assert choices[0].short == "1:00 PM"


# --------------------------------------------------------------------------- #
# Older cards to dismiss
# --------------------------------------------------------------------------- #

def test_the_offer_takes_old_cards_but_never_a_roster_request() -> None:
    old = (NOW - timedelta(days=8)).isoformat()
    mine = [
        proposal(alert_id=1, created_at=old),
        # Re-fired yesterday: not old.
        proposal(alert_id=2, created_at=old, last_seen_at=(NOW - timedelta(days=1)).isoformat()),
        proposal(alert_id=3, created_at=old, roster_request=object()),
        proposal(alert_id=4),
    ]
    offer = hr.dismiss_offer(mine, NOW)
    assert offer.alert_ids == [1]
    assert offer.days == 7 and offer.label == "Dismiss 1 older than 7 days"


# --------------------------------------------------------------------------- #
# Handled rows
# --------------------------------------------------------------------------- #

def handled(kind: str, alert_id: int | None, *, at: str = "2026-09-13T06:00:00+00:00", **over: Any) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, alert_id=alert_id, at=at, superseded_by_alert_id=over.get("into"))


def test_a_newer_close_keeps_the_visible_slot_over_an_older_escalation() -> None:
    rows = hr.group_handled([
        handled("closed", 7, at="2026-09-13T07:00:00+00:00"),
        handled("escalated", 7, at="2026-09-13T01:00:00+00:00"),
    ])
    assert len(rows) == 1
    item, also, _ = rows[0]
    assert item.kind == "closed" and [a.kind for a in also] == ["escalated"]


def test_an_escalation_beats_a_newer_route_or_nudge() -> None:
    rows = hr.group_handled([
        handled("nudged", 3, at="2026-09-13T07:00:00+00:00"),
        handled("escalated", 3, at="2026-09-13T03:00:00+00:00"),
        handled("routed", 3, at="2026-09-13T01:00:00+00:00"),
    ])
    item, also, _ = rows[0]
    assert item.kind == "escalated" and [a.kind for a in also] == ["nudged", "routed"]


def test_merges_fold_into_a_listed_survivor_only() -> None:
    rows = hr.group_handled([handled("closed", 9), handled("merged", 10, into=9), handled("merged", 11, into=9)])
    assert len(rows) == 1 and rows[0][2] == 2
    alone = hr.group_handled([handled("merged", 10, into=99)])
    assert len(alone) == 1 and alone[0][0].kind == "merged"
    assert hr.group_handled([handled("merged", 7, into=7)]) == []


def test_rows_sort_escalations_then_closes_then_others_then_transparency() -> None:
    rows = hr.group_handled([handled("watching", None), handled("routed", 1), handled("closed", 2), handled("escalated", 3)])
    assert [r[0].kind for r in rows] == ["escalated", "closed", "routed", "watching"]


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

ZERO = {
    "needs_you": 0, "handled": 0, "people_overdue": 0, "people_need_reply": 0,
    "dept_at_risk": 0, "goals_at_risk": 0, "in_flight": 0, "monitoring": 0,
}


def test_an_all_clear_day_has_no_phrases() -> None:
    assert hr.summary(**ZERO) == []


def test_the_summary_leads_with_needs_you_then_handled() -> None:
    parts = hr.summary(**{**ZERO, "needs_you": 3, "handled": 12, "in_flight": 7, "dept_at_risk": 1})
    assert [p.text for p in parts] == [
        "3 wait for your decision", "The Executive handled 12 on its own", "1 department at risk", "7 under way",
    ]
    assert [p.count for p in parts] == [3, 12, 1, 7]
    assert parts[0].target.model_dump(exclude_none=True) == {"kind": "needs_you"}
    assert parts[1].target.model_dump(exclude_none=True) == {"kind": "panel", "panel": "handled"}


def test_singular_wording_people_and_the_solo_goals_link() -> None:
    parts = hr.summary(**{**ZERO, "needs_you": 1, "people_overdue": 2, "people_need_reply": 1,
                          "goals_at_risk": 1, "monitoring": 1})
    assert [p.text for p in parts] == [
        "1 waits for your decision", "2 overdue", "1 awaiting reply", "1 goal at risk", "1 signal monitored",
    ]
    assert parts[1].target.panel == "people"
    assert parts[3].target.model_dump(exclude_none=True) == {"kind": "href", "href": "/goals"}


# --------------------------------------------------------------------------- #
# Tiles and the quiet day
# --------------------------------------------------------------------------- #

def _tiles(**over: Any) -> list[hr.Tile]:
    args: dict[str, Any] = {
        "solo": False, "departments": [], "people": [], "team": 0, "in_flight": [],
        "handled": 0, "monitoring": 0, "clients": 0, "now": NOW,
    }
    args.update(over)
    return hr.tiles(**args)


def test_team_tiles() -> None:
    tiles = _tiles(
        departments=[dept(at_risk_count=1), dept(), dept(goal_count=0)],
        people=[person(1, "A", status="needs_reply", overdue=True), person(2, "B")],
        team=2,
        in_flight=[SimpleNamespace(overdue=False, run_at=(NOW + timedelta(hours=8)).isoformat())],
        handled=3, monitoring=1, clients=2,
    )
    assert [(t.key, t.value, t.sub, t.tone) for t in tiles] == [
        ("departments", "1", "at risk", "amber"),
        ("people", "1", "1 to reply · 1 overdue", "rose"),
        ("team", "2", "with teammates", None),
        ("inFlight", "1", "next in 8h", None),
        ("handled", "3", "by the Executive", "emerald"),
        ("monitoring", "1", "signal", None),
        ("clients", "2", "parked clients", None),
    ]


def test_departments_on_track_or_not_set_up_and_one_person_has_no_tile() -> None:
    assert [(t.value, t.sub, t.tone) for t in _tiles(departments=[dept(), dept()], people=[person(1, "A")])] == [
        ("2", "on track", "emerald"),
    ]
    assert [(t.value, t.sub, t.tone) for t in _tiles(departments=[dept(goal_count=0)])] == [
        ("1", "not set up yet", None),
    ]
    overdue = _tiles(in_flight=[SimpleNamespace(overdue=True, run_at=NOW.isoformat())])
    assert (overdue[1].sub, overdue[1].tone) == ("1 overdue", "amber")


def test_solo_has_no_department_or_people_tile() -> None:
    tiles = _tiles(solo=True, departments=[dept(at_risk_count=1)], people=[person(1, "A"), person(2, "B")], handled=1)
    assert [t.key for t in tiles] == ["handled"]


def test_a_quiet_day_needs_a_clear_board_and_nobody_waiting() -> None:
    clear = {"proposals": 0, "waiting_cards": 0, "departments": [dept()], "people": [person(1, "A")]}
    assert hr.quiet(**clear)
    assert not hr.quiet(**{**clear, "proposals": 1})
    assert not hr.quiet(**{**clear, "waiting_cards": 1})
    assert not hr.quiet(**{**clear, "departments": [dept(awaiting_count=1)]})
    assert not hr.quiet(**{**clear, "people": [person(1, "A", awaiting_count=2)]})
    # No department set up is still a quiet day.
    assert hr.quiet(**{**clear, "departments": [dept(goal_count=0)]})


def test_the_banner_needs_a_department_set_up_and_not_the_first_week() -> None:
    assert hr.quiet_banner(quiet_day=True, departments=[dept()], first_week=False)
    assert not hr.quiet_banner(quiet_day=False, departments=[dept()], first_week=False)
    assert not hr.quiet_banner(quiet_day=True, departments=[dept(goal_count=0)], first_week=False)
    assert not hr.quiet_banner(quiet_day=True, departments=[dept()], first_week=True)


# --------------------------------------------------------------------------- #
# Cards counted from elsewhere
# --------------------------------------------------------------------------- #

def test_waiting_cards_add_up_and_a_failing_counter_counts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hr, "_WAITING_CARDS", {})

    def broken(_person: Any) -> int:
        raise RuntimeError("down")

    hr.register_waiting_cards("a", lambda _p: 2)
    hr.register_waiting_cards("b", broken)
    hr.register_waiting_cards("c", lambda _p: 1)
    assert hr.waiting_card_count(SimpleNamespace(id=1)) == 3
    assert hr.waiting_card_count(None) == 0


# --------------------------------------------------------------------------- #
# A demo person (people.demo): the cards nobody owns are theirs to look at
# --------------------------------------------------------------------------- #

def test_a_card_nobody_owns_is_in_a_demo_persons_needs_you() -> None:
    unrouted = proposal()
    assert hr.lane(unrouted, caller_id=7, caller_is_principal=False, solo=False, caller_is_demo=True) == "needs_you"
    assert hr.lane(unrouted, caller_id=7, caller_is_principal=False, solo=False) == "team"
    # Someone else's card stays the team's.
    theirs = proposal(routed_to_person_id=3)
    assert hr.lane(theirs, caller_id=7, caller_is_principal=False, solo=False, caller_is_demo=True) == "team"


def test_a_demo_person_only_looks_at_a_card_that_isnt_theirs() -> None:
    def demo_actions(p: SimpleNamespace, lane: str) -> list[str]:
        return keys(hr.card_actions(
            p, lane_=lane, caller_is_principal=False, people_by_id={3: person(3, "Ben Teammate")},  # type: ignore[arg-type]
            snoozes=hr.snooze_choices(NOW, ZoneInfo("UTC")), caller_is_demo=True, caller_id=7,
        ))

    assert demo_actions(proposal(recommended_move="nudge"), "needs_you") == ["discuss"]
    assert demo_actions(proposal(routed_to_person_id=3), "team") == ["discuss"]
    assert demo_actions(proposal(category="monitoring"), "monitoring") == ["discuss"]
    doc = proposal(topic_tags=["artifact"], artifact_format="link", artifact_url="https://docs.example/x")
    assert demo_actions(doc, "needs_you") == ["open_document", "discuss", "open_in_app"]
    # Their own card is theirs to answer, but they approve only a decision of
    # theirs (an ERPNext draft), as the approval routes allow.
    own = demo_actions(proposal(routed_to_person_id=7), "needs_you")
    assert "dismiss" in own and "approve" not in own and "approve_with_edits" not in own
    assert "approve" in demo_actions(proposal(routed_to_person_id=7, decision_instance_id=5), "needs_you")
    # Solo puts every card in Needs you: someone else's is still only looked at.
    assert demo_actions(proposal(routed_to_person_id=3), "needs_you") == ["discuss"]


# An ERP alert's record (proposal.record): Open for everyone who sees the card.

def test_an_erp_alerts_card_offers_open_first_in_the_menu() -> None:
    record = SimpleNamespace(doctype="Sales Invoice", name="ACC-SINV-2026-00042")
    actions = offered(proposal(record=record))
    assert keys(actions)[:3] == ["approve", "discuss", "open_record"]
    opened = actions[2]
    assert opened.label == "Open" and not opened.primary and opened.href is None
    assert "open_record" not in keys(offered(proposal()))


def test_a_demo_person_gets_open_beside_discuss_never_as_primary() -> None:
    record = SimpleNamespace(doctype="Item", name="DEMO-BRK-16")
    actions = hr.card_actions(
        proposal(record=record), lane_="needs_you", caller_is_principal=False, people_by_id={},
        snoozes=hr.snooze_choices(NOW, ZoneInfo("UTC")), caller_is_demo=True, caller_id=7,
    )
    assert keys(actions) == ["discuss", "open_record"]
    assert not any(a.primary for a in actions)
