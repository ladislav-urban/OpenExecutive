"""The project-status job (phase 1): each Friday afternoon, after the team's
Project Updates, a short status of each open project, in the same order
every week.

At 17:00 company time on Fridays it reads:

1. **the company's projects not Completed** (``list_records`` Project,
   ``status != Completed``), with their figures: status, percent complete,
   expected start and end, hours used (``actual_time``), estimated and
   actual cost, billed amount and customer. A shape holds no list of
   statuses, so code drops Cancelled ones; On Hold ones get the closing
   line only;
2. **their open tasks** (``list_records`` Task, ``status != Completed``,
   ``project in`` read 1's projects);
3. **their Project Updates of the last week** (``list_records`` Project
   Update, submitted, ``date >=`` seven days back, ``project in`` read 1's
   projects), with the status text the team wrote on its ``users`` rows.

Reads 2 and 3 take read 1's projects as ``$projects``: the active ones
first, worst first, then those on hold, at most 50 (``Derived``, marked
truncated when there were more); a name the slot wouldn't take is never
sent. Both are skipped when no project counts. None reads Timesheet (a
person's hours), an expense claim total, or Communication.

Its rules are code (``projects``, ``build``), unit-tested on fixed data:

- **Hours:** ``actual_time`` against the estimate at cost: over when
  ``total_costing_amount`` passes ``estimated_costing``, by that share
  (P%), and by the hours that share is of the hours used; "no estimate"
  without ``estimated_costing``.
- **Schedule:** ``expected_end_date`` against today: "N days late", "on
  plan", or "no end date".
- **Late tasks:** an open task (not Completed, Cancelled or a template)
  whose ``exp_end_date`` is before today, oldest first, up to 5 a project.
- **The team's words:** the latest submitted update of the week (never one
  dated after today), its users' status texts joined, cut to 1,200 raw
  characters, tags dropped, cleaned and cut to 300 (``QUOTE_MAX``), marked
  "..." when cut. None: "No status update this week".
- **In short:** one sentence per open project, from its figures alone
  (``standing``): behind schedule or on it (or no end date), over its
  estimated cost or within it (or no estimate), and whether it has late
  tasks. A fixed sentence for each combination, never a number.
- **Order:** the most days late first, then the most over, the most late
  tasks, the billed value and the name.

What it leaves: **one notice, "Projects this week"**, for the job's
reviewer, or the principal, with up to ``NOTICE_MAX`` projects, each
with code's lines and its "In short" sentence, then "The team wrote"
(their words, inside the notice's untrusted block); and a closing "On hold"
row naming each project on hold with its overdue tasks. Notes say when a
read was cut or there were more projects than shown.

**No model line.** Every line the notice writes is code's, from the
figures; the team's update is only quoted, under "The team wrote", inside
the untrusted block. The job has no reason step (``reason_schema`` None), so
no model reads an update and nothing an update says, however it is worded,
can reach a line the notice writes.

No card: the job leaves notices only (``check_fields`` is None), so the
draft-card, hold and kept-from-reviewer paths don't apply to it.
"""
from __future__ import annotations

import html
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

from openexecutive.watch.registry import (
    MAX_LIST,
    CardSpec,
    Derived,
    DraftSpec,
    Prior,
    ReadArguments,
    Settings,
    WatchJob,
    WatchRead,
)

