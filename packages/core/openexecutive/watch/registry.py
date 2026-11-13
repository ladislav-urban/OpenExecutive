"""The watch jobs, in code only.

A job is a ``WatchJob``: the reads it makes (``WatchRead``: an oe_connector
tool and the shape of its arguments, the same entry and slot names as the
job's ``oe_connector_watch_jobs`` listing on the ERPNext side), the rules that
turn their results into notices and draft cards (``build``, pure code
returning ``CardSpec``\\ s and ``DraftSpec``\\ s), when it runs (once a day at
``schedule``, or at each of its ``slots`` on its ``weekdays``), how many cards
a day it may leave, and the settings the principal may change.

A read may depend on the ones before it (``WatchRead.arguments`` gets their
results): collections asks for the payment record of the customers its
receivables read found overdue. A derived list that comes out empty skips
the read (``None``); one cut to its first 50 says so (``Derived``).

There is no way to add a job at run time: no settings row, chat turn or API
names one. ``JOBS`` is read-only and holds what this module lists in
``_LISTED``: phase 1's collections job (``watch.jobs.collections``), its
payment-checks job (``watch.jobs.payment_checks``), its quote-follow-up
job (``watch.jobs.quote_follow_up``), its late-deliveries job
(``watch.jobs.late_deliveries``) and its project-status job
(``watch.jobs.project_status``), and phase 2's bank-matching job
(``watch.jobs.bank_matching``) and invoice-capture job
(``watch.jobs.invoice_capture``). The test job,
``watch.jobs._example``, is put in by the tests alone.

A read may also be **repeated** (``WatchRead.repeat``): its ``arguments``
return ``Repeated``, one call's arguments per item (bank matching's
candidates, one read per bank line), at most ``repeat`` of them and never
more than ``MAX_REPEAT``. Each call is made on its own: its own shape check,
read marker, ``acting`` record and timeout. ``build`` gets ``{"each":
[results...], "truncated": bool}`` for it.

A tool the gateway withholds from every turn
(``connected_systems.ERPNEXT_WITHHELD_TOOLS``) may be listed only by the job
``JOB_WITHHELD_EXCEPTIONS`` gives it to, and the gateway opens it only under
that job's own read.

**Shapes** are oe_connector's (its ``watch.py``, ladislav-urban/oe_connector#536):

- a literal matches only the same JSON type and value (``true`` is never
  ``1``, and ``1.0`` never ``1``);
- a typed slot takes any value of its type (``SLOTS``);
- the top-level ``fields`` list is the most a call may ask for;
- any other list matches a list of the same length, element by element: a
  filter such as ``["<=", "$date"]`` or ``["in", "$parties"]``, never "any
  list";
- an object must have exactly the listed keys.

The runner checks each read's arguments here before the call, and
oe_connector checks them again, as the job's ERP user, when it answers. A
read whose shape breaks oe_connector's soundness rules (``shape_problem``: a
mistyped slot, a list or object inside a list, a slot in a list anywhere but
``[operator, slot]``, a list slot as a filter's value on its own) is refused
when its job is defined, so the registry never imports one.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, time
from types import MappingProxyType
from typing import Any

# oe_connector's watch.JOB_ID: the key in oe_connector_watch_jobs, and the
# `sub` of a watch assertion.
JOB_ID = re.compile(r"[a-z0-9-]{1,40}")
SLOTS: tuple[str, ...] = (
    "$company", "$date", "$party", "$parties", "$customers", "$projects", "$int", "$bank_line", "$mail_draft",
    "$mail_file",
)
# The list slots (oe_connector's watch.LIST_SLOTS): up to MAX_LIST names,
# only after in or not in. oe_connector also checks each name is a document
# of the slot's DocType the call's user may read (its LIST_SLOT_DOCTYPES:
# Supplier, Customer, Project); here, that each is a name.
LIST_SLOTS: tuple[str, ...] = ("$parties", "$customers", "$projects")
# Slots this side only: a value oe_connector's listing holds as a literal,
# one read per value (an operator lists bank matching's
# ``bank_lines_to_match`` once per Bank Account). Checked here as a name;
# oe_connector checks the literal and its own list of accounts.
LOCAL_SLOTS: tuple[str, ...] = ("$bank_account",)
# Slots that only mean something together, as a read_attachment read's
# top-level name and file, beside "doctype": "Purchase Invoice" (the
# invoice-capture job's mailed draft and its mail's one file; oe_connector's
# watch.MAIL_SLOTS). Anywhere else, or on another tool, the read is unsound.
MAIL_SLOTS: Mapping[str, str] = MappingProxyType({"$mail_draft": "name", "$mail_file": "file"})
MAIL_TOOL = "read_attachment"
# The operators a slot may follow inside a list: a single value's, or a list
# slot's (in, not in). Never like, where a name's % or _ would widen the read.
VALUE_OPERATORS: tuple[str, ...] = ("=", "!=", "<", "<=", ">", ">=")
LIST_OPERATORS: tuple[str, ...] = ("in", "not in")
DATE_RANGE_DAYS = 400
MAX_LIST = 50
MAX_INT = 500
# The most calls one repeated read makes in a run.
MAX_REPEAT = 50
# A Company, Customer or Supplier name, and a setting's text (oe_connector
# bounds a supplier's name at 140 too).
MAX_TEXT = 140
# The gateway's name for an oe_connector tool is this plus its own name.
GATEWAY_PREFIX = "erpnext__"
# A document's name as a slot takes it (a Bank Transaction's, a Bank
# Account's): no control characters, no leading or trailing space.
_NAME_RE = re.compile(r"[^\x00-\x1f\x7f-\x9f\s](?:[^\x00-\x1f\x7f-\x9f]{0,138}[^\x00-\x1f\x7f-\x9f\s])?")
# The oe_connector tools any job may list. A withheld one is listed only by
# the job connected_systems.JOB_WITHHELD_EXCEPTIONS gives it to.
READ_TOOLS: frozenset[str] = frozenset({"run_report", "list_records"})
MAX_CARD_CAP = 50

Settings = Mapping[str, Any]


class SettingsError(ValueError):
    """Settings that don't fit the job's; the message is ours, for a 400."""


@dataclass(frozen=True)
class Derived:
    """A read's arguments built from the reads before it, with whether a
    list in them was cut to its first ``MAX_LIST`` names (the job's own
    order). The run records the cut."""

    arguments: dict[str, Any]
    truncated: bool = False


@dataclass(frozen=True)
class Repeated:
    """A repeated read's calls this run: one arguments dict per item, in the
    job's order, and whether the job's own list was cut. The runner makes at
    most the read's ``repeat`` of them and records any more as cut."""

    arguments: tuple[dict[str, Any], ...]
    truncated: bool = False


