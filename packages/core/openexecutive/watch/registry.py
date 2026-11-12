"""The watch jobs, in code only.

A job is a ``WatchJob``: the reads it makes (``WatchRead``: an oe_connector
tool and the shape of its arguments, the same entry and slot names as the
job's ``oe_connector_watch_jobs`` listing on the ERPNext side), the rules that
turn their results into notices and draft cards (``build``, pure code
returning ``CardSpec``\\ s and ``DraftSpec``\\ s), when it runs, how many cards
a day it may leave, and the settings the principal may change.

A read may depend on the ones before it (``WatchRead.arguments`` gets their
results): collections asks for the payment record of the customers its
receivables read found overdue. A derived list that comes out empty skips
the read (``None``); one cut to its first 50 says so (``Derived``).

There is no way to add a job at run time: no settings row, chat turn or API
names one. ``JOBS`` is read-only and holds what this module lists in
``_LISTED``: phase 1's collections job (``watch.jobs.collections``). The
test job, ``watch.jobs._example``, is put in by the tests alone.

A tool the gateway withholds from every turn
(``connected_systems.ERPNEXT_WITHHELD_TOOLS``) may be listed only by the job
``JOB_WITHHELD_EXCEPTIONS`` gives it to, and the gateway opens it only under
that job's own read.

**Shapes** are oe_connector's (its ``watch.py``): a fixed value must match
exactly; a typed slot takes any value of its type (``SLOTS``); a ``fields``
list is the most a call may ask for; an object must have exactly the listed
keys. The runner checks each read's arguments here before the call, and
oe_connector checks them again, as the job's ERP user, when it answers.
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
SLOTS: tuple[str, ...] = ("$company", "$date", "$party", "$parties", "$customers", "$int")
DATE_RANGE_DAYS = 400
MAX_LIST = 50
MAX_INT = 500
# A Company, Customer or Supplier name, and a setting's text (oe_connector
# bounds a supplier's name at 140 too).
MAX_TEXT = 140
# The gateway's name for an oe_connector tool is this plus its own name.
GATEWAY_PREFIX = "erpnext__"
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


# The results of the reads before this one, in order, each without its
# ``acting`` key; a skipped read's is {}.
Prior = tuple[Mapping[str, Any], ...]
# A read's arguments: a dict, a ``Derived`` one, or None to skip the read
# (a derived list came out empty: never sent with an empty list).
ReadArguments = dict[str, Any] | Derived | None


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
    - ``group``: what the notice groups it under (a customer)."""

    key: str
    operation: str
    doctype: str
    fields: Mapping[str, Any]
    expect: Mapping[str, str]
    title: str
    reason_key: str = ""
    group: str = ""


# A job's own builder of a card's fields, by operation and DocType: the
# fields as they will be checked, stored and sent, or why not. Run before
# each check and again at approval, so a tampered card is refused.
CheckFields = Callable[[str, str, Mapping[str, Any]], dict[str, Any] | str]


@dataclass(frozen=True)
class ReasonSchema:
    """A job's reason step: one short line per finding, written by a model
    from the findings' ``facts``. ``instruction`` says what a line explains
    (a constant in the job's code)."""

    instruction: str
    max_findings: int = 20


# (each read's result, in the order of ``reads``, {} for a skipped one; the
# run's settings; its day) -> the findings. Pure code: every figure comes
# from the results.
Build = Callable[[Sequence[Mapping[str, Any]], Settings, date], Sequence[CardSpec | DraftSpec]]


def _setting_ok(value: object, bounds: tuple[int, int] | None = None) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        low, high = bounds or (1, MAX_INT)
        return low <= value <= high
    if isinstance(value, str):
        return _text_ok(value, allow_empty=True)
    if isinstance(value, list):
        return len(value) <= MAX_LIST and all(isinstance(v, str) and _text_ok(v) for v in value)
    return False


@dataclass(frozen=True)
class WatchJob:
    id: str
    title: str
    reads: tuple[WatchRead, ...]
    build: Build
    # Its hour (and minute) each day, in the company's time zone.
    schedule: time
    card_cap_day: int
    default_settings: Mapping[str, Any]
    reason_schema: ReasonSchema | None = None
    # Its draft cards' builder (``CheckFields``); None: a job of notices only.
    check_fields: CheckFields | None = None
    # A whole-number setting's bounds other than 1 to MAX_INT (e.g. an amount).
    setting_bounds: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    # What a notice calls its draft cards ("reminder drafts").
    card_noun: str = "drafts"

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
        if not (1 <= self.card_cap_day <= MAX_CARD_CAP):
            raise ValueError("a job's card cap is 1 to 50 a day")
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
    if slot in ("$parties", "$customers"):
        return (
            isinstance(value, list)
            and 0 < len(value) <= MAX_LIST
            and all(isinstance(v, str) and _text_ok(v) for v in value)
        )
    return False


def shape_mismatch(shape: Any, value: Any, today: date, where: str = "") -> str:
    """Why ``value`` doesn't fit ``shape``, or "" when it does. The reason
    names keys and slots only, never a value."""
    if isinstance(shape, str) and shape in SLOTS:
        return "" if _slot_holds(shape, value, today) else f"{where or 'value'} isn't a {shape[1:]}"
    if isinstance(shape, Mapping):
        if not isinstance(value, Mapping):
            return f"{where or 'arguments'} should be an object"
        extra = sorted(set(value) - set(shape))
        if extra:
            return f"{', '.join(str(k) for k in extra)[:200]} isn't listed"
        missing = sorted(set(shape) - set(value))
        if missing:
            return f"{', '.join(missing)} is missing"
        for key, sub in shape.items():
            why = shape_mismatch(sub, value[key], today, f"{where}.{key}" if where else key)
            if why:
                return why
        return ""
    if isinstance(shape, list | tuple) and where.split(".")[-1] == "fields":
        if not isinstance(value, list) or not value or not all(isinstance(f, str) for f in value):
            return f"{where} should be a list of fields"
        return f"{where} asks for a field that isn't listed" if set(value) - set(shape) else ""
    return "" if value == shape else f"{where or 'value'} isn't the listed value"


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
            ok = (
                isinstance(value, list)
                and len(value) <= MAX_LIST
                and all(isinstance(v, str) and _text_ok(v) for v in value)
            )
        if not ok:
            raise SettingsError(f"Setting {key} isn't of its type or is out of bounds.")
        out[key] = list(value) if isinstance(value, list) else value
    return out


# The jobs this install runs, each with its entry in oe_connector_watch_jobs
# (payment-checks joins in its own PR). Imported here, once everything a job
# module uses is defined.
from openexecutive.watch.jobs import collections as _collections  # noqa: E402

_LISTED: tuple[WatchJob, ...] = (_collections.JOB,)
_JOBS: dict[str, WatchJob] = {job.id: job for job in _LISTED}
JOBS: Mapping[str, WatchJob] = MappingProxyType(_JOBS)
