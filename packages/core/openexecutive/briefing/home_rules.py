"""Home's rules, worked out for the caller of ``GET /today``.

The web app's Home and any other client of ``/today`` (a host app's own Home
screen) read these as fields instead of each holding a copy:

- which lane each proposal sits in (``lane``): the caller's own queue, a
  teammate's, or monitoring;
- what kind of card it is and what it offers (``card_kind``, ``actions``);
- the snooze times, in the caller's own time zone;
- the "dismiss the older ones" offer;
- the summary line under the greeting, the count tiles and the quiet day;
- the handled rows, grouped one per alert.

Pure functions over the ``/today`` models (duck-typed, so this module imports
none of them); ``api.routes.today`` calls them and fills the response.
Cards that live outside ``/today`` (Act as me's replies and actions, an
extension's drafts) count toward "Needs you" through
``register_waiting_cards``.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, time, timedelta, tzinfo
from typing import Any, Literal
from urllib.parse import quote, urlparse

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# The topic tags that make a decision-backed alert a card of its own kind
# (the UI's LeadTrainingCard and HeldActionCard).
LEAD_TRAINING_TAG = "take_the_lead:training"
HELD_ACTION_TAG = "decision_class:department_action"

# "Dismiss N older than X days" in Needs you's menu.
NEEDS_YOU_DISMISS_OLDER_THAN_DAYS = 7

# Snooze: three hours on (rounded up to the quarter hour) while that is still
# today, then 9:00 tomorrow, then 9:00 next Monday.
_MORNING_HOUR = 9
_LATER_TODAY_HOURS = 3

# Peer memory records a chat turn's memory text as the person's own words;
# the briefing's hand-offs send a short line of theirs instead of the card.
_MEMORY_LINE_MAX_CHARS = 300

Lane = Literal["needs_you", "team", "monitoring"]
CardKind = Literal["proposal", "roster_request", "lead_training", "held_action"]
ActionKey = Literal[
    "approve",
    "approve_with_edits",
    "dismiss",
    "snooze",
    "run_again",
    "open_run",
    "nudge",
    "run_workflow",
    "open_document",
    "open_in_app",
    "open_person",
    "discuss",
]
Tone = Literal["amber", "emerald", "rose"]


class CardAction(BaseModel):
    """One thing a proposal card offers. The card shows the one marked
    ``primary`` as its button and ``discuss`` beside it; the rest go in its
    menu in this order."""

    key: ActionKey
    label: str
    primary: bool = False
    # Where it leads: an app path, or an outside URL when ``external``.
    href: str | None = None
    external: bool = False
    # snooze: when the card comes back (ISO-8601, with offset), and the short
    # form for "Snoozed until …".
    until: str | None = None
    short: str | None = None
    # discuss / nudge: the chat turn to start, and the line peer memory keeps
    # as the person's own words for it.
    prompt: str | None = None
    memory_text: str | None = None


class SnoozeChoice(BaseModel):
    label: str
    short: str
    until: str


class SummaryTarget(BaseModel):
    """Where a summary phrase leads: the Needs you queue, a tile's panel, or
    another page."""

    kind: Literal["needs_you", "panel", "href"]
    panel: Literal["handled", "people", "departments", "inFlight", "monitoring"] | None = None
    href: str | None = None


class SummaryPart(BaseModel):
    text: str
    count: int
    target: SummaryTarget


class DismissOffer(BaseModel):
    """Needs you's "Dismiss N older than X days": the caller's own cards
    older than ``days`` (roster requests never), as explicit ids for
    ``POST /alerts/bulk-ack``."""

    days: int
    alert_ids: list[int]
    label: str


class Tile(BaseModel):
    """One count tile under Needs you; ``key`` names the panel it opens."""

    key: Literal["departments", "people", "team", "inFlight", "handled", "monitoring", "clients"]
    value: str
    label: str
    sub: str
    tone: Tone | None = None


# --------------------------------------------------------------------------- #
# Cards waiting elsewhere
# --------------------------------------------------------------------------- #

# name → counter(person) for cards Needs you holds that ``/today`` doesn't
# list itself. A counter that fails counts 0, as the card list it stands for
# shows nothing then.
_WAITING_CARDS: dict[str, Callable[[Any], int]] = {}


def register_waiting_cards(name: str, counter: Callable[[Any], int]) -> None:
    """Count ``counter(person)`` toward the person's Needs you."""
    _WAITING_CARDS[name] = counter


