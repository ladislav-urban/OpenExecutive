"""How the Watch list shows a watch, so every client reads the same words.

The web app's Watch list and a host app's own Watch list screen read these as
fields (``WatchlistView``, ``SignalView``, ``DraftView``, ``GET
/watchlist/form``, ``POST /watchlist/preview``) instead of holding copies:

- whether a watch is a suggestion waiting for a yes, and whether the
  Executive added it;
- its trigger, cadence and loudness in words (``describe``'s lines);
- the quieter version a noisy watch offers (``raise_the_bar``);
- the decline reasons, with what each does, and the verb they go under;
- the group a watch sits in, with its label and order;
- a signal's outcome in words;
- the slug a watch gets when none is given, and a draft's form values.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from openexecutive.monitoring.describe import (
    STOCK_DEFAULT_PCT,
    cadence_line,
    loudness_line,
    trigger_line,
)
from openexecutive.monitoring.models import ORIGIN_RESEARCH, ORIGIN_RESEARCH_PROPOSED

# --------------------------------------------------------------------------- #
# The form's choices
# --------------------------------------------------------------------------- #


class SignalTypeOption(BaseModel):
    value: str
    label: str
    # What to put in the target field.
    hint: str
    # Each poll is billed (a model web search), unlike the keyless feeds.
    billed: bool = False
    # An example trigger for the form (a JSON object).
    trigger_example: str = "{}"


class Option(BaseModel):
    value: str
    label: str


class DeclineReason(BaseModel):
    value: str
    label: str
    # What it does, said on the button.
    hint: str


_KEYWORDS_EXAMPLE = '{"keywords": ["layoffs", "downtime"]}'

SIGNAL_TYPES: tuple[SignalTypeOption, ...] = (
    SignalTypeOption(
        value="stock", label="Stock", hint="Yahoo Finance ticker — e.g. AAPL",
        trigger_example='{"abs_change_pct_gte": 5}',
    ),
    SignalTypeOption(
        value="rss", label="RSS / Atom", hint="Feed URL — e.g. competitor changelog",
        trigger_example=_KEYWORDS_EXAMPLE,
    ),
    SignalTypeOption(value="vendor_status", label="Vendor status", hint="Statuspage atom/RSS feed URL"),
    SignalTypeOption(
        value="edgar", label="SEC EDGAR", hint="Ticker or CIK — e.g. AAPL or 320193",
        trigger_example='{"forms": ["8-K", "10-K"]}',
    ),
    SignalTypeOption(
        value="page_watch",
        label="Page change",
        hint="Public page URL to watch for content changes — e.g. a pricing or careers page",
        trigger_example=_KEYWORDS_EXAMPLE,
    ),
    SignalTypeOption(
        value="query",
        label="Web search (billed)",
        hint="Standing search query — e.g. Acme Corp layoffs OR restructuring OR funding",
        billed=True,
        trigger_example=_KEYWORDS_EXAMPLE,
    ),
)

CADENCES: tuple[str, ...] = ("real_time", "15min", "hourly", "daily", "weekly")
SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "urgent")

DECLINE_REASONS: tuple[DeclineReason, ...] = (
    DeclineReason(value="not_relevant", label="Not relevant", hint="Never suggest this company/topic again"),
    DeclineReason(value="too_noisy", label="Too noisy", hint="Keep it, but only high-severity signals"),
    DeclineReason(value="wrong_source", label="Wrong source", hint="Right topic, drop only this feed/page"),
)

_FLOOR_WORDS = {
    "low": "Everything, even small changes",
    "medium": "Only things that matter",
    "high": "Only big things",
    "urgent": "Only urgent things",
}


class FormDefaults(BaseModel):
    signal_type: str = "stock"
    cadence: str = "15min"
    severity_floor: str = "low"
    severity_ceiling: str = "urgent"
    mode: str = "active"


class WatchForm(BaseModel):
    """The add form's choices and defaults (``GET /watchlist/form``). A
    trigger is sent as a JSON object (``POST /watchlist``'s ``trigger``),
    never as text to parse."""

    signal_types: list[SignalTypeOption]
    cadences: list[Option]
    severities: list[Option]
    decline_reasons: list[DeclineReason]
    defaults: FormDefaults = Field(default_factory=FormDefaults)


def watch_form() -> WatchForm:
    return WatchForm(
        signal_types=list(SIGNAL_TYPES),
        cadences=[Option(value=c, label=cadence_line(c)) for c in CADENCES],
        severities=[Option(value=s, label=_FLOOR_WORDS[s]) for s in SEVERITIES],
        decline_reasons=list(DECLINE_REASONS),
    )


# --------------------------------------------------------------------------- #
# A watch
# --------------------------------------------------------------------------- #


class WatchGroup(BaseModel):
    """The group a watch sits in on the list: its signal type."""

    key: str
    label: str
    # Known types in the form's order; any other after them (sort by
    # ``order``, then ``key``).
    order: int


class RaiseTheBar(BaseModel):
    """The quieter version of a noisy watch."""

    # Button text.
    label: str
    # What changes, said once it is done.
    done: str
    # The PATCH /watchlist/{slug} body that makes it so.
    patch: dict[str, Any]


class DeclineChoice(BaseModel):
    value: str
    label: str
    hint: str
    # The menu item: "Decline: not relevant (never suggest this company/topic again)".
    menu_label: str
    # Stopping a watch the Executive added: the PATCH /watchlist/{slug} that
    # keeps it but quieter (too noisy), else None to DELETE it with this
    # reason. A suggestion is declined with POST /watchlist/{slug}/decline.
    patch: dict[str, Any] | None = None


class DeclineMenu(BaseModel):
    """How a watch the Executive suggested or added is turned down."""

    # "Decline" a suggestion, "Stop watching" one it added.
    verb: str
    reasons: list[DeclineChoice]


def is_suggestion(origin: str, mode: str) -> bool:
    """A research suggestion: the Executive wanted to watch it but wasn't
    sure enough to add it on its own. It polls without alerting until
    approved or declined."""
    return origin == ORIGIN_RESEARCH_PROPOSED and mode == "dry_run"


def group_of(signal_type: str) -> WatchGroup:
    known = [s.value for s in SIGNAL_TYPES]
    if signal_type in known:
        option = SIGNAL_TYPES[known.index(signal_type)]
        return WatchGroup(key=signal_type, label=option.label, order=known.index(signal_type))
    return WatchGroup(key=signal_type, label=signal_type, order=len(known))


def decline_menu(origin: str, mode: str) -> DeclineMenu | None:
    if is_suggestion(origin, mode):
        verb = "Decline"
    elif origin == ORIGIN_RESEARCH:
        verb = "Stop watching"
    else:
        return None
    return DeclineMenu(
        verb=verb,
        reasons=[
            DeclineChoice(
                value=r.value, label=r.label, hint=r.hint,
                menu_label=f"{verb}: {r.label.lower()} ({r.hint[:1].lower()}{r.hint[1:]})",
                # The same remedy the decline route applies: keep the source,
                # only high-severity signals surface.
                patch={"severity_floor": "high"} if verb == "Stop watching" and r.value == "too_noisy" else None,
            )
            for r in DECLINE_REASONS
        ],
    )


def _pct_of(trigger: dict[str, Any]) -> float:
    v = trigger.get("abs_change_pct_gte")
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and v > 0 else STOCK_DEFAULT_PCT


def raise_the_bar(signal_type: str, trigger: dict[str, Any] | None, severity_floor: str) -> RaiseTheBar | None:
    """A stock's move threshold doubled (whole percent, at most 100),
    otherwise the severity floor one step up. None when it can't get quieter."""
    trig = dict(trigger or {})
    if signal_type == "stock":
        now = _pct_of(trig)
        nxt = min(100, max(int(now * 2 + 0.5), int(now) + 1))
        if nxt > now:
            return RaiseTheBar(
                label=f"Only moves of {nxt}%+",
                done=f"You'll only hear about moves of {nxt}% or more.",
                patch={"trigger": {**trig, "abs_change_pct_gte": nxt}},
            )
    if severity_floor not in SEVERITIES or severity_floor == SEVERITIES[-1]:
        return None
    nxt_floor = SEVERITIES[SEVERITIES.index(severity_floor) + 1]
    words = _FLOOR_WORDS[nxt_floor]
    about = re.sub(r"^Only ", "", words).lower()
    return RaiseTheBar(label=words, done=f"You'll only hear about {about} from this.", patch={"severity_floor": nxt_floor})


def outcome_label(outcome: str | None) -> str:
    """A signal's outcome in words: pending, alerted, failed, "suppressed: …"."""
    if outcome is None:
        return "pending"
    return re.sub(r"^suppressed_", "suppressed: ", outcome)


# --------------------------------------------------------------------------- #
# Slugs and drafts
# --------------------------------------------------------------------------- #

_SLUG_PREFIX = {
    "stock": "stock",
    "rss": "rss",
    "vendor_status": "status",
    "edgar": "edgar",
    "page_watch": "page",
    "query": "search",
}
_SLUG_MAX = 61


def suggest_slug(signal_type: str, target: str) -> str:
    """The slug a watch gets when none is given: its type and target,
    kebab-cased to the slug rule (``[a-z0-9][a-z0-9-]{0,60}``). A URL is
    named by its host and path."""
    t = target.strip().lower()
    if not t:
        return ""
    t = re.sub(r"^[a-z][a-z0-9+.-]*://", "", t)
    t = re.sub(r"^www\.", "", t)
    prefix = _SLUG_PREFIX.get(signal_type) or re.sub(r"[^a-z0-9]+", "-", signal_type.lower())
    body = re.sub(r"[^a-z0-9]+", "-", t)
    slug = re.sub(r"^-+", "", re.sub(r"-+", "-", f"{prefix}-{body}"))
    return re.sub(r"-+$", "", slug[:_SLUG_MAX])


class FormInitial(BaseModel):
    """A draft's values as the add form starts from them."""

    slug: str
    signal_type: str
    target: str
    display_label: str
    trigger: dict[str, Any]
    cadence: str
    severity_floor: str
    severity_ceiling: str
    mode: str
    notes: str


def form_initial(draft: Any) -> FormInitial:
    """An unknown signal type starts as a stock, an unknown cadence as every
    15 minutes, and anything but a dry run as active."""
    known = {s.value for s in SIGNAL_TYPES}
    return FormInitial(
        slug=draft.slug,
        signal_type=draft.signal_type if draft.signal_type in known else "stock",
        target=draft.target,
        display_label=draft.display_label,
        trigger=dict(draft.trigger or {}),
        cadence=draft.cadence if draft.cadence in CADENCES else "15min",
        severity_floor=draft.severity_floor,
        severity_ceiling=draft.severity_ceiling,
        mode="dry_run" if draft.mode == "dry_run" else "active",
        notes=draft.notes,
    )


class WatchPreview(BaseModel):
    """What the add form's values come to (``POST /watchlist/preview``)."""

    # The slug it gets when the slug field is left empty.
    slug: str
    trigger_words: str
    cadence_words: str
    loudness_words: str


def preview(signal_type: str, target: str, trigger: dict[str, Any], cadence: str, floor: str, ceiling: str) -> WatchPreview:
    return WatchPreview(
        slug=suggest_slug(signal_type, target),
        trigger_words=trigger_line(signal_type, trigger),
        cadence_words=cadence_line(cadence),
        loudness_words=loudness_line(floor, ceiling),
    )


__all__ = [
    "DECLINE_REASONS",
    "SIGNAL_TYPES",
    "DeclineMenu",
    "FormInitial",
    "RaiseTheBar",
    "WatchForm",
    "WatchGroup",
    "WatchPreview",
    "decline_menu",
    "form_initial",
    "group_of",
    "is_suggestion",
    "outcome_label",
    "preview",
    "raise_the_bar",
    "suggest_slug",
    "watch_form",
]
