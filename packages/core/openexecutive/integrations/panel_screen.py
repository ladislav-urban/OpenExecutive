"""A panel reply's sentences about what is on the person's screen, held to
what is: the page the panel sent this turn, or the views the answer opened on
it (the pilot's Nov 5, 2026 second retake, learning-2: "The Sales Invoice list
you have open also works…", after the answer had opened the Accounts
Receivable report in its place).

SCREEN_RULE asks the model for this; this is the code that holds it. A
sentence is dropped when it points at something as on screen ("you have
open", "beside the panel", "on your screen") and names, as a list, report or
form, something the screen doesn't show: the page the answer replaced by
opening another view, or a page never open at all. Only a sentence that names
it is dropped; one that only says "the report beside the panel" stands. A
reply that would be left empty is kept as it was. Code only, no model call."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from openexecutive.orchestrator import answer_correct

# Words that say something is on the person's screen now.
_ON_SCREEN = re.compile(
    r"\b(?:you(?:'ve| have)(?: got)? (?:\w+ )?open|you have up|currently open|now open|open beside|"
    r"beside (?:the|this|your) panel|next to (?:the|this|your) panel|on your screen|in front of you)\b",
    re.I,
)
# "the Sales Invoice list", "your Accounts Receivable report": a named view.
_NAMED = re.compile(
    r"\b(?i:the|your|this|that)\s+(?P<name>(?:[A-Z][\w&'’.-]*\s+){1,6}?)(?P<kind>list|report|form|chart|view)\b"
)
_VIEW_KEYS = ("doctype", "report", "report_name", "name", "page", "workspace", "dashboard", "chart")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKC", text).lower()).strip()


def _names(items: list[Any]) -> set[str]:
    out: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in _VIEW_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                out.add(_norm(value))
    return out


def _singular(text: str) -> str:
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in text.split())


def _within(small: str, big: str) -> bool:
    """``small`` as whole words of ``big`` (both normalised), plurals aside
    ("the Sales Invoices list" for Sales Invoice)."""
    return f" {_singular(small)} " in f" {_singular(big)} "


def _shown(name: str, allowed: set[str]) -> bool:
    n = _norm(name)
    # Whole words, three letters or more: a document named "e" shows nothing.
    return any(_within(n, a) or _within(a, n) for a in allowed if len(a) >= 3) if len(n) >= 3 else False


# A sentence that warns off or offers instead isn't a claim about the screen.
_KEEP_WORDS = re.compile(r"\b(?:don['’]t|do not|never|instead|rather than|can['’]t|cannot|unable)\b", re.I)


def drop_stale_screen(reply: str, page: Any, views: list[dict[str, Any]]) -> tuple[str, int]:
    """``reply`` without the sentences that name a view as on screen when the
    screen doesn't show it, and how many went (module docstring)."""
    if not reply or not _ON_SCREEN.search(reply):
        return reply, 0
    if (
        isinstance(page, dict) and isinstance(page.get("screen"), str) and page["screen"]
        and not any(page.get(k) for k in ("doctype", "name", "report")) and not views
    ):
        # Beside one of Dynocortex's own screens: SCREEN_RULE has the reply
        # say it can't see that screen, which names it.
        return reply, 0
    # Once the answer opens a view, that is what the screen shows; the page
    # it was asked from is gone (SCREEN_RULE).
    allowed = _names(views) if views else _names([page] if isinstance(page, dict) else [])
    gone: set[answer_correct.Unit] = set()
    all_units = answer_correct.units(reply)
    for m in _ON_SCREEN.finditer(reply):
        unit = answer_correct.unit_of(all_units, m.start(), m.end())
        if unit is None or unit in gone or unit.lead or unit.kind in ("heading", "header", "code", "row"):
            continue
        part = reply[unit.start:unit.end]
        norm_part = _norm(part)
        if _KEEP_WORDS.search(part) or any(_within(a, norm_part) for a in allowed if len(a) >= 3):
            # A warning, an alternative, or a sentence that also names what
            # is on screen: left as written.
            continue
        named = [n.group("name") for n in _NAMED.finditer(part)]
        if named and not any(_shown(name, allowed) for name in named):
            gone.add(unit)
    # Only the outermost of nested units: a child inside a removed parent
    # goes with it (spliced twice, it would cut what follows).
    gone = {u for u in gone if not any(o is not u and o.start <= u.start and u.end <= o.end for o in gone)}
    # Never every item of a list: its lead-in would be left with nothing.
    for block in {u.block for u in gone if u.block >= 0}:
        members = [u for u in all_units if u.block == block and u.kind in ("item", "row")]
        if members and all(any(g.start <= u.start and u.end <= g.end for g in gone) for u in members):
            gone = {u for u in gone if u.block != block}
    if not gone:
        return reply, 0
    out = reply
    for unit in sorted(gone, key=lambda u: u.start, reverse=True):
        out = out[:unit.start] + out[unit.end:]
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if not out:
        return reply, 0
    return out, len(gone)