def waiting_card_count(person: Any) -> int:
    if person is None:
        return 0
    total = 0
    for name, counter in _WAITING_CARDS.items():
        try:
            total += max(0, int(counter(person)))
        except Exception:
            logger.warning("home_rules: counting %s failed", name, exc_info=True)
    return total


# --------------------------------------------------------------------------- #
# Lanes and card kinds
# --------------------------------------------------------------------------- #

def lane(
    proposal: Any, *, caller_id: int | None, caller_is_principal: bool, solo: bool,
    caller_is_demo: bool = False,
) -> Lane:
    """Monitoring signals have their own lane. Otherwise a card is the
    caller's when it is routed to them, or routed to no one and they are the
    principal or a demo person (people.demo: on a demo install the cards
    nobody owns are what there is to show them, to look at only); the rest
    is the team's. Solo, and a caller nobody resolved (a direct call), see
    every card as their own."""
    if proposal.category == "monitoring":
        return "monitoring"
    if caller_id is None or solo:
        return "needs_you"
    routed = proposal.routed_to_person_id
    if routed == caller_id or (routed is None and (caller_is_principal or caller_is_demo)):
        return "needs_you"
    return "team"


def card_kind(proposal: Any) -> CardKind:
    """A roster request, a Take the lead training card and a held chat action
    are answered on cards of their own; everything else is a proposal."""
    if proposal.roster_request is not None:
        return "roster_request"
    tags = proposal.topic_tags or []
    if proposal.decision_instance_id is not None:
        if LEAD_TRAINING_TAG in tags:
            return "lead_training"
        if HELD_ACTION_TAG in tags:
            return "held_action"
    return "proposal"


# --------------------------------------------------------------------------- #
# Snooze
# --------------------------------------------------------------------------- #

def _clock(moment: datetime) -> str:
    """"9:00 AM" / "4:30 PM"."""
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _at_morning(local: datetime, days_ahead: int, zone: tzinfo) -> datetime:
    day = local.date() + timedelta(days=days_ahead)
    return datetime.combine(day, time(_MORNING_HOUR), tzinfo=zone)


def snooze_choices(now: datetime, zone: tzinfo) -> list[SnoozeChoice]:
    """Later today, tomorrow morning and next Monday morning, in ``zone``.
    Later today only while it still lands today; next Monday is a week on
    when today is Monday, and left out on a Sunday, where it would be
    tomorrow."""
    local = now.astimezone(zone)
    choices: list[SnoozeChoice] = []
    later = (now + timedelta(hours=_LATER_TODAY_HOURS)).astimezone(zone).replace(second=0, microsecond=0)
    later += timedelta(minutes=math.ceil(later.minute / 15) * 15 - later.minute)
    if later.date() == local.date():
        choices.append(SnoozeChoice(label=f"Snooze until {_clock(later)}", short=_clock(later), until=later.isoformat()))
    tomorrow = _at_morning(local, 1, zone)
    choices.append(SnoozeChoice(
        label=f"Snooze until tomorrow {_clock(tomorrow)}",
        short=f"tomorrow {_clock(tomorrow)}",
        until=tomorrow.isoformat(),
    ))
    # Monday = 0 in Python; days on to the next Monday, a week when today is one.
    to_monday = (7 - local.weekday()) % 7 or 7
    if to_monday > 1:
        monday = _at_morning(local, to_monday, zone)
        choices.append(SnoozeChoice(
            label=f"Snooze until Monday {_clock(monday)}",
            short=f"Monday {_clock(monday)}",
            until=monday.isoformat(),
        ))
    return choices