# The results of the reads before this one, in order, each without its
# ``acting`` key; a skipped read's is {}.
Prior = tuple[Mapping[str, Any], ...]
# A read's arguments: a dict, a ``Derived`` one, or None to skip the read
# (a derived list came out empty: never sent with an empty list); for a
# repeated read, ``Repeated`` (or None).
ReadArguments = dict[str, Any] | Derived | Repeated | None


@dataclass(frozen=True)
class WatchRead:
    """One read a job makes: oe_connector's ``tool`` with arguments of
    ``shape``, built for each run by ``arguments`` (the run's settings, its
    day and the earlier reads' results). ``label`` is how a notice names it
    ("the receivables report")."""

    tool: str
    shape: Mapping[str, Any]
    arguments: Callable[[Settings, date, Prior], ReadArguments]
    label: str
    # A repeated read: at most this many calls a run (1 to MAX_REPEAT), its
    # arguments ``Repeated``. 0: one call.
    repeat: int = 0
    # A repeated read whose one call may be refused without ending the run
    # (one supplier's unreadable PDF): that item's result is
    # ``{"unread": True}``, which the job's rules treat as "needs a person",
    # never a card. Only on a repeated read.
    may_fail: bool = False

    @property
    def gateway_tool(self) -> str:
        return GATEWAY_PREFIX + self.tool


