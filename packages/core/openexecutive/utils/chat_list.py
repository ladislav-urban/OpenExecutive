"""How the Chats list groups conversations, worked out for the caller's zone.

The web app's Chats page and a host app's own Chats screen read these as
fields (``SessionSummary.date_group``, ``POST /sessions/topics/layout``)
instead of each holding a copy of the rule:

- each chat's date group: Pinned, Today, Yesterday, the previous 7 days, the
  previous 30, Older (``date_group``);
- the topic view: pinned chats, then each topic (or an area over two or more
  related topics) in a band by how long since its last chat, then Other
  (``layout_topics``);
- when a topic was last active, in a word (``active_label``).

Pure functions over plain mappings; the route passes the caller's own chats
and topics.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any, Literal

from pydantic import BaseModel, Field

DateGroupKey = Literal["pinned", "today", "yesterday", "prev7", "prev30", "older"]
BandKey = Literal["active", "week", "twoWeeks", "month"]

_DATE_LABELS: dict[str, str] = {
    "pinned": "Pinned",
    "today": "Today",
    "yesterday": "Yesterday",
    "prev7": "Previous 7 Days",
    "prev30": "Previous 30 Days",
    "older": "Older",
}
DATE_GROUP_ORDER: tuple[str, ...] = tuple(_DATE_LABELS)

# Bands of the topic view, by whole days since the topic's last chat.
_BANDS: tuple[tuple[str, str, float], ...] = (
    ("active", "Active this week", 7),
    ("week", "Quiet for a week", 14),
    ("twoWeeks", "Quiet for 2 weeks", 30),
    ("month", "Quiet for a month or more", math.inf),
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class DateGroup(BaseModel):
    key: DateGroupKey
    label: str
    # Where the group sits in the list (Pinned first).
    order: int


class TopicGroup(BaseModel):
    """Pinned, one topic, or Other, with its chats newest first."""

    # "pinned", "topic:<id>" or "other".
    key: str
    label: str
    # The topic, for its menu; absent on Pinned and Other.
    topic_id: int | None = None
    session_ids: list[str]
    # When its newest chat was last active, in a word ("Today", "Tue", "Oct 3").
    active_label: str = ""


class TopicEntry(BaseModel):
    """A heading in a band: one topic, or an area over two or more related ones."""

    # "topic:<id>" or "area:<name>".
    key: str
    label: str
    topic_id: int | None = None
    # The area's name, for its menu; absent on a topic.
    area: str | None = None
    # An area's topics, most recently active first.
    children: list[TopicGroup] = Field(default_factory=list)
    # Every chat under it, newest first.
    session_ids: list[str]
    last_active: str
    active_label: str = ""


class TopicBand(BaseModel):
    key: BandKey
    label: str
    entries: list[TopicEntry]


class TopicLayout(BaseModel):
    pinned: TopicGroup | None = None
    bands: list[TopicBand] = Field(default_factory=list)
    other: TopicGroup | None = None


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A stamp without an offset is the database's UTC.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _ts(iso: str | None) -> float:
    parsed = _parse(iso)
    return parsed.timestamp() if parsed is not None else 0.0


def _start_of_today(now: datetime, zone: tzinfo) -> datetime:
    local = now.astimezone(zone)
    return datetime.combine(local.date(), time(0), tzinfo=zone)


def date_group(updated_at: str | None, pinned: bool, now: datetime, zone: tzinfo) -> DateGroup:
    """Pinned chats first, whatever their date; then by the day of their last
    activity in ``zone``. An unreadable stamp is Older."""
    if pinned:
        key = "pinned"
    else:
        at = _parse(updated_at)
        today = _start_of_today(now, zone)
        if at is None:
            key = "older"
        elif at >= today:
            key = "today"
        elif at >= today - timedelta(days=1):
            key = "yesterday"
        elif at >= today - timedelta(days=7):
            key = "prev7"
        elif at >= today - timedelta(days=30):
            key = "prev30"
        else:
            key = "older"
    return DateGroup(key=key, label=_DATE_LABELS[key], order=DATE_GROUP_ORDER.index(key))  # type: ignore[arg-type]


def active_label(iso: str | None, now: datetime, zone: tzinfo) -> str:
    """When something was last active, in a word: Today, Yesterday, the
    weekday this week, else the date (with the year when not this one)."""
    at = _parse(iso)
    if at is None:
        return ""
    today = _start_of_today(now, zone)
    if at >= today:
        return "Today"
    days = math.ceil((today - at).total_seconds() / 86400)
    if days <= 1:
        return "Yesterday"
    local = at.astimezone(zone)
    if days < 7:
        return _WEEKDAYS[local.weekday()]
    label = f"{_MONTHS[local.month - 1]} {local.day}"
    return label if local.year == now.astimezone(zone).year else f"{label}, {local.year}"


def _group_by_topic(
    sessions: Sequence[Mapping[str, Any]], topics: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Pinned chats first; topics in the order listed (a topic with one chat
    here folds into Other unless its owner kept or pinned it); Other last,
    as the list came."""
    pinned: list[Mapping[str, Any]] = []
    by_topic: dict[int, list[Mapping[str, Any]]] = {}
    other: list[Mapping[str, Any]] = []
    known = {t["id"] for t in topics}
    for s in sessions:
        if s.get("pinned_at"):
            pinned.append(s)
        elif s.get("topic_id") is not None and s["topic_id"] in known:
            by_topic.setdefault(s["topic_id"], []).append(s)
        else:
            other.append(s)
    groups: list[dict[str, Any]] = []
    if pinned:
        groups.append({"key": "pinned", "label": "Pinned", "topic": None, "items": pinned})
    for topic in topics:
        items = by_topic.get(topic["id"])
        if not items:
            continue
        if len(items) == 1 and not topic.get("kept") and not topic.get("pinned_at"):
            other.append(items[0])
            continue
        groups.append({"key": f"topic:{topic['id']}", "label": topic["name"], "topic": topic, "items": items})
    if other:
        order = {s["session_id"]: i for i, s in enumerate(sessions)}
        other.sort(key=lambda s: order.get(s["session_id"], 0))
        groups.append({"key": "other", "label": "Other", "topic": None, "items": other})
    return groups