# --------------------------------------------------------------------------- #
# What a card offers
# --------------------------------------------------------------------------- #

def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def memory_line(action: str, subject: str) -> str:
    """The person's own line for a hand-off: the action as its own sentence,
    then the card's subject in quotes."""
    s = _clip(" ".join(subject.split()), _MEMORY_LINE_MAX_CHARS)
    return f'{action}.\n"{s}"' if s else f"{action}."


def _monitoring_seed(proposal: Any) -> str:
    body = proposal.body or proposal.headline
    return (
        f"Help me understand this signal we're monitoring:\n\n{body}\n\n"
        f"[Discuss mode — alert_id={proposal.alert_id}] This is a passive "
        "monitoring signal, not a proposal to approve. Explain why it matters, "
        "whether it warrants any action, and what you'd recommend. Answer "
        "conversationally. Only if I explicitly ask you to act should you do "
        "more than advise; do not ack or dismiss the alert yourself."
    )


def _discuss_prompt(proposal: Any, *, is_artifact: bool, is_monitoring: bool, is_decision: bool) -> str:
    """The chat seed for Discuss: the whole card, and what the Executive may
    and may not do from chat for this kind of card."""
    alert_id = proposal.alert_id
    if is_artifact:
        rationale = f"\n\nWhy you flagged it: {proposal.suggested_action}" if proposal.suggested_action else ""
        return (
            "Let's discuss this artifact you flagged for my review:\n\n"
            f"# {proposal.headline}\n\n{proposal.body or ''}{rationale}\n\n"
            f"[Discuss mode — alert_id={alert_id}, artifact id "
            f"alert:{alert_id}; to revise it, publish a new version with "
            f'draft_artifact(supersedes="alert:{alert_id}")] This is a document '
            "for review, not an action to approve. Answer my questions about it "
            'conversationally. When I say I\'m done ("got it", "reviewed", "thanks"), '
            f'call ack_alert(alert_id={alert_id}, status="ack") to clear '
            "it from my queue. Take no other action."
        )
    if is_monitoring:
        return _monitoring_seed(proposal)
    if is_decision:
        body = proposal.body or proposal.headline
        return (
            f"Let's talk through this meeting I've proposed:\n\n{body}\n\n"
            "[Discuss mode] This booking is awaiting your approval on the "
            "briefing. Help me decide whether the time, attendees, and purpose "
            "make sense. Do NOT book, cancel, or ack anything from chat — I'll "
            "approve or dismiss it from the card itself."
        )
    text = proposal.body or proposal.headline
    suggested = f"\n\nIf I approve, you will:\n{proposal.suggested_action}" if proposal.suggested_action else ""
    primer = (
        f"\n\n[Discuss mode — alert_id={alert_id}] "
        "This proposal is NOT YET approved. Answer my questions conversationally. "
        'When I explicitly approve ("ok", "approve", "go ahead", "do it"), switch to '
        "execute mode: actually attempt the work (use web_search and your other tools — "
        "don't just promise), reply inline with the deliverable, schedule a fresh "
        "follow-up via schedule_followup if it's time-bound, and call "
        f'ack_alert(alert_id={alert_id}, status="ack"). '
        'If I dismiss it ("never mind", "drop it"), call '
        f'ack_alert(alert_id={alert_id}, status="dismissed") and stop. '
        "Until explicit approval/dismissal, take no action and do not ack."
    )
    return f"Tell me about this proposal:\n\n{text}{suggested}{primer}"


def _discuss_memory(proposal: Any, *, is_artifact: bool, is_monitoring: bool, is_decision: bool) -> str:
    if is_artifact:
        action = "Let's discuss this flagged artifact"
    elif is_monitoring:
        action = "Help me understand this monitored signal"
    elif is_decision:
        action = "Let's talk through this proposed meeting"
    else:
        action = "Tell me about this proposal"
    return memory_line(action, proposal.headline)