@dataclass(frozen=True)
class CardSpec:
    """One finding a job's rules leave the principal a notice for.

    - ``key``: the finding's own id in code (e.g. ``overdue:<customer>``).
      Only its digest is kept, as the notice's dedupe key, and it's what the
      reason step keys its line by, never shown.
    - ``summary``: the notice's one line.
    - ``values``: (label, value) pairs; the label is code's, the value
      ERPNext's.
    - ``facts``: what the reason step is shown about the finding (bounded).

    Every string here may carry ERPNext's text, so the runner strips and caps
    each one and wraps the body as untrusted; the notice's subject never uses
    any of it."""

    key: str
    summary: str
    values: tuple[tuple[str, str], ...] = ()
    facts: Mapping[str, str | int | float] = field(default_factory=dict)
    # The notice's rows (e.g. each customer to chase), each a finding of its
    # own for the reason step and shown with its reason under the summary.
    items: tuple[CardSpec, ...] = ()
    # Lines in code's own words (counts only, never ERPNext's text), shown
    # outside the untrusted block.
    notes: tuple[str, ...] = ()
    # What the notice's subject counts, when more than its rows (0: its rows,
    # or one).
    count: int = 0
    # Left for the principal whatever the run's route: a check on what the
    # reviewer approved, or a finding ERPNext kept from the reviewer (their
    # permissions, or separation of duties), never told back to them.
    to_principal: bool = False
    # A ``once`` job's findings this notice counts without naming (the
    # quote-follow-up job's quotations to leads): recorded as told with its
    # rows once the notice is left. Keys only, never shown.
    counted: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftSpec:
    """One draft card a job's rules propose: the operation, DocType and
    fields, built in code from the reads (never by a model) and checked again
    by the job's ``check_fields`` before ERPNext's own check, which runs as
    the job's reviewer.

    - ``key``: the finding's id in code (``dunning:<invoice>:<type>``); only
      its digest is kept, as the card's dedupe key.
    - ``expect``: what ERPNext's check must echo in its summary (e.g. the
      invoice and its customer), or no card is left.
    - ``title``: the card's headline, code's words around ERPNext's names
      (cleaned when the card is left).
    - ``reason_key``: the notice item whose reason line the card shows as
      "Why now".
    - ``group``: what the notice groups it under (a customer).
    - ``name``: the document a change or a block is on ("" for a new draft).
    - ``notice``: the finding told as a notice when no card can be left for
      it (the run went to the principal, the reviewer can't see the
      document, the day's cap); None when the job's notices already cover it.
    - ``shown``: (label, value) lines the card shows besides ERPNext's check,
      built by code from the reads (bank matching's weaker candidates, never
      proposed); never sent, and never in its notice."""

    key: str
    operation: str
    doctype: str
    fields: Mapping[str, Any]
    expect: Mapping[str, Any]
    title: str
    reason_key: str = ""
    group: str = ""
    name: str = ""
    notice: CardSpec | None = None
    shown: tuple[tuple[str, str], ...] = ()


# A job's own builder of a card's fields, by operation, DocType and the
# document's name ("" for a new draft): the fields as they will be checked,
# stored and sent, or why not. Run before each check and again at approval,
# so a tampered card is refused.
CheckFields = Callable[[str, str, Mapping[str, Any], str], dict[str, Any] | str]


# How long a text fact the reason step shows the model, and a notice shows
# a value, unless a job's ReasonSchema says longer (at most MAX_FACT_CHARS).
DEFAULT_FACT_CHARS = 120
MAX_FACT_CHARS = 400


@dataclass(frozen=True)
class ReasonSchema:
    """A job's reason step: one short line per finding, written by a model
    from the findings' ``facts``. ``instruction`` says what a line explains
    (a constant in the job's code). ``max_fact_chars``: how long a text fact
    may be, cleaned (``DEFAULT_FACT_CHARS`` to ``MAX_FACT_CHARS``); the
    job's notice values are cut at the same length, so a quoted text reads
    the same on the notice as the model was shown it.
    ``no_digits``: a line holding any digit or number word is dropped
    (``runner.without_digits``); the job's own lines carry every figure, so
    its model line says only in words what they mean. ``per_finding``: one
    call per finding, each shown that finding's facts alone, rather than one
    call for all."""

    instruction: str
    max_findings: int = 20
    max_fact_chars: int = DEFAULT_FACT_CHARS
    no_digits: bool = False
    per_finding: bool = False


# (each read's result, in the order of ``reads``, {} for a skipped one; the
# run's settings; its day) -> the findings. Pure code: every figure comes
# from the results.
Build = Callable[[Sequence[Mapping[str, Any]], Settings, date], Sequence[CardSpec | DraftSpec]]
# A job's notices that come from this install's own records rather than a
# read (bank matching's monthly list of the matches made): (the run's
# settings, its day) -> notices. Called after ``build``.
ExtraNotices = Callable[[Settings, date], Sequence[CardSpec]]