def _newest(items: Sequence[Mapping[str, Any]]) -> str:
    best = items[0].get("updated_at", "") if items else ""
    for s in items:
        if _ts(s.get("updated_at")) > _ts(best):
            best = s.get("updated_at", "")
    return best or ""


_SEPARATORS = " \t:·–—-"


def _within_area(name: str, area: str) -> str:
    """Under its area a topic drops the area's name it starts with ("St.
    Albans Offers" reads "Offers"), but only a whole word ("Boardwalk Permit"
    stays as is under "Board")."""
    if not name.lower().startswith(area.lower()):
        return name
    after = name[len(area):]
    if after and after[0] not in _SEPARATORS:
        return name
    rest = after.lstrip(_SEPARATORS)
    return rest[0].upper() + rest[1:] if rest else name


def layout_topics(
    sessions: Sequence[Mapping[str, Any]],
    topics: Sequence[Mapping[str, Any]],
    now: datetime,
    zone: tzinfo,
) -> TopicLayout:
    """The topic view, kept short as topics pile up: pinned chats, then each
    topic (or an area over two or more related topics that still show; a lone
    one stands as its topic) in a band by how long since its last chat, then
    Other. A pinned topic stays in the active band. Within a band, pinned
    first, then the most recently active."""

    def ids(items: Sequence[Mapping[str, Any]]) -> list[str]:
        return [str(s["session_id"]) for s in items]

    def group_out(g: dict[str, Any], label: str | None = None) -> TopicGroup:
        return TopicGroup(
            key=g["key"], label=label if label is not None else g["label"],
            topic_id=g["topic"]["id"] if g["topic"] else None, session_ids=ids(g["items"]),
            active_label=active_label(g["items"][0].get("updated_at") if g["items"] else None, now, zone),
        )

    layout = TopicLayout()
    by_area: dict[str, list[dict[str, Any]]] = {}
    loose: list[dict[str, Any]] = []
    for g in _group_by_topic(sessions, topics):
        if g["key"] == "pinned":
            layout.pinned = group_out(g)
        elif g["key"] == "other":
            layout.other = group_out(g)
        elif g["topic"] and g["topic"].get("area"):
            by_area.setdefault(str(g["topic"]["area"]).lower(), []).append(g)
        else:
            loose.append(g)

    # (entry, pinned?) pairs.
    entries: list[tuple[TopicEntry, bool]] = []

    def topic_entry(g: dict[str, Any]) -> tuple[TopicEntry, bool]:
        last = _newest(g["items"])
        entry = TopicEntry(
            key=g["key"], label=g["label"], topic_id=g["topic"]["id"], session_ids=ids(g["items"]),
            last_active=last, active_label=active_label(g["items"][0].get("updated_at"), now, zone),
        )
        return entry, bool(g["topic"].get("pinned_at"))

    entries.extend(topic_entry(g) for g in loose)
    for members in by_area.values():
        if len(members) < 2:
            entries.extend(topic_entry(g) for g in members)
            continue
        area = members[0]["topic"].get("area") or members[0]["label"]
        children = sorted(members, key=lambda g: _ts(_newest(g["items"])), reverse=True)
        items = sorted(
            (s for g in children for s in g["items"]), key=lambda s: _ts(s.get("updated_at")), reverse=True,
        )
        last = _newest(items)
        entries.append((
            TopicEntry(
                key=f"area:{area.lower()}", label=area, area=area,
                children=[group_out(g, _within_area(g["label"], area)) for g in children],
                session_ids=ids(items), last_active=last, active_label=active_label(last, now, zone),
            ),
            any(g["topic"].get("pinned_at") for g in children),
        ))

    today = _start_of_today(now, zone)

    def band_of(entry: TopicEntry, pinned: bool) -> str:
        if pinned:
            return "active"
        at = _parse(entry.last_active)
        stamp = at.timestamp() if at is not None else 0.0
        days = 0 if stamp >= today.timestamp() else math.ceil((today.timestamp() - stamp) / 86400)
        return next(key for key, _, under in _BANDS if days < under)

    for key, label, _ in _BANDS:
        in_band = [(e, p) for e, p in entries if band_of(e, p) == key]
        # Stable: pinned first, then the most recently active.
        in_band.sort(key=lambda ep: (not ep[1], -_ts(ep[0].last_active)))
        if in_band:
            layout.bands.append(TopicBand(key=key, label=label, entries=[e for e, _ in in_band]))  # type: ignore[arg-type]
    return layout