def _uri_component(value: str) -> str:
    """JavaScript's encodeURIComponent."""
    return quote(value, safe="-_.!~*'()")


def card_actions(
    proposal: Any,
    *,
    lane_: Lane,
    caller_is_principal: bool,
    people_by_id: dict[int, Any],
    snoozes: Sequence[SnoozeChoice],
    caller_is_demo: bool = False,
    caller_id: int | None = None,
) -> list[CardAction]:
    """What a proposal card offers, primary first, then Discuss, then its
    menu. A card of another kind (roster request, training, held action) is
    answered on its own card and offers nothing here.

    The primary is the review's recommended move when it is one the person
    completes (run again, nudge, run a workflow); Dismiss when the review
    thinks the card is stale; Open for a document; nothing for a monitoring
    signal (Discuss leads); otherwise Approve. Approve is the card's own
    person's, or the principal's on a teammate's card, as the server allows.

    A demo person (``caller_is_demo``) only looks at a card that isn't
    routed to them (``caller_id``): Discuss, and Open on a document. On
    their own card they approve only a decision (their ERPNext draft), as
    the approval routes allow. Approving, dismissing, snoozing,
    nudging or running something would act on the company's shared card for
    the principal, and the server refuses it too.
    """
    if card_kind(proposal) != "proposal":
        return []
    tags = proposal.topic_tags or []
    is_artifact = "artifact" in tags
    is_monitoring = proposal.category == "monitoring"
    is_decision = proposal.decision_instance_id is not None
    likely_stale = (proposal.review_verdict or "") == "likely_stale"
    routed = proposal.routed_to_person_id
    assignee = people_by_id.get(routed) if routed is not None else None
    first_name = assignee.full_name.split(" ")[0] if assignee is not None and assignee.full_name else None
    move = proposal.recommended_move or ""
    may_approve = lane_ == "needs_you" or (lane_ == "team" and caller_is_principal)
    failed = proposal.failed_run_id or None

    nudge: CardAction | None = None
    if move == "nudge" and not is_artifact and not is_monitoring:
        who = assignee.full_name if assignee is not None else "the owner"
        nudge = CardAction(
            key="nudge",
            label=f"Nudge {first_name or 'owner'}",
            prompt=(
                f"Nudge {who} about this item — it has gone quiet: {proposal.headline}\n\n"
                "Send a short, friendly check-in via message_person and tell me what you sent."
            ),
            memory_text=memory_line(f"Nudge {who} about this item", proposal.headline),
        )
    workflow: CardAction | None = None
    if move == "suggest_workflow" and proposal.suggested_workflow and not is_artifact and not is_monitoring:
        workflow = CardAction(
            key="run_workflow",
            label=f"Run {proposal.suggested_workflow.replace('_', ' ')}",
            href=f"/jobs/{proposal.suggested_workflow}",
        )
    rerun = CardAction(key="run_again", label="Run again") if failed else None
    open_run = CardAction(key="open_run", label="Open the run", href=f"/jobs/runs/{_uri_component(failed)}") if failed else None
    approve = (
        CardAction(key="approve", label="Approve")
        if not is_monitoring and not is_artifact and not failed and may_approve else None
    )
    edit_approve = (
        CardAction(key="approve_with_edits", label="Edit & approve")
        if not is_monitoring and not is_artifact and not is_decision and not failed and may_approve else None
    )
    if is_artifact:
        dismiss = CardAction(key="dismiss", label="Mark reviewed")
    else:
        dismiss = CardAction(key="dismiss", label="Dismiss signal" if is_monitoring else "Dismiss")
    open_doc = (
        CardAction(key="open_document", label="Open document", href=f"/artifacts/{_uri_component(f'alert:{proposal.alert_id}')}")
        if is_artifact else None
    )
    open_in_app: CardAction | None = None
    if is_artifact and proposal.artifact_format == "link" and proposal.artifact_url:
        host = urlparse(proposal.artifact_url).hostname or proposal.artifact_url
        open_in_app = CardAction(key="open_in_app", label=f"Open in app ({host})", href=proposal.artifact_url, external=True)
    person_page = (
        CardAction(key="open_person", label=f"Open {first_name or assignee.full_name}'s page", href=f"/people/{assignee.id}")
        if assignee is not None else None
    )

    primary: CardAction | None
    if is_artifact:
        primary = open_doc
    elif is_monitoring:
        primary = None
    elif likely_stale:
        primary = dismiss
    else:
        primary = rerun or nudge or workflow or approve

    kind = {"is_artifact": is_artifact, "is_monitoring": is_monitoring, "is_decision": is_decision}
    discuss = CardAction(
        key="discuss", label="Discuss",
        prompt=_discuss_prompt(proposal, **kind), memory_text=_discuss_memory(proposal, **kind),
    )
    # Not on a drafted document, which lives in the gallery rather than the queue.
    snooze_actions = [] if is_artifact else [
        CardAction(key="snooze", label=c.label, until=c.until, short=c.short) for c in snoozes
    ]
    menu = [
        a for a in (rerun, open_run, nudge, workflow, approve, edit_approve, *snooze_actions, dismiss, open_in_app, person_page)
        if a is not None and a is not primary
    ]
    # Theirs alone: routed to them (an ERPNext draft they asked for).
    # Anything else they only look at.
    if caller_is_demo and (routed is None or routed != caller_id):
        looks = [a for a in (open_doc, open_in_app) if a is not None]
        if looks:
            return [looks[0].model_copy(update={"primary": True}), discuss, *looks[1:]]
        return [discuss]
    if caller_is_demo and not is_decision:
        # /alerts/{id}/approve refuses every demo person; only a decision of
        # theirs is answered (/decisions/{id}/approve).
        menu = [a for a in menu if a is not approve and a is not edit_approve]
        if primary is approve:
            primary = None
    out: list[CardAction] = []
    if primary is not None:
        out.append(primary.model_copy(update={"primary": True}))
    out.append(discuss)
    out.extend(menu)
    return out