def _setting_ok(value: object, bounds: tuple[int, int] | None = None) -> bool:
    """A setting's value of its type, within bounds: a whole number's own
    (1 to MAX_INT unless given), or a list's count of names (0 to MAX_LIST
    unless given)."""
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        low, high = bounds or (1, MAX_INT)
        return low <= value <= high
    if isinstance(value, str):
        return _text_ok(value, allow_empty=True)
    if isinstance(value, list):
        low, high = bounds or (0, MAX_LIST)
        return low <= len(value) <= min(high, MAX_LIST) and all(isinstance(v, str) and _text_ok(v) for v in value)
    return False


@dataclass(frozen=True)
class WatchJob:
    id: str
    title: str
    reads: tuple[WatchRead, ...]
    build: Build
    # Its hour (and minute) each day, in the company's time zone; for a job
    # with ``slots``, its first.
    schedule: time
    card_cap_day: int
    default_settings: Mapping[str, Any]
    reason_schema: ReasonSchema | None = None
    # Its draft cards' builder (``CheckFields``); None: a job of notices only.
    check_fields: CheckFields | None = None
    # A whole-number setting's bounds other than 1 to MAX_INT (e.g. an
    # amount), or a list setting's count of names other than 0 to MAX_LIST.
    setting_bounds: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    # What a notice calls its draft cards ("reminder drafts").
    card_noun: str = "drafts"
    # The times it runs each day (company time), when more than once: a run
    # per slot, the day's card cap counted across them. Empty: once a day at
    # ``schedule``.
    slots: tuple[time, ...] = ()
    # The days it runs (Monday 0 to Sunday 6).
    weekdays: frozenset[int] = frozenset(range(7))
    # Its notices from this install's own records (``ExtraNotices``), if any.
    extra_notices: ExtraNotices | None = None
    # Each finding raised once, ever, rather than once a day: the runner
    # records the findings a left notice told (its key, its rows' keys and
    # ``counted``; ``settings.mark_told``) for the job's rules to leave out
    # (``settings.told``) from the notice only, and a finding a card was ever
    # left for gets no second card, whatever became of the first
    # (``cards.leave_drafts``); one the day's cap held back gets it later.
    once: bool = False
    # A once job whose finding keys carry their own period (late deliveries'
    # ISO week): a hold written when ERPNext refused a finding's check ends
    # with the period. Without one, such a hold lasts ``cards.HOLD_DAYS``.
    periodic_keys: bool = False

    def __post_init__(self) -> None:
        if not JOB_ID.fullmatch(self.id):
            raise ValueError("a job's id is oe_connector's: [a-z0-9-]{1,40}")
        if not self.title or not self.title.isprintable() or len(self.title) > 80:
            raise ValueError("a job's title is one short line")
        if not self.reads:
            raise ValueError("a job reads something")
        from openexecutive.prompts.connected_systems import (
            ERPNEXT_WITHHELD_TOOLS,
            JOB_WITHHELD_EXCEPTIONS,
        )

        # A withheld tool only by the job that owns it (the gateway's one
        # exception mirrored), any other only from READ_TOOLS.
        owned = JOB_WITHHELD_EXCEPTIONS.get(self.id, frozenset())
        for read in self.reads:
            withheld = read.gateway_tool in ERPNEXT_WITHHELD_TOOLS
            if read.tool not in READ_TOOLS and not (withheld and read.gateway_tool in owned):
                raise ValueError(f"a job may not list {read.tool!r}")
            if not isinstance(read.shape, Mapping) or not read.label or not read.label.isprintable():
                raise ValueError("each read has a shape and a label")
            unsound = shape_problem(read.shape) or mail_tool_problem(read.tool, read.shape)
            if unsound:
                raise ValueError(f"a read's shape is unsound: {unsound}")
            if (
                not isinstance(read.repeat, int) or isinstance(read.repeat, bool)
                or not 0 <= read.repeat <= MAX_REPEAT
            ):
                raise ValueError(f"a read repeats at most {MAX_REPEAT} times")
            if not isinstance(read.may_fail, bool) or (read.may_fail and not read.repeat):
                raise ValueError("only a repeated read's call may fail on its own")
        if not (1 <= self.card_cap_day <= MAX_CARD_CAP):
            raise ValueError("a job's card cap is 1 to 50 a day")
        schema = self.reason_schema
        if schema is not None and (
            not isinstance(schema.max_fact_chars, int) or isinstance(schema.max_fact_chars, bool)
            or not DEFAULT_FACT_CHARS <= schema.max_fact_chars <= MAX_FACT_CHARS
        ):
            raise ValueError(f"a reason's text facts are {DEFAULT_FACT_CHARS} to {MAX_FACT_CHARS} characters")
        if schema is not None and not isinstance(schema.no_digits, bool):
            raise ValueError("a reason's no_digits is true or false")
        if schema is not None and not isinstance(schema.per_finding, bool):
            raise ValueError("a reason's per_finding is true or false")
        for key, bound in self.setting_bounds.items():
            if (
                key not in self.default_settings or len(bound) != 2
                or not all(isinstance(b, int) and not isinstance(b, bool) for b in bound) or bound[0] > bound[1]
            ):
                raise ValueError(f"setting {key!r}'s bounds are two whole numbers, low to high")
        for key, value in self.default_settings.items():
            if not isinstance(key, str) or not key or not _setting_ok(value, self.setting_bounds.get(key)):
                raise ValueError(f"setting {key!r} isn't a bool, a number, a line of text or a list of names")
        if not self.card_noun or not self.card_noun.isprintable() or len(self.card_noun) > 40:
            raise ValueError("a job's card noun is a few words")
        if self.slots and (
            list(self.slots) != sorted(set(self.slots)) or self.slots[0] != self.schedule
            or len(self.slots) > 24 or any(s.second or s.microsecond or s.tzinfo for s in self.slots)
        ):
            raise ValueError("a job's slots are distinct times, in order, from its schedule")
        if not self.weekdays or not self.weekdays <= frozenset(range(7)):
            raise ValueError("a job runs on some of the week's days, Monday 0 to Sunday 6")

    def slot_keys(self) -> tuple[tuple[time, str], ...]:
        """Its slots and their keys ("HH:MM"), or its schedule with key ""
        for a job that runs once a day."""
        if not self.slots:
            return ((self.schedule, ""),)
        return tuple((s, s.strftime("%H:%M")) for s in self.slots)

    def due_slots(self, now: time, weekday: int) -> tuple[str, ...]:
        """The keys of the slots due by ``now`` on a day of ``weekday``, in order."""
        if weekday not in self.weekdays:
            return ()
        return tuple(key for at, key in self.slot_keys() if at <= now)

    def tools(self) -> frozenset[str]:
        """The gateway names of the tools it lists."""
        return frozenset(read.gateway_tool for read in self.reads)