JOB_ID = "project-status"
PROJECT = "Project"
TASK = "Task"
UPDATE = "Project Update"
PROJECT_FIELDS = [
    "name", "project_name", "status", "percent_complete", "expected_start_date", "expected_end_date",
    "actual_time", "estimated_costing", "total_costing_amount", "total_billed_amount", "customer",
]
# No task hours (expected_time, actual_time) or progress: the job's figures
# are the project's own, and oe_connector's listing reads none of them.
TASK_FIELDS = ["name", "subject", "project", "status", "exp_end_date"]
UPDATE_FIELDS = ["name", "project", "date", "users.project_status"]
# Its reads' entries in oe_connector_watch_jobs, as oe_connector lists them
# (tests/unit/watch_job_vectors.json and watch_shape_vectors.json pin them).
PROJECTS_SHAPE: Mapping[str, Any] = {
    "doctype": PROJECT,
    "fields": PROJECT_FIELDS,
    "filters": {"company": "$company", "status": ["!=", "Completed"]},
    "limit": "$int",
}
TASKS_SHAPE: Mapping[str, Any] = {
    "doctype": TASK,
    "fields": TASK_FIELDS,
    "filters": {"company": "$company", "status": ["!=", "Completed"], "project": ["in", "$projects"]},
    "limit": "$int",
}
UPDATES_SHAPE: Mapping[str, Any] = {
    "doctype": UPDATE,
    "fields": UPDATE_FIELDS,
    "filters": {"docstatus": 1, "date": [">=", "$date"], "project": ["in", "$projects"]},
    "limit": "$int",
}
READ_LIMIT = 500
# An update counts when dated this many days back or later: the team writes
# one each Friday, so last Friday's is still this week's when today's isn't in.
UPDATE_DAYS = 7
NOTICE_MAX = 10
LATE_TASKS_SHOWN = 5
# Every ERPNext name or status, capped before it is cleaned.
TEXT_MAX = 140
# The team's status text: its raw length kept as it is read (each users row,
# and again all of an update's rows joined) before tags are dropped and it is
# cleaned, and the most of it quoted (on the notice and to the model), marked
# with CUT_MARK when cut. runner.clean bounds itself too (CLEAN_RAW_FACTOR).
STATUS_RAW_MAX = 1200
QUOTE_MAX = 300
CUT_MARK = "..."
# A late task's subject on a notice line, so five fit within the line's
# width with the count first; a held project's title likewise.
SUBJECT_SHOWN = 36
TITLE_SHOWN = 100
DAYS_MAX = 9999
SHUT = frozenset({"completed", "cancelled"})
ON_HOLD = "on hold"
TASK_SHUT = frozenset({"completed", "cancelled", "template"})
# A tag as long as the kept text (a pasted inline image's data URI), so none
# leaves a fragment; and one the raw cut left open.
# Only a tag-like start (a letter, /, ! or ?), so a bare comparison sign
# in prose ("cost < budget, hours > plan") isn't taken for one.
_TAG = re.compile(rf"<[/!?A-Za-z][^<>]{{0,{STATUS_RAW_MAX}}}>")
_OPEN_TAG = re.compile(rf"<[/!?A-Za-z][^<>]{{0,{STATUS_RAW_MAX}}}$")
# The sentence code writes for each open project, by where it stands on time
# and where its cost stands against the estimate (``standing``).
STANDING = {
    ("behind", "over"): "Behind schedule and over its estimated cost",
    ("behind", "within"): "Behind schedule and within its estimated cost",
    ("behind", "none"): "Behind schedule, with no cost estimate",
    ("on", "over"): "On schedule but over its estimated cost",
    ("on", "within"): "On schedule and within its estimated cost",
    ("on", "none"): "On schedule, with no cost estimate",
    ("no_end", "over"): "Over its estimated cost, with no end date set",
    ("no_end", "within"): "Within its estimated cost, with no end date set",
    ("no_end", "none"): "No end date or cost estimate set",
}
LATE_TASKS_CLAUSE = "; it has late tasks"
OPEN_QUOTE, CLOSE_QUOTE = "\u201c", "\u201d"
NO_UPDATE = "No status update this week"
UNSHOWN_UPDATE = "An update was posted; its text couldn't be shown"
MORE_LINE = "{n} more open projects; the first {shown} are shown."
MORE_HELD_LINE = "{n} more projects on hold; the first {shown} are shown."
PROJECTS_CUT_LINE = (
    "Not every open project could be read in full this run: there were more than 50, or a project's "
    "name couldn't be sent in the read; those are left out."
)
TASKS_CUT_LINE = "Not every open task could be read this run, so some late tasks may be missing."
UPDATES_CUT_LINE = "Not every project update could be read this run, so some projects may show none."