# --------------------------------------------------------------------------- #
# Needs you's older-than offer
# --------------------------------------------------------------------------- #

def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def dismiss_offer(mine: Iterable[Any], now: datetime, days: int = NEEDS_YOU_DISMISS_OLDER_THAN_DAYS) -> DismissOffer:
    """The caller's own cards last seen more than ``days`` ago (the server's
    TTL anchor, so a re-firing situation is not old). A roster request is
    answered on its card and never swept."""
    cutoff = now - timedelta(days=days)
    ids = []
    for p in mine:
        if p.roster_request is not None:
            continue
        seen = _parse(p.last_seen_at or p.created_at)
        if seen is not None and seen < cutoff:
            ids.append(p.alert_id)
    return DismissOffer(days=days, alert_ids=ids, label=f"Dismiss {len(ids)} older than {days} days")


# --------------------------------------------------------------------------- #
# Handled rows
# --------------------------------------------------------------------------- #

# Reading order: what needs the principal, then what they can undo, then what
# went to others, then transparency.
_HANDLED_ORDER = {
    "escalated": 0,
    "closed": 1,
    "merged": 1,
    "routed": 2,
    "nudged": 2,
    "drafted": 2,
    "suggested_workflow": 3,
    "watching": 3,
    "stopped_watching": 3,
}


def _is_close(kind: str) -> bool:
    return kind in ("closed", "merged")