def get(job_id: object) -> WatchJob | None:
    """The registered job with this id, or None."""
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        return None
    return JOBS.get(job_id)


# --------------------------------------------------------------------------- #
# Shapes (oe_connector's watch.py, checked here before the call)
# --------------------------------------------------------------------------- #


def _text_ok(value: str, *, allow_empty: bool = False) -> bool:
    return (
        (allow_empty or bool(value))
        and len(value) <= MAX_TEXT
        and value == value.strip()
        and value.isprintable()
    )


def _slot_holds(slot: str, value: Any, today: date) -> bool:
    if slot == "$int":
        return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_INT
    if slot == "$date":
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return False
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return False
        return abs((day - today).days) <= DATE_RANGE_DAYS
    if slot in ("$company", "$party"):
        return isinstance(value, str) and _text_ok(value)
    if slot in ("$bank_line", "$bank_account", "$mail_draft", "$mail_file"):
        return isinstance(value, str) and _text_ok(value) and _NAME_RE.fullmatch(value) is not None
    if slot in LIST_SLOTS:
        return (
            isinstance(value, list)
            and 0 < len(value) <= MAX_LIST
            and all(isinstance(v, str) and _text_ok(v) for v in value)
        )
    return False


# Whether a slot holds a value: the runner's own check (``_slot_holds``), or
# a test's oracle.
SlotHolds = Callable[[str, Any], bool]


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def mail_problem(shape: Any) -> str:
    """A read with a mail slot names a Purchase Invoice as ``$mail_draft``
    and ``$mail_file`` beside it, both at top level (oe_connector's
    ``watch._mail_problem``), or why not."""
    if not isinstance(shape, Mapping):
        return ""
    if not any(isinstance(v, str) and v in MAIL_SLOTS for v in shape.values()):
        return ""
    if (
        shape.get("name") != "$mail_draft" or shape.get("file") != "$mail_file"
        or shape.get("doctype") != "Purchase Invoice"
    ):
        return 'a mail slot needs "doctype": "Purchase Invoice", "name": "$mail_draft" and "file": "$mail_file"'
    return ""


