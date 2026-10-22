"""The Chats list's grouping (utils.chat_list), as the API gives it.

The cases are the web app's former tests of the same rules
(scripts/sessionGroups.test.mjs), with "local time" made the caller's zone.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openexecutive.utils import chat_list as cl

ZONE = ZoneInfo("Europe/Prague")


def at(day: int, hour: int = 12, *, month: int = 9) -> str:
    return datetime(2026, month, day, hour, tzinfo=ZONE).isoformat()


def chat(session_id: str, updated_at: str, **extra: Any) -> dict[str, Any]:
    return {"session_id": session_id, "title": session_id, "created_at": updated_at, "updated_at": updated_at, **extra}


def test_chats_land_in_today_yesterday_7_30_older_by_the_callers_day() -> None:
    now = datetime(2026, 9, 24, 15, tzinfo=ZONE)
    cases = {"today": at(24, 0), "yesterday": at(23, 23), "prev7": at(17), "prev30": at(25, month=8), "older": at(24, month=8)}
    for key, when in cases.items():
        assert cl.date_group(when, False, now, ZONE).key == key, key
    assert cl.date_group(at(24, 0), False, now, ZONE).label == "Today"


def test_the_day_is_the_callers_not_utcs() -> None:
    # 23:30 UTC on the 23rd is already the 24th in Prague.
    now = datetime(2026, 9, 24, 10, tzinfo=ZONE)
    late = "2026-09-23T23:30:00+00:00"
    assert cl.date_group(late, False, now, ZONE).key == "today"
    assert cl.date_group(late, False, now, ZoneInfo("UTC")).key == "yesterday"


def test_pinned_first_whatever_the_date_and_a_bad_stamp_is_older() -> None:
    now = datetime(2026, 9, 24, 15, tzinfo=ZONE)
    pinned = cl.date_group(at(1, month=1), True, now, ZONE)
    assert (pinned.key, pinned.label, pinned.order) == ("pinned", "Pinned", 0)
    assert cl.date_group("not-a-date", False, now, ZONE).key == "older"
    assert [cl.date_group(at(24), False, now, ZONE).order, cl.date_group(at(1), False, now, ZONE).order, cl.date_group(at(1, month=8), False, now, ZONE).order] == [1, 4, 5]


def _layout(sessions: list[dict[str, Any]], topics: list[dict[str, Any]], now: datetime) -> cl.TopicLayout:
    return cl.layout_topics(sessions, topics, now, ZONE)


def test_topic_view_pinned_first_then_topics_one_offs_under_other() -> None:
    when = at(1, month=10)
    topics = [
        {"id": 1, "name": "Hiring a sales lead", "kept": False},
        {"id": 2, "name": "Acme renewal", "kept": False},
        {"id": 3, "name": "Board prep", "kept": True},
    ]
    sessions = [
        chat("pinned", when, topic_id=1, pinned_at=when),
        chat("hire-a", when, topic_id=1),
        chat("loose", when),
        chat("acme", when, topic_id=2),
        chat("hire-b", when, topic_id=1),
        chat("board", when, topic_id=3),
        chat("gone", when, topic_id=99),
    ]
    layout = _layout(sessions, topics, datetime(2026, 10, 2, 12, tzinfo=ZONE))
    assert layout.pinned is not None and layout.pinned.session_ids == ["pinned"]
    labels = [(e.label, e.session_ids, e.topic_id) for b in layout.bands for e in b.entries]
    assert sorted(labels) == sorted([("Hiring a sales lead", ["hire-a", "hire-b"], 1), ("Board prep", ["board"], 3)])
    # A one-chat topic nobody kept, no topic, or an unknown one: Other, as listed.
    assert layout.other is not None and layout.other.session_ids == ["loose", "acme", "gone"]
    assert layout.other.topic_id is None


def test_topic_layout_areas_bands_and_other() -> None:
    now = datetime(2026, 10, 17, 12, tzinfo=ZONE)

    def ago(n: int) -> str:
        return (datetime(2026, 10, 17, 9, tzinfo=ZONE) - timedelta(days=n)).isoformat()

    topics = [
        {"id": 1, "name": "St. Albans Disposition", "kept": False, "area": "St. Albans"},
        {"id": 2, "name": "St. Albans Offers", "kept": False, "area": "St. Albans"},
        {"id": 3, "name": "CAHEC Close", "kept": False},
        {"id": 4, "name": "Estes Park", "kept": False, "area": "Colorado"},
        {"id": 5, "name": "Insurance", "kept": False},
        {"id": 6, "name": "Old pinned", "kept": True, "pinned_at": ago(40)},
    ]
    sessions = [
        chat("d1", ago(0), topic_id=1), chat("o1", ago(1), topic_id=2), chat("c1", ago(2), topic_id=3),
        chat("d2", ago(3), topic_id=1), chat("c2", ago(4), topic_id=3), chat("o2", ago(5), topic_id=2),
        chat("e1", ago(9), topic_id=4), chat("e2", ago(10), topic_id=4),
        chat("i1", ago(45), topic_id=5), chat("i2", ago(50), topic_id=5),
        chat("p1", ago(60), topic_id=6), chat("p2", ago(61), topic_id=6),
        chat("loose", ago(2)),
    ]
    layout = _layout(sessions, topics, now)
    assert [(b.label, [e.label for e in b.entries]) for b in layout.bands] == [
        # A pinned topic stays up top, whatever its age.
        ("Active this week", ["Old pinned", "St. Albans", "CAHEC Close"]),
        # An area with one topic left is just that topic.
        ("Quiet for a week", ["Estes Park"]),
        ("Quiet for a month or more", ["Insurance"]),
    ]
    area = layout.bands[0].entries[1]
    assert area.area == "St. Albans" and area.key == "area:st. albans"
    # Under the area, a topic drops the area's name it starts with.
    assert [g.label for g in area.children] == ["Disposition", "Offers"]
    assert area.children[0].topic_id == 1
    assert area.session_ids == ["d1", "o1", "d2", "o2"]
    assert area.active_label == "Today"
    assert layout.other is not None and layout.other.session_ids == ["loose"]


def test_an_areas_name_comes_off_only_as_a_whole_word() -> None:
    now = datetime(2026, 10, 17, 12, tzinfo=ZONE)
    when = datetime(2026, 10, 16, 9, tzinfo=ZONE).isoformat()
    topics = [
        {"id": 1, "name": "Board: Q3 deck", "kept": False, "area": "Board"},
        {"id": 2, "name": "Boardwalk Permit", "kept": False, "area": "Board"},
    ]
    sessions = [chat(s, when, topic_id=1 if i < 2 else 2) for i, s in enumerate("abcd")]
    area = _layout(sessions, topics, now).bands[0].entries[0]
    assert sorted(g.label for g in area.children) == ["Boardwalk Permit", "Q3 deck"]


def test_six_days_ago_is_still_this_week_seven_is_quiet() -> None:
    now = datetime(2026, 10, 17, 12, tzinfo=ZONE)
    topics = [{"id": 1, "name": "Six", "kept": False}, {"id": 2, "name": "Seven", "kept": False}]

    def c(session_id: str, day: int, topic_id: int) -> dict[str, Any]:
        return chat(session_id, datetime(2026, 10, 17 - day, 9, tzinfo=ZONE).isoformat(), topic_id=topic_id)

    layout = _layout([c("s1", 6, 1), c("s2", 6, 1), c("v1", 7, 2), c("v2", 7, 2)], topics, now)
    assert [(b.label, [e.label for e in b.entries]) for b in layout.bands] == [
        ("Active this week", ["Six"]),
        ("Quiet for a week", ["Seven"]),
    ]


def test_active_label_today_yesterday_weekday_then_the_date() -> None:
    now = datetime(2026, 10, 17, 12, tzinfo=ZONE)
    assert cl.active_label(datetime(2026, 10, 17, 8, tzinfo=ZONE).isoformat(), now, ZONE) == "Today"
    assert cl.active_label(datetime(2026, 10, 16, 23, tzinfo=ZONE).isoformat(), now, ZONE) == "Yesterday"
    assert cl.active_label(datetime(2026, 10, 13, 9, tzinfo=ZONE).isoformat(), now, ZONE) == "Tue"
    assert cl.active_label(datetime(2026, 10, 3, 9, tzinfo=ZONE).isoformat(), now, ZONE) == "Oct 3"
    assert cl.active_label(datetime(2025, 12, 30, 9, tzinfo=ZONE).isoformat(), now, ZONE) == "Dec 30, 2025"
    assert cl.active_label("", now, ZONE) == ""