def group_handled(items: Sequence[Any]) -> list[tuple[Any, list[Any], int]]:
    """One ``(item, also, folded_in)`` per alert from the newest-first list.

    A merge whose survivor is also listed becomes a "duplicate folded in" on
    the survivor. The newest move takes the visible slot, except that a close
    (it carries Undo) is never hidden behind an older move, and otherwise an
    escalation beats routed / nudged / drafted, so the row that needs the
    principal is never buried. Rows sort by kind, newest first within one.
    """
    listed = {h.alert_id for h in items if h.alert_id is not None}
    folded_into: dict[int, int] = {}
    rest = []
    for h in items:
        into = h.superseded_by_alert_id
        if h.kind == "merged" and into is not None and into in listed:
            folded_into[into] = folded_into.get(into, 0) + 1
        else:
            rest.append(h)
    by_key: dict[str, list[Any]] = {}
    rows: list[list[Any]] = []
    for h in rest:
        key = f"alert-{h.alert_id}" if h.alert_id is not None else f"{h.kind}-{h.at}-"
        row = by_key.get(key)
        if row is None:
            row = [h, [], folded_into.get(h.alert_id, 0) if h.alert_id is not None else 0]
            by_key[key] = row
            rows.append(row)
        elif h.kind == "escalated" and not _is_close(row[0].kind) and row[0].kind != "escalated":
            row[1].append(row[0])
            row[0] = h
        else:
            row[1].append(h)
    rows.sort(key=lambda r: _HANDLED_ORDER.get(r[0].kind, 4))
    return [(r[0], r[1], r[2]) for r in rows]


# --------------------------------------------------------------------------- #
# Summary, tiles and the quiet day
# --------------------------------------------------------------------------- #

def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def summary(
    *,
    needs_you: int,
    handled: int,
    people_overdue: int,
    people_need_reply: int,
    dept_at_risk: int,
    goals_at_risk: int,
    in_flight: int,
    monitoring: int,
) -> list[SummaryPart]:
    """The line under the greeting, as phrases in reading order: what needs
    you, what the Executive did on its own, then the rest by urgency. Zero
    counts are left out; none at all means all clear."""
    parts: list[SummaryPart] = []

    def add(count: int, text: str, target: SummaryTarget) -> None:
        if count > 0:
            parts.append(SummaryPart(text=text, count=count, target=target))

    add(needs_you, f"{needs_you} {_plural(needs_you, 'thing needs', 'things need')} you", SummaryTarget(kind="needs_you"))
    add(handled, f"The Executive handled {handled} on its own", SummaryTarget(kind="panel", panel="handled"))
    add(people_overdue, f"{people_overdue} overdue", SummaryTarget(kind="panel", panel="people"))
    add(people_need_reply, f"{people_need_reply} awaiting reply", SummaryTarget(kind="panel", panel="people"))
    add(
        dept_at_risk,
        f"{dept_at_risk} {_plural(dept_at_risk, 'department', 'departments')} at risk",
        SummaryTarget(kind="panel", panel="departments"),
    )
    add(goals_at_risk, f"{goals_at_risk} {_plural(goals_at_risk, 'goal', 'goals')} at risk", SummaryTarget(kind="href", href="/goals"))
    add(in_flight, f"{in_flight} under way", SummaryTarget(kind="panel", panel="inFlight"))
    add(monitoring, f"{monitoring} {_plural(monitoring, 'signal', 'signals')} monitored", SummaryTarget(kind="panel", panel="monitoring"))
    return parts


def active_departments(departments: Iterable[Any]) -> list[Any]:
    return [d for d in departments if d.goal_count > 0 or d.awaiting_count > 0]


def departments_needing_attention(departments: Iterable[Any]) -> list[Any]:
    return [
        d for d in active_departments(departments)
        if d.at_risk_count > 0 or d.off_track_count > 0 or d.awaiting_count > 0
    ]