def mail_tool_problem(tool: str, shape: Any) -> str:
    """The mail slots open a mail's PDF to read_attachment, never a draft's
    contents to another tool (oe_connector's ``watch._mail_tool_problem``)."""
    if (
        isinstance(shape, Mapping) and any(isinstance(v, str) and v in MAIL_SLOTS for v in shape.values())
        and tool != MAIL_TOOL
    ):
        return "the mail slots are only for read_attachment"
    return ""


def shape_problem(shape: Any, where: str = "", in_filters: bool = False) -> str:
    """Why a read's shape is unsound (oe_connector's ``watch._shape_problem``),
    or "" when it is sound. An unsound read leaves its job out on both sides.
    A read's tool is checked apart (``mail_tool_problem``)."""
    if not where and isinstance(shape, Mapping):
        why = mail_problem(shape)
        if why:
            return why
    if isinstance(shape, str) and shape.startswith("$"):
        if shape not in SLOTS and shape not in LOCAL_SLOTS:
            return f"{where or 'value'}: {shape} isn't a slot"
        if shape in MAIL_SLOTS and where != MAIL_SLOTS[shape]:
            return f"{where or 'value'}: {shape} stands only as the read's {MAIL_SLOTS[shape]}"
        if in_filters and shape in LIST_SLOTS:
            return f'{where}: a list slot in a filter goes in ["in", {shape}]'
        return ""
    if isinstance(shape, Mapping):
        for key, sub in shape.items():
            path = f"{where}.{key}" if where else str(key)
            why = shape_problem(sub, path, in_filters or key == "filters")
            if why:
                return why
        return ""
    if isinstance(shape, list | tuple):
        if where == "fields":
            ok = all(isinstance(f, str) and not f.startswith("$") for f in shape)
            return "" if ok else "fields: only field names"
        if not all(_is_scalar(e) for e in shape):
            return f"{where}: a list inside a list or an object inside one"
        slots = [e for e in shape if isinstance(e, str) and e.startswith("$")]
        if not slots:
            return ""
        unknown = [e for e in slots if e not in SLOTS]
        if unknown:
            return f"{where}: {unknown[0]} isn't a slot"
        if any(e in MAIL_SLOTS for e in slots):
            return f"{where}: a mail slot never goes in a list"
        if len(shape) != 2 or shape[1] not in SLOTS:
            return f"{where}: a slot in a list only as [operator, slot]"
        operator, slot = shape
        if operator in VALUE_OPERATORS and slot not in LIST_SLOTS:
            return ""
        if operator in LIST_OPERATORS and slot in LIST_SLOTS:
            return ""
        return f"{where}: {slot} can't follow {operator!r}"
    return "" if _is_scalar(shape) else f"{where or 'value'}: not a JSON value"