# --------------------------------------------------------------------------- #
# Reading the results
# --------------------------------------------------------------------------- #


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _day(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _name(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= TEXT_MAX else ""


def _rows(result: Mapping[str, Any], *, required: bool) -> list[Mapping[str, Any]]:
    """A result's rows. A skipped read is {} and none."""
    if not result and not required:
        return []
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ValueError("no rows")
    return [r for r in rows if isinstance(r, Mapping)]


def _number(value: float, places: int = 1) -> str:
    text = f"{round(value, places):,.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


@dataclass(frozen=True)
class Project:
    name: str
    title: str
    status: str
    customer: str
    percent: float | None
    end: date | None
    hours: float | None
    estimate: float | None
    cost: float | None
    billed: float

    @property
    def on_hold(self) -> bool:
        return self.status.casefold() == ON_HOLD

    def days_late(self, today: date) -> int:
        return min((today - self.end).days, DAYS_MAX) if self.end is not None and self.end < today else 0

    @property
    def over(self) -> float | None:
        """How far its cost passes the estimate, a share (0.4 is 40%); 0
        within it, None with no estimate."""
        if self.estimate is None or self.estimate <= 0:
            return None
        if self.cost is None or self.cost <= self.estimate:
            return 0.0
        return self.cost / self.estimate - 1

    @property
    def hours_over(self) -> float | None:
        """The hours used past the estimate: the hours used, by the share of
        the cost that passes it. None when not over or no hours."""
        over = self.over
        if not over or self.hours is None or self.hours <= 0 or self.cost is None:
            return None
        return self.hours * over / (1 + over)


def projects(result: Mapping[str, Any]) -> list[Project]:
    """Read 1's projects that count: named, and not Completed or Cancelled
    (dropped in code; the read leaves out Completed only)."""
    out: list[Project] = []
    seen: set[str] = set()
    for row in _rows(result, required=True):
        name = _name(row.get("name"))
        status = _name(row.get("status"))
        if not name or name in seen or status.casefold() in SHUT:
            continue
        seen.add(name)
        percent = _amount(row.get("percent_complete"))
        out.append(Project(
            name=name,
            title=_name(row.get("project_name")) or name,
            status=status,
            customer=_name(row.get("customer")),
            percent=min(max(percent, 0.0), 100.0) if percent is not None else None,
            end=_day(row.get("expected_end_date")),
            hours=_amount(row.get("actual_time")),
            estimate=_amount(row.get("estimated_costing")),
            cost=_amount(row.get("total_costing_amount")),
            billed=_amount(row.get("total_billed_amount")) or 0.0,
        ))
    return out


def _pre_order(project: Project, today: date) -> tuple[Any, ...]:
    """Read 1's order: active projects worst first, then those on hold."""
    return (project.on_hold, -project.days_late(today), -(project.over or 0.0), -project.billed, project.name)


def selected(result: Mapping[str, Any], today: date) -> tuple[list[Project], bool]:
    """The projects reads 2 and 3 ask about, in read 1's order, at most
    ``MAX_LIST``, and whether any was left out: past the 50, or a name the
    ``$projects`` slot wouldn't take (never sent)."""
    from openexecutive.watch.registry import _slot_holds

    chosen: list[Project] = []
    left_out = False
    for project in sorted(projects(result), key=lambda p: _pre_order(p, today)):
        if not _slot_holds("$projects", [project.name], today) or len(chosen) >= MAX_LIST:
            left_out = True
            continue
        chosen.append(project)
    return chosen, left_out


# --------------------------------------------------------------------------- #
# The reads' arguments
# --------------------------------------------------------------------------- #


def _projects(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    return {
        "doctype": PROJECT,
        "fields": list(PROJECT_FIELDS),
        "filters": {"company": settings["company"], "status": ["!=", "Completed"]},
        "limit": READ_LIMIT,
    }


def _tasks(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    chosen, cut = selected(prior[0], today) if prior else ([], False)
    if not chosen:
        return None
    return Derived({
        "doctype": TASK,
        "fields": list(TASK_FIELDS),
        "filters": {
            "company": settings["company"],
            "status": ["!=", "Completed"],
            "project": ["in", [p.name for p in chosen]],
        },
        "limit": READ_LIMIT,
    }, truncated=cut)


def _updates(settings: Settings, today: date, prior: Prior = ()) -> ReadArguments:
    chosen, cut = selected(prior[0], today) if prior else ([], False)
    if not chosen:
        return None
    return Derived({
        "doctype": UPDATE,
        "fields": list(UPDATE_FIELDS),
        "filters": {
            "docstatus": 1,
            "date": [">=", (today - timedelta(days=UPDATE_DAYS)).isoformat()],
            "project": ["in", [p.name for p in chosen]],
        },
        "limit": READ_LIMIT,
    }, truncated=cut)


# --------------------------------------------------------------------------- #
# Tasks and updates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LateTask:
    subject: str
    due: date


def late_tasks(result: Mapping[str, Any], today: date) -> dict[str, list[LateTask]]:
    """Each project's late open tasks, oldest first: not Completed,
    Cancelled or a template, due before today."""
    out: dict[str, list[LateTask]] = {}
    seen: set[str] = set()
    for row in _rows(result, required=False):
        name, project = _name(row.get("name")), _name(row.get("project"))
        due = _day(row.get("exp_end_date"))
        if not name or not project or name in seen or due is None or due >= today:
            continue
        if _name(row.get("status")).casefold() in TASK_SHUT:
            continue
        seen.add(name)
        out.setdefault(project, []).append(LateTask(_name(row.get("subject")) or name, due))
    for found in out.values():
        found.sort(key=lambda t: (t.due, t.subject))
    return out


def settled(text: str, limit: int = QUOTE_MAX) -> str:
    """``text`` cleaned as a notice cleans it, at most ``limit``, again until
    cleaning changes nothing ("" when it never settles)."""
    from openexecutive.watch.runner import clean

    for _ in range(5):
        again = clean(text, limit)
        if again == text:
            return text
        text = again
    return ""


def quoted(raw: object) -> str:
    """The team's status text as the job quotes it: its raw length capped
    before anything else, tags dropped, entities read, then cleaned (one
    line, no links, no markdown) to at most ``QUOTE_MAX``, ending in
    ``CUT_MARK`` when it was cut: at ``QUOTE_MAX``, or already at
    ``STATUS_RAW_MAX`` as written, however short it cleans to."""
    if not isinstance(raw, str):
        return ""
    raw_cut = len(raw) > STATUS_RAW_MAX
    text = _TAG.sub(" ", raw[:STATUS_RAW_MAX])
    if raw_cut:
        # A tag the raw cut left open goes too ("a < b" uncut is prose).
        text = _OPEN_TAG.sub(" ", text)
    text = html.unescape(text)
    # Cleaned a little past the quote (its trailing space is stripped), to
    # tell whether it was cut.
    full = settled(text, QUOTE_MAX + 40)
    if len(full) <= QUOTE_MAX and not raw_cut:
        return full
    if not full:
        return ""
    return settled(full[:QUOTE_MAX - len(CUT_MARK)].rstrip() + CUT_MARK)


def _status_value(row: Mapping[str, Any]) -> Any:
    """A users row's status text, under its dotted key or ERPNext's own (a
    child field comes back by its bare name)."""
    return row["users.project_status"] if "users.project_status" in row else row.get("project_status")


def team_words(result: Mapping[str, Any], today: date) -> dict[str, str]:
    """Each project's latest update of the week (by date, then name; one
    dated after today is never this week's), its users' status texts in
    order, as ``quoted``. A project whose latest update quotes as nothing
    (all markup) maps to "", which the notice tells as ``UNSHOWN_UPDATE``."""
    latest: dict[str, tuple[date, str]] = {}
    texts: dict[str, list[str]] = {}
    for row in _rows(result, required=False):
        name, project = _name(row.get("name")), _name(row.get("project"))
        day = _day(row.get("date"))
        if not name or not project or day is None or day > today:
            continue
        held = latest.get(project)
        if held is None or (day, name) > held:
            latest[project] = (day, name)
        text = _status_value(row)
        if isinstance(text, str):
            # One past the cut, so quoted() still sees that it was cut.
            texts.setdefault(name, []).append(text[:STATUS_RAW_MAX + 1])
    # Every project with an update this week: "" when its text quotes as
    # nothing (all markup), which the notice says in code's words.
    out: dict[str, str] = {}
    for project, (_day_of, name) in latest.items():
        out[project] = quoted(" ".join(texts.get(name, []))[:STATUS_RAW_MAX + 1])
    return out


# --------------------------------------------------------------------------- #
# The notice
# --------------------------------------------------------------------------- #


def _hours_line(project: Project) -> str:
    used = f"{_number(project.hours)} hours used" if project.hours is not None else "hours used not recorded"
    over = project.over
    if over is None:
        return f"{used}; no estimate"
    hours = project.hours_over
    if over > 0 and hours is not None and round(hours) >= 1:
        return f"{used}; {_number(hours)} hours over the estimate at cost, {round(over * 100)}%"
    if over > 0 and round(over * 100) >= 1:
        return f"{used}; {round(over * 100)}% over the estimated cost"
    return f"{used}; within the estimate"


def _schedule_line(project: Project, today: date) -> str:
    if project.end is None:
        return "no end date"
    days = project.days_late(today)
    if days:
        return f"{_plural(days, 'day')} late, due {project.end.isoformat()}"
    return f"on plan, due {project.end.isoformat()}"


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - len(CUT_MARK)].rstrip() + CUT_MARK


def _tasks_line(found: Sequence[LateTask]) -> str:
    """The count first (and how many aren't shown), then the oldest five,
    each subject shortened so the line fits its width."""
    shown = "; ".join(
        f"{_short(t.subject, SUBJECT_SHOWN)}, due {t.due.isoformat()}" for t in found[:LATE_TASKS_SHOWN]
    )
    rest = len(found) - LATE_TASKS_SHOWN
    return f"{len(found)}" + (f", {rest} not shown" if rest > 0 else "") + f": {shown}"


@dataclass(frozen=True)
class Status:
    project: Project
    late: tuple[LateTask, ...]
    words: str
    # An update was posted this week, whether or not its text could be quoted.
    posted: bool = False

    def order(self, today: date) -> tuple[Any, ...]:
        p = self.project
        return (-p.days_late(today), -(p.over or 0.0), -len(self.late), -p.billed, p.name)


def standing(project: Project, late: int, today: date) -> str:
    """The "In short" sentence: ``STANDING``'s for where ``project`` stands
    on time and on cost, and ``LATE_TASKS_CLAUSE`` when it has ``late``
    tasks. From the figures alone, the same ones the notice's own lines
    show (``_schedule_line``, ``_hours_line``)."""
    schedule = "no_end" if project.end is None else "behind" if project.days_late(today) else "on"
    over = project.over
    if over is None:
        cost = "none"
    else:
        hours = project.hours_over
        shown_over = over > 0 and ((hours is not None and round(hours) >= 1) or round(over * 100) >= 1)
        cost = "over" if shown_over else "within"
    return STANDING[(schedule, cost)] + (LATE_TASKS_CLAUSE if late else "") + "."


def _row(status: Status, today: date) -> CardSpec:
    """A project as the notice shows it: code's lines, then the team's words
    quoted."""
    project = status.project
    values: list[tuple[str, str]] = [("Project", project.name)]
    if project.customer:
        values.append(("Customer", project.customer))
    if project.percent is not None:
        values.append(("Complete", f"{_number(project.percent, 0)}%"))
    values.append(("Hours", _hours_line(project)))
    values.append(("Schedule", _schedule_line(project, today)))
    if status.late:
        values.append(("Late tasks", _tasks_line(status.late)))
    values.append(("In short", standing(project, len(status.late), today)))
    shown = _quote(status.words) if status.words else ""
    if shown:
        values.append(("The team wrote", shown))
    elif status.words:
        values.append(("Update", UNSHOWN_UPDATE))
    else:
        values.append(("Update", UNSHOWN_UPDATE if status.posted else NO_UPDATE))
    return CardSpec(
        key=f"project:{project.name}:{today.isoformat()}", summary=project.title, values=tuple(values),
    )


# Every quotation mark, prime or quote ornament a team's text could close the
# quote with or imitate one with (two single marks look like a double one),
# as written or once a notice's cleaning (NFKC) reads it: made a plain
# apostrophe, and a run of them one apostrophe (``_quote``). NFKC has already
# split a double prime into two primes by then. Other look-alike symbols are
# a known residual: the quote stays on its own labelled line, inside the
# untrusted block, under code's own "In short".
_QUOTE_MARKS = str.maketrans({mark: "'" for mark in (
    '"\u201c\u201d\u201e\u201f\u00ab\u00bb\u2033\u2036\u301d\u301e\u301f\uff02'
    "`\u2018\u2019\u201a\u201b\u2032\u2035\u2039\u203a\u275b\u275c\u275d\u275e\u02ba\u02bb\u02bc\u02ee\uff07"
)})
_APOSTROPHES = re.compile(r"'{2,}")
# A double mark that is a unit, after a number, said in words before marks
# become apostrophes, so a quantity never changes ('2" conduit' would show as
# "2' conduit", a foot): seconds after degrees and minutes, inches otherwise.
# The words are already cleaned (NFKC), so a double prime arrives as two.
_DOUBLE_MARK = "(?:[\"\u201c\u201d\u02ba\u02ee\uff02]|[\u2032'\u2019]{2})"
# Seconds of arc: a double mark right after the seconds of "45° 30' 15".
_ARC_SECONDS = re.compile(
    rf"(\d\s?\u00b0\s?\d+(?:\.\d+)?\s?[\u2032'\u2019]\s?\d+(?:\.\d+)?){_DOUBLE_MARK}(?=[\s.,;:!?)]|$)"
)
# Any double mark, to walk a text's quoted spans and tell "phase 2" (a mark
# that closes a span) from 2" (inches).
_SPAN_MARK = re.compile(rf"{_DOUBLE_MARK}|[„‟«»]")
# Marks that only open a span („fáze 2“, «2»), never inches, and one that
# only closes it.
_SPAN_OPENERS = frozenset("„‟«")
_SPAN_CLOSER = "»"
# What may follow an inch mark: a space, punctuation, the end, or the
# "x", "×", "-" and "/" of 2"x4", 3/4"-thick and 2"/3".
_UNIT_AFTER = re.compile(r"[\s.,;:!?)x×/-]|$")


def _units(words: str) -> str:
    """``words`` with a double mark that is a unit written as one: " sec"
    after degrees and minutes, else " in". A mark is inches only right after
    a digit, followed by a space, punctuation or the end, and not closing a
    quoted span the text opened ("phase 2", room 4 "the big one" keep their
    marks). Walked left to right; an inch mark opens no span, and „ ‟ «
    always open one. A quote inside a quote ('He said "use 2" pipe"') and a
    double mark after degrees and minutes that means inches are read the
    one way; a letter right after an inch mark (6"dia) keeps it a mark:
    known residuals."""
    text = _ARC_SECONDS.sub(r"\1 sec", words)
    out: list[str] = []
    at = 0
    open_span = False
    for mark in _SPAN_MARK.finditer(text):
        before = text[at:mark.start()]
        start = mark.start()
        if mark.group(0) in _SPAN_OPENERS:
            open_span = True
        elif open_span or mark.group(0) == _SPAN_CLOSER:
            open_span = False
        elif start and text[start - 1].isdigit() and _UNIT_AFTER.match(text, mark.end()):
            out.append(before + " in")
            at = mark.end()
            continue
        else:
            open_span = True
        out.append(before + mark.group(0))
        at = mark.end()
    out.append(text[at:])
    return "".join(out)


def _cut(text: str) -> str:
    """``text`` at most ``QUOTE_MAX``, ending in ``CUT_MARK`` when it was cut."""
    if len(text) <= QUOTE_MAX:
        return text
    return text[:QUOTE_MAX - len(CUT_MARK)].rstrip() + CUT_MARK


def _quote(words: str) -> str:
    """The team's words in quotation marks that hold: no mark of their own
    inside (``_QUOTE_MARKS``), so text in them that imitates code's labels
    ("In short: on schedule") stays the team's, and the whole a fixed point
    of the notice's cleaning, so the closing mark is never lost (a text
    ending "www." would join it into a link). "" when no form holds."""
    units = _units(words)
    # A combining mark on an apostrophe would keep two of them apart ("'\u0301'").
    kept: list[str] = []
    for ch in units.translate(_QUOTE_MARKS):
        if kept and kept[-1] == "'" and unicodedata.category(ch).startswith("M"):
            continue
        kept.append(ch)
    inner = _cut(_APOSTROPHES.sub("'", "".join(kept)))
    # Settled again: a mark that hid a link from the first cleaning
    # ("\u02bchttp://x") exposes it now, and it reads "link" rather than the
    # whole quote failing. Settled past the limit so cleaning never cuts it
    # silently; any cut is ours, with its mark (written-out units lengthen it).
    cut = inner.endswith(CUT_MARK)
    again = settled(inner, QUOTE_MAX + 40)
    inner = _APOSTROPHES.sub("'", settled(_cut(again)) if again else inner)
    if cut and not inner.endswith(CUT_MARK):
        # Settling at the cut joined the mark into a link ("...http://a..."):
        # the quote still says it was cut.
        inner = settled(inner[:QUOTE_MAX - len(CUT_MARK) - 1].rstrip() + " " + CUT_MARK) or inner
    if not inner:
        return ""
    # The closing mark spaced off when the text's end would take it in.
    for wrapped in (f"{OPEN_QUOTE}{inner}{CLOSE_QUOTE}", f"{OPEN_QUOTE}{inner} {CLOSE_QUOTE}"):
        if (
            settled(wrapped, QUOTE_MAX + 2) == wrapped
            and wrapped.count(OPEN_QUOTE) == 1 and wrapped.count(CLOSE_QUOTE) == 1
        ):
            return wrapped
    return ""


def _held_row(held: Sequence[Status], today: date) -> CardSpec:
    """The closing line: each project on hold with its overdue tasks."""
    values = []
    for status in held:
        late = status.late
        # The count and date first, the title shortened, so the line keeps
        # them within its width.
        said = (
            f"{_plural(len(late), 'overdue task')}, the oldest due {late[0].due.isoformat()}: "
            f"{_short(late[0].subject, SUBJECT_SHOWN)}"
            if late else "no overdue task"
        )
        values.append(("Project", f"{said}; {_short(status.project.title, TITLE_SHOWN)}"))
    return CardSpec(key=f"on-hold:{today.isoformat()}", summary="On hold", values=tuple(values))


def build(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> list[CardSpec | DraftSpec]:
    """The "Projects this week" notice, figures from the reads only."""
    projects_result, tasks_result, updates_result = reads
    chosen, left_out = selected(projects_result, today)
    if not chosen:
        return []
    late = late_tasks(tasks_result, today)
    words = team_words(updates_result, today)
    statuses = [
        Status(p, tuple(late.get(p.name, ())), "" if p.on_hold else words.get(p.name, ""), p.name in words)
        for p in chosen
    ]
    active = sorted((s for s in statuses if not s.project.on_hold), key=lambda s: s.order(today))
    held = [s for s in statuses if s.project.on_hold]
    shown = active[:NOTICE_MAX]
    rows = [_row(s, today) for s in shown]
    if held:
        rows.append(_held_row(held[:NOTICE_MAX], today))
    notes: list[str] = []
    if len(active) > NOTICE_MAX:
        notes.append(MORE_LINE.format(n=len(active) - NOTICE_MAX, shown=NOTICE_MAX))
    if len(held) > NOTICE_MAX:
        notes.append(MORE_HELD_LINE.format(n=len(held) - NOTICE_MAX, shown=NOTICE_MAX))
    if left_out or projects_result.get("truncated") is True:
        notes.append(PROJECTS_CUT_LINE)
    if tasks_result.get("truncated") is True:
        notes.append(TASKS_CUT_LINE)
    if updates_result.get("truncated") is True:
        notes.append(UPDATES_CUT_LINE)
    return [CardSpec(
        key=f"projects:{settings['company']}:{today.isoformat()}",
        summary="Projects this week",
        items=tuple(rows),
        notes=tuple(notes),
        count=len(active) + len(held),
    )]


JOB: WatchJob = WatchJob(
    id=JOB_ID,
    title="Project status",
    reads=(
        WatchRead("list_records", PROJECTS_SHAPE, _projects, "the open projects"),
        WatchRead("list_records", TASKS_SHAPE, _tasks, "their open tasks"),
        WatchRead("list_records", UPDATES_SHAPE, _updates, "this week's project updates"),
    ),
    build=build,
    # Friday afternoon, after the team's Project Updates.
    schedule=time(17, 0),
    weekdays=frozenset({4}),
    card_cap_day=1,
    default_settings={"company": ""},
    # The team's words are quoted up to QUOTE_MAX, in quotation marks.
    value_chars=QUOTE_MAX + 2,
    card_noun="cards",
)