def people_summary(people: Sequence[Any]) -> tuple[str, bool]:
    """"2 to reply · 1 overdue", and whether anyone is overdue."""
    needs_reply = sum(1 for p in people if p.status == "needs_reply")
    awaiting = sum(1 for p in people if p.status == "awaiting")
    on_leave = sum(1 for p in people if p.status == "on_leave")
    overdue = sum(1 for p in people if p.overdue)
    parts = []
    if needs_reply:
        parts.append(f"{needs_reply} to reply")
    if awaiting:
        parts.append(f"{awaiting} to action")
    if overdue:
        parts.append(f"{overdue} overdue")
    if on_leave:
        parts.append(f"{on_leave} on leave")
    return (" · ".join(parts) if parts else "All clear"), overdue > 0


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def _in_future(when: datetime, now: datetime) -> str:
    """"in 8h"; "soon" once it is due."""
    mins = _js_round((when - now).total_seconds() / 60)
    if mins <= 0:
        return "soon"
    if mins < 60:
        return f"in {mins}m"
    hours = _js_round(mins / 60)
    if hours < 24:
        return f"in {hours}h"
    return f"in {_js_round(hours / 24)}d"


def in_flight_next(in_flight: Sequence[Any], now: datetime) -> str:
    overdue = sum(1 for f in in_flight if f.overdue)
    if overdue:
        return f"{overdue} overdue"
    times = sorted(t for t in (_parse(f.run_at) for f in in_flight) if t is not None)
    return f"next {_in_future(times[0], now)}" if times else "scheduled"


def tiles(
    *,
    solo: bool,
    departments: Sequence[Any],
    people: Sequence[Any],
    team: int,
    in_flight: Sequence[Any],
    handled: int,
    monitoring: int,
    clients: int,
    now: datetime,
) -> list[Tile]:
    """The count tiles with something to show. Solo has no Departments or
    People tile; its own tiles (due soon, projects, the weekly review) come
    from their own endpoints and lead the row."""
    out: list[Tile] = []
    if not solo:
        active = active_departments(departments)
        attention = departments_needing_attention(departments)
        at_risk = [d for d in attention if d.at_risk_count > 0 or d.off_track_count > 0]
        tone: Tone | None
        if attention:
            sub, tone = ("at risk" if at_risk else "need attention"), "amber"
        elif active:
            sub, tone = "on track", "emerald"
        else:
            sub, tone = "not set up yet", None
        out.append(Tile(
            key="departments", value=str(len(attention) or len(active) or len(departments)),
            label="Departments", sub=sub, tone=tone,
        ))
        if len(people) > 1:
            busy = sum(1 for p in people if p.status in ("needs_reply", "awaiting"))
            text, has_overdue = people_summary(people)
            out.append(Tile(
                key="people", value=str(busy or len(people)), label="People",
                sub=text if busy else "all clear",
                tone="rose" if has_overdue else "amber" if busy else "emerald",
            ))
    if team:
        out.append(Tile(key="team", value=str(team), label="Across the team", sub="with teammates"))
    if in_flight:
        out.append(Tile(
            key="inFlight", value=str(len(in_flight)), label="Under way", sub=in_flight_next(in_flight, now),
            tone="amber" if any(f.overdue for f in in_flight) else None,
        ))
    if handled:
        out.append(Tile(key="handled", value=str(handled), label="Handled", sub="by the Executive", tone="emerald"))
    if monitoring:
        out.append(Tile(
            key="monitoring", value=str(monitoring), label="Monitoring",
            sub=_plural(monitoring, "signal", "signals"),
        ))
    if clients:
        out.append(Tile(key="clients", value=str(clients), label="Across your clients", sub="parked clients"))
    return out


def quiet(*, proposals: int, waiting_cards: int, departments: Sequence[Any], people: Sequence[Any]) -> bool:
    """A quiet day: nothing on the board or waiting elsewhere, and no
    department or person waiting. Home then says nothing under an empty
    Needs you."""
    return (
        proposals == 0
        and waiting_cards == 0
        and all(
            d.at_risk_count == 0 and d.off_track_count == 0 and d.awaiting_count == 0
            for d in active_departments(departments)
        )
        and all(p.awaiting_count == 0 for p in people)
    )


def quiet_banner(*, quiet_day: bool, departments: Sequence[Any], first_week: bool) -> bool:
    """The quiet-day banner: a quiet day with a department set up, and not
    the first week (whose card says what to start on instead)."""
    return quiet_day and bool(active_departments(departments)) and not first_week