def shape_mismatch(
    shape: Any, value: Any, today: date, where: str = "", *, holds: SlotHolds | None = None,
) -> str:
    """Why ``value`` doesn't fit ``shape``, or "" when it does (oe_connector's
    ``watch._mismatch``). The reason names keys and slots only, never a
    value. ``holds``: whether a slot takes a value; the runner's own check on
    ``today`` unless given."""
    if isinstance(shape, str) and (shape in SLOTS or shape in LOCAL_SLOTS):
        held = holds(shape, value) if holds is not None else _slot_holds(shape, value, today)
        return "" if held else f"{where or 'value'} isn't a {shape[1:]}"
    if isinstance(shape, Mapping):
        if not isinstance(value, Mapping):
            return f"{where or 'arguments'} should be an object"
        extra = sorted(str(k) for k in set(value) - set(shape))
        if extra:
            return f"{', '.join(extra)[:200]} isn't listed"
        missing = sorted(set(shape) - set(value))
        if missing:
            return f"{', '.join(missing)} is missing"
        for key, sub in shape.items():
            why = shape_mismatch(sub, value[key], today, f"{where}.{key}" if where else key, holds=holds)
            if why:
                return why
        return ""
    if isinstance(shape, list | tuple) and where == "fields":
        # The top-level fields only: the most a call may ask for.
        if not isinstance(value, list) or not value or not all(isinstance(f, str) for f in value):
            return f"{where} should be a list of fields"
        return f"{where} asks for a field that isn't listed" if set(value) - set(shape) else ""
    if isinstance(shape, list | tuple):
        # Any other list: the same length, element by element.
        if not isinstance(value, list) or len(value) != len(shape):
            return f"{where or 'value'} should be a list of {len(shape)}"
        for i, (sub, item) in enumerate(zip(shape, value, strict=True)):
            why = shape_mismatch(sub, item, today, f"{where}[{i}]", holds=holds)
            if why:
                return why
        return ""
    # A literal: the same JSON type and value, so true is never 1 and 1.0 never 1.
    return "" if type(value) is type(shape) and value == shape else f"{where or 'value'} isn't the listed value"


def read_for(job: WatchJob, gateway_tool: str, arguments: Mapping[str, Any], today: date) -> WatchRead | None:
    """The job's read that ``gateway_tool`` with ``arguments`` fits, or None."""
    for read in job.reads:
        if read.gateway_tool == gateway_tool and not shape_mismatch(read.shape, arguments, today):
            return read
    return None


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def check_settings(job: WatchJob, settings: object) -> dict[str, Any]:
    """``settings`` when they have exactly the job's keys, each of its
    default's type and within bounds; raises ``SettingsError``."""
    if not isinstance(settings, Mapping):
        raise SettingsError("Settings are an object.")
    expected = set(job.default_settings)
    given = set(settings)
    if given != expected:
        unknown = sorted(str(k)[:40] for k in given - expected)
        missing = sorted(expected - given)
        parts = ([f"unknown: {', '.join(unknown)[:200]}"] if unknown else []) + (
            [f"missing: {', '.join(missing)}"] if missing else []
        )
        raise SettingsError(f"Settings must have exactly this job's keys ({'; '.join(parts)}).")
    out: dict[str, Any] = {}
    for key, default in job.default_settings.items():
        value = settings[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            low, high = job.setting_bounds.get(key, (1, MAX_INT))
            ok = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
        elif isinstance(default, str):
            ok = isinstance(value, str) and _text_ok(value, allow_empty=True)
        else:
            low, high = job.setting_bounds.get(key, (0, MAX_LIST))
            ok = (
                isinstance(value, list)
                and low <= len(value) <= min(high, MAX_LIST)
                and all(isinstance(v, str) and _text_ok(v) for v in value)
            )
        if not ok:
            raise SettingsError(f"Setting {key} isn't of its type or is out of bounds.")
        out[key] = list(value) if isinstance(value, list) else value
    return out


# The jobs this install runs, each with its entry in oe_connector_watch_jobs.
# Imported here, once everything a job module uses is defined.
from openexecutive.watch.jobs import bank_matching as _bank_matching  # noqa: E402
from openexecutive.watch.jobs import collections as _collections  # noqa: E402
from openexecutive.watch.jobs import invoice_capture as _invoice_capture  # noqa: E402
from openexecutive.watch.jobs import late_deliveries as _late_deliveries  # noqa: E402
from openexecutive.watch.jobs import payment_checks as _payment_checks  # noqa: E402
from openexecutive.watch.jobs import project_status as _project_status  # noqa: E402
from openexecutive.watch.jobs import quote_follow_up as _quote_follow_up  # noqa: E402

_LISTED: tuple[WatchJob, ...] = (
    _collections.JOB, _payment_checks.JOB, _bank_matching.JOB, _invoice_capture.JOB, _quote_follow_up.JOB,
    _late_deliveries.JOB, _project_status.JOB,
)
_JOBS: dict[str, WatchJob] = {job.id: job for job in _LISTED}
JOBS: Mapping[str, WatchJob] = MappingProxyType(_JOBS)
