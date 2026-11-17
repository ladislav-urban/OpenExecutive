"""Cost caps: a limit per turn, per person a day, per job a day and a
monthly one.

Every model call the package makes goes through ``providers.registry.
get_provider()``, which wraps the hosted backends in
``providers.capped.CappedProvider``. Before each call goes out, the wrapper
calls :func:`check`; after it, the call site's ``audit.usage.log_model_usage``
calls :func:`add`. So:

* **Counted against whom.** Each call counts against up to three scopes:
  the turn's speaker (``person``), the unattended job it runs in (``job``,
  bound with :func:`bind_job`) and everything (``total``).
* **The turn.** ``COST_CAP_TURN_USD`` holds one turn (everything a single
  message causes, or one unattended run) to a limit, the principal's too:
  it stops a runaway loop rather than rationing a budget. It is counted in
  memory (:func:`bind_turn`), never in the DB, since a turn never outlives
  the process. The reply checks (``claim_check``, ``grounding``,
  ``answer_check``) are kept off it (:func:`_check_exempt`, theirs alone),
  never off the other caps.
* **Counters survive a restart.** They are rows of ``usage_counters`` in
  the episodic DB, keyed by scope, key and day. A day runs in the company's
  time zone (``workspace_settings.get_user_timezone``); the month is the
  sum of its days.
* **Fails closed.** With a cap on, a counter that can't be read refuses the
  call. A call whose cost the provider doesn't report and the price table
  doesn't know counts at the table's highest prices.
* **Checked against the worst case.** A call is refused once the spend so
  far plus the call's worst case (its prompt at the full input price and its
  ``max_tokens`` at the output price) would pass the cap, so one call can't
  carry a scope far past it.
* **The principal.** Never held to the person cap; the monthly cap holds
  for every call.
* **The principal is told** once per cap and key a day: a card on Home,
  private to them, and a message on their usual channel.

Every cap defaults to 0, which is off. With all three off nothing is read
before a call, but :func:`add` still counts, so turning a cap on mid-day
sees the day's spend so far.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import math
import sqlite3
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

logger = logging.getLogger(__name__)

TABLE = "usage_counters"
SOURCE = "cost_cap"
# The audit event each refusal writes (scope, key, figures; never a name).
EVENT = "cost_cap_reached"

Scope = Literal["person", "job", "total"]
RefusedScope = Literal["turn", "person", "job", "total", "unreadable"]

# What a turn stopped by its own cap says, to someone on People.
TURN_STOP_REPLY = (
    "I stopped here: this answer reached its spend limit. Ask me to continue and "
    "I'll pick up from this point."
)

_T = TypeVar("_T")
_P = ParamSpec("_P")


# --------------------------------------------------------------------------- #
# The turn
# --------------------------------------------------------------------------- #

# One lock for every turn's total: reservations run in worker threads
# (asyncio.to_thread), so the check and the add must be one step.
_turn_lock = threading.Lock()


class TurnSpend:
    """What one turn has spent so far, in memory. Bound by :func:`bind_turn`
    and shared by every task and thread the turn's context reaches (the
    object, not a copy). Closed when the turn ends: a call charged to a
    closed turn counts against no turn."""

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:16]
        self.spent = 0.0
        self.closed = False
        # Set once a cap stopped the turn: its reply already says so, and no
        # route or adapter replaces or repeats it.
        self.stopped_by_cap = False
        # The sentence the stop added, so a later check can keep it last.
        self.stop_sentence: str | None = None

    def take(self, amount: float, cap: float) -> tuple[bool, float]:
        """Add ``amount`` unless that would pass ``cap`` (0 is no cap): one
        check-and-add under the lock. ``(taken, spent before)``."""
        with _turn_lock:
            if self.closed:
                return True, self.spent
            before = self.spent
            if cap > 0 and before + amount > cap:
                return False, before
            self.spent = before + amount
            return True, before

    def adjust(self, delta: float) -> None:
        with _turn_lock:
            if not self.closed:
                self.spent = max(self.spent + delta, 0.0)

    def close(self) -> None:
        with _turn_lock:
            self.closed = True


_turn: ContextVar[TurnSpend | None] = ContextVar("cost_cap_turn", default=None)
# Set only by the reply checks, through _check_exempt.
_exempt: ContextVar[bool] = ContextVar("cost_cap_check_exempt", default=False)


def current_turn() -> TurnSpend | None:
    """The open turn the current call counts against, or None."""
    turn = _turn.get()
    return turn if turn is not None and not turn.closed else None


@contextlib.contextmanager
def bind_turn(*, fresh: bool = False, close: bool = True) -> Iterator[TurnSpend]:
    """Count every model call inside the block against one turn, closed at
    the end. Bind it where the turn's session is bound: the SSE turn, each
    channel's handler, each unattended run. Inside an open turn it yields
    that one (one message is one turn, however many layers bind it) unless
    ``fresh``. Saves and restores rather than ``Token.reset``, so it holds
    across the task hops of a stepped stream (as ``set_session`` does).

    ``close=False`` leaves the turn open for the tasks started inside the
    block (an unattended sweep's work, which is that run's own); nothing
    started after the block sees it."""
    outer = _turn.get()
    if not fresh and outer is not None and not outer.closed:
        yield outer
        return
    turn = TurnSpend()
    _turn.set(turn)
    try:
        yield turn
    finally:
        if close:
            turn.close()
        _turn.set(outer)


def ensure_turn() -> TurnSpend:
    """The open turn, or a new one bound for the rest of this context (for a
    caller that drives ``Executive.stream_chat`` with nothing bound: the CLI,
    a test). Never closed, as ``current_session.set`` is never reset there."""
    turn = current_turn()
    if turn is None:
        turn = TurnSpend()
        _turn.set(turn)
    return turn


def as_turn(handler: Callable[_P, Awaitable[_T]]) -> Callable[_P, Awaitable[_T]]:
    """A channel's message handler as one turn of its own (:func:`bind_turn`,
    fresh), so every model call one inbound message causes counts against
    one turn, the handler's own (a transcription, a triage) included."""

    @functools.wraps(handler)
    async def handle(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        with bind_turn(fresh=True):
            return await handler(*args, **kwargs)

    return handle


async def run_as_turn(work: Awaitable[_T]) -> _T:
    """Await ``work`` as one turn of its own: an unattended run (a watch
    job's, a scheduled action's) counts as one turn under the turn cap."""
    with bind_turn(fresh=True):
        return await work


@contextlib.contextmanager
def unbind_turn() -> Iterator[None]:
    """Count nothing inside the block against the current turn: for a task
    the turn spawns that outlives it (a memory pass, an alert)."""
    outer = _turn.get()
    _turn.set(None)
    try:
        yield
    finally:
        _turn.set(outer)


@contextlib.contextmanager
def _check_exempt() -> Iterator[None]:
    """Private to the reply checks (``orchestrator.claim_check``,
    ``briefing.grounding``, ``orchestrator.answer_check``): their calls are
    neither charged to nor refused by the turn cap, so a cap never switches
    a check off. The person, job and month caps still hold. A test greps
    the package so no other module uses it."""
    token = _exempt.set(True)
    try:
        yield
    finally:
        _exempt.reset(token)


def mark_stopped(sentence: str) -> None:
    """Mark the current turn as stopped by a cap, its reply ending with
    ``sentence``."""
    turn = _turn.get()
    if turn is not None:
        turn.stopped_by_cap = True
        turn.stop_sentence = sentence


def turn_stopped() -> bool:
    """Whether a cap stopped the current turn (its reply already says so)."""
    turn = _turn.get()
    return turn is not None and turn.stopped_by_cap


def stop_sentence() -> str | None:
    """The sentence a cap stop added to the current turn's reply, if any."""
    turn = _turn.get()
    return turn.stop_sentence if turn is not None and turn.stopped_by_cap else None

# The job an unattended run is part of. Bound by every unattended runner;
# an unattended session with none bound counts as UNNAMED_JOB.
_job: ContextVar[str | None] = ContextVar("cost_cap_job", default=None)
UNNAMED_JOB = "unattended"


@contextlib.contextmanager
def bind_job(job_id: str) -> Iterator[None]:
    """Count every model call inside the block against ``job_id``. Tasks
    started inside it inherit the binding."""
    token = _job.set(job_id)
    try:
        yield
    finally:
        _job.reset(token)


# Whom a call is spent for when its session doesn't say: a People id (as a
# string) or an outside bucket (``OUTSIDE_PREFIX``). Bound by paths whose
# session carries no speaker though one is known: the ERPNext panel, a
# supplier's agent, a research run's synthesis, a workflow's steps.
_spender: ContextVar[str | None] = ContextVar("cost_cap_spender", default=None)


@contextlib.contextmanager
def bind_spender(key: int | str | None) -> Iterator[None]:
    """Count every model call inside the block against ``key`` (a People id,
    or an outside bucket) when its session names no one. ``None`` binds
    nothing."""
    if key is None:
        yield
        return
    token = _spender.set(str(key))
    try:
        yield
    finally:
        _spender.reset(token)


def spender_of(session: Any) -> str | None:
    """The person key ``session`` is spent for, to carry into work it starts."""
    return _person_key(session)


def current_job() -> str | None:
    """The job the current call counts against, or None outside one."""
    job = _job.get()
    if job:
        return job
    session = _session()
    if session is not None and getattr(session, "unattended", False) is True:
        return UNNAMED_JOB
    return None


@dataclass(frozen=True)
class Caps:
    person_daily: float
    job_daily: float
    monthly: float
    turn: float = 0.0

    @property
    def stored_on(self) -> bool:
        """Whether a cap counted in the DB is on (all but the turn's)."""
        return self.person_daily > 0 or self.job_daily > 0 or self.monthly > 0

    @property
    def any_on(self) -> bool:
        return self.stored_on or self.turn > 0


def caps() -> Caps:
    """The caps in force. A setting that can't be read is no cap at all
    only when it can't be read at all; a malformed value is caught by the
    settings model at startup."""
    from openexecutive.config import get_settings

    s = get_settings()
    return Caps(
        person_daily=float(getattr(s, "cost_cap_person_daily_usd", 0.0) or 0.0),
        job_daily=float(getattr(s, "cost_cap_job_daily_usd", 0.0) or 0.0),
        monthly=float(getattr(s, "cost_cap_monthly_usd", 0.0) or 0.0),
        turn=float(getattr(s, "cost_cap_turn_usd", 0.0) or 0.0),
    )


class CostCapReached(Exception):
    """A model call not sent because it would pass a cost cap."""

    def __init__(
        self, scope: RefusedScope, key: str, cap: float, spent: float,
        resets_at: datetime, name: str = "", worst_case: float = 0.0,
    ) -> None:
        # Only the scope: this text reaches logs and other audit rows (a
        # failed action's error). The key and the figures are kept on the
        # object and in the principal's own cost_cap_reached row.
        super().__init__(f"cost cap reached: {scope}")
        self.scope = scope
        self.key = key
        self.cap = cap
        self.spent = spent
        self.resets_at = resets_at
        self.name = name
        self.worst_case = worst_case
        # Whether the asker is off People, read where the call was refused
        # (the session is bound there), for reply() wherever it is asked.
        self.outside = (scope == "person" and key.startswith(OUTSIDE_PREFIX)) or (
            scope != "person" and _asker_is_outside()
        )

    def reply(self) -> str:
        """What the person whose call it was is told. Anyone off People (a
        stranger's mail, a chat space, a supplier's agent, an AI app) hears
        only that it can't answer: the company's budget is not theirs to know."""
        if self.outside or (self.scope != "person" and _asker_is_outside()):
            from openexecutive.providers.refusal import reply_text

            return reply_text()
        if self.scope == "turn":
            return TURN_STOP_REPLY
        if self.scope == "person":
            return "You've reached today's usage limit. It resets at midnight."
        if self.scope == "job":
            return "This job has reached today's usage limit. It runs again tomorrow."
        if self.scope == "total":
            return "This month's usage limit is reached. The principal can raise it."
        return "Usage can't be checked right now, so nothing was sent. The team has been told."


def capped(exc: BaseException) -> CostCapReached | None:
    """``exc`` or what it wraps, when it is a cap refusal."""
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo:
        cur = todo.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, CostCapReached):
            return cur
        if isinstance(cur, BaseExceptionGroup):
            todo.extend(cur.exceptions)
        todo.extend(nxt for nxt in (cur.__cause__, cur.__context__) if nxt is not None)
    return None


# --------------------------------------------------------------------------- #
# Who a call counts against
# --------------------------------------------------------------------------- #

def _session() -> Any:
    try:
        from openexecutive.orchestrator.schedule_tools import current_session

        return current_session.get()
    except Exception:  # noqa: BLE001
        return None


# The person key of a call no one on People made: one bucket per channel
# (a stranger's mail, a chat space, a supplier's agent, an AI app), held to
# the person cap like anyone else, so outsiders can't spend the month alone.
OUTSIDE_PREFIX = "outside:"


def _person_id(session: Any) -> int | None:
    """The People id the current call is spent for: a teammate's queued run,
    else the signed-in or verified sender. An email's sender counts only when
    Gmail authenticated it: a From line alone proves nothing. None for a turn
    with no one on People behind it."""
    if session is None:
        return None
    email = str(getattr(session, "origin_channel", "") or "") == "email"
    authenticated = (
        getattr(session, "email_authenticated", False) is True
        or getattr(session, "email_sender_authenticated", False) is True
    )
    for attr in ("run_for_person_id", "caller_person_id", "gate_sender_person_id"):
        value = getattr(session, attr, None)
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        if email and attr != "run_for_person_id" and not authenticated:
            return None
        return value
    bound = _spender.get()
    if bound is not None and bound.isdigit():
        return int(bound)
    return None


def _outside_channel(session: Any) -> str | None:
    """The channel of a turn someone off People drove, else None."""
    if session is None or getattr(session, "unattended", False) is True:
        return None
    if getattr(session, "from_mcp_client", False) is True:
        return "mcp"
    channel = str(getattr(session, "origin_channel", "") or "")
    if getattr(session, "sender_off_team", False) is True:
        return channel or "email"
    return channel or None


def _person_key(session: Any) -> str | None:
    """The person scope's key: a People id, an outside channel's bucket, or
    None (the system's own work, or a web caller resolved to no one)."""
    person = _person_id(session)
    if person is not None:
        return str(person)
    channel = _outside_channel(session)
    if channel:
        return f"{OUTSIDE_PREFIX}{channel}"
    return _spender.get()


def _asker_is_outside() -> bool:
    key = _person_key(_session())
    return key is not None and key.startswith(OUTSIDE_PREFIX)


def _is_principal(person_id: int) -> bool:
    from openexecutive.people.store import get_person

    person = get_person(person_id)
    return person is not None and person.is_principal


# --------------------------------------------------------------------------- #
# Days and the counter table
# --------------------------------------------------------------------------- #

def _zone() -> tzinfo:
    from openexecutive.memory.workspace_settings import get_user_timezone

    return get_user_timezone()


def _local(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(_zone())


def day_key(now: datetime | None = None) -> str:
    """Today's date in the company's time zone."""
    return _local(now).date().isoformat()


def _next_day(now: datetime | None) -> datetime:
    local = _local(now)
    start = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=local.tzinfo)
    return start.astimezone(UTC)


def _next_month(now: datetime | None) -> datetime:
    local = _local(now)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return datetime(year, month, 1, tzinfo=local.tzinfo).astimezone(UTC)


def _db() -> Path:
    from openexecutive.memory.episodic import _resolve_db_path

    return _resolve_db_path(None)


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(str(_db()), timeout=10)


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    day TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, key, day)
)
"""


def _spent(conn: sqlite3.Connection, scope: str, key: str, day: str, *, month: bool = False) -> float:
    try:
        if month:
            row = conn.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) FROM {TABLE} WHERE scope = ? AND key = ? AND day LIKE ?",
                (scope, key, day[:7] + "-%"),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT cost_usd FROM {TABLE} WHERE scope = ? AND key = ? AND day = ?",
                (scope, key, day),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0.0  # nothing counted yet
        raise
    value = float(row[0]) if row and row[0] is not None else 0.0
    if not math.isfinite(value):
        raise ValueError(f"{TABLE} holds a non-finite spend for {scope} {key}")
    return value


def spend_today(scope: Scope, key: str, now: datetime | None = None) -> float:
    """What ``scope``/``key`` has spent today (or this month, for ``total``)."""
    with contextlib.closing(_connect()) as conn:
        return _spent(conn, scope, key, day_key(now), month=scope == "total")


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #

def _is_local(model: str) -> bool:
    try:
        from openexecutive.config import get_settings
        from openexecutive.providers.registry import _local_models

        return model in _local_models(get_settings())
    except Exception:  # noqa: BLE001
        return False


def _top_price() -> Any:
    """The table's highest price per token class, for a model it doesn't
    know: an unknown cost counts high, never as nothing."""
    from openexecutive.audit.pricing import ModelPrice, price_table

    prices = [entry.price for entry in price_table().models.values()]
    prices += [entry.long for entry in price_table().models.values() if entry.long is not None]
    if not prices:
        return None
    return ModelPrice(
        input=max(p.input for p in prices),
        cache_write_5m=max(p.cache_write_5m for p in prices),
        cache_write_1h=max(p.cache_write_1h for p in prices),
        cache_read=max(p.cache_read for p in prices),
        output=max(p.output for p in prices),
    )


def charge(model: str, counts: dict[str, Any]) -> float:
    """What one finished call counts against its caps, in USD."""
    if _is_local(model):
        return 0.0
    cost = counts.get("cost_usd")
    if cost is not None:
        try:
            value = float(cost)
        except (TypeError, ValueError, OverflowError):
            value = math.nan
        if math.isfinite(value) and value >= 0:
            return value
    top = _top_price()
    if top is None:
        return 0.0

    def _n(field: str) -> int:
        try:
            return max(int(counts.get(field) or 0), 0)
        except (TypeError, ValueError, OverflowError):
            return 0

    return (
        _n("input_tokens") * top.input
        + _n("cache_creation_input_tokens") * top.cache_write_1h
        + _n("cache_read_input_tokens") * top.cache_read
        + _n("output_tokens") * top.output
    ) / 1_000_000


def _scopes(session: Any, job: str | None) -> list[tuple[Scope, str]]:
    keys: list[tuple[Scope, str]] = [("total", "all")]
    person = _person_key(session)
    if person is not None:
        keys.append(("person", person))
    if job:
        keys.append(("job", job))
    return keys


def attribution() -> dict[str, Any]:
    """Who and what the current call is spent for, for its audit row."""
    out: dict[str, Any] = {}
    session = _session()
    person = _person_id(session)
    if person is not None:
        out["person_id"] = person
    else:
        key = _person_key(session)
        if key is not None:
            out["person_key"] = key
    job = current_job()
    if job:
        out["job_id"] = job
    # The turn it counted against (null outside one, or for a reply check,
    # which no turn is charged for).
    turn = current_turn()
    out["turn_id"] = turn.id if turn is not None and not _exempt.get() else None
    return out


_UPSERT = (
    f"INSERT INTO {TABLE} (scope, key, day, cost_usd, calls) VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT (scope, key, day) DO UPDATE SET "
    "cost_usd = MAX(cost_usd + excluded.cost_usd, 0), calls = calls + excluded.calls"
)


@dataclass(frozen=True)
class Reservation:
    """What a call put on its scopes before it was sent (its worst case),
    for :func:`settle` to put right once its cost is known."""

    scopes: tuple[tuple[str, str], ...]
    day: str
    amount: float
    # What it put on its turn (0 when no turn or no turn cap applies).
    turn: TurnSpend | None = None
    turn_amount: float = 0.0


def _write(scopes: Any, day: str, amount: float, *, calls: int) -> None:
    """Add ``amount`` and ``calls`` to each scope, in one write transaction
    taken at once (BEGIN IMMEDIATE), tried twice under contention."""
    for attempt in (1, 2):
        try:
            with contextlib.closing(_connect()) as conn:
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(_SCHEMA)
                    for scope, key in scopes:
                        conn.execute(_UPSERT, (scope, key, day, amount, calls))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            return
        except sqlite3.OperationalError:
            if attempt == 2:
                raise


def release(reservation: Reservation) -> None:
    """Take back a call's reservation: it was never billed (refused or
    failed before an answer). Never raises."""
    if reservation.turn is not None and reservation.turn_amount:
        reservation.turn.adjust(-reservation.turn_amount)
    if not reservation.amount:
        return
    try:
        _write(reservation.scopes, reservation.day, -reservation.amount, calls=-1)
    except Exception:  # noqa: BLE001
        logger.exception("cost caps: couldn't release a reservation; its worst case stays counted")


def add(model: str, counts: dict[str, Any] | None, *, now: datetime | None = None) -> None:
    """Count one finished call no :class:`Reservation` covers (a voice note).
    Never raises."""
    try:
        cost = charge(model, counts or {})
        turn = current_turn()
        if turn is not None and not _exempt.get():
            turn.adjust(cost)
        _write(_scopes(_session(), current_job()), day_key(now), cost, calls=1)
    except Exception:  # noqa: BLE001 - counting must never fail the call it follows
        logger.exception("cost caps: couldn't count a %s call", model)


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #

def worst_case(model: str, kwargs: dict[str, Any]) -> float:
    """The most one call with these arguments could cost: every prompt
    token at the dearer of the input and the 1-hour cache-write price, and
    every ``max_tokens`` token at the output price. A prompt is taken as one
    token per two characters of its JSON, which covers text in any script
    but the rare one where a character is more than one token; settling
    puts it right once the call answers."""
    if _is_local(model):
        return 0.0
    from openexecutive.audit.pricing import price_for

    try:
        prompt = json.dumps(
            [kwargs.get("system"), kwargs.get("messages"), kwargs.get("tools")],
            default=str, ensure_ascii=False,
        )
    except Exception:  # noqa: BLE001
        prompt = str(kwargs.get("messages"))
    prompt_tokens = len(prompt) // 2 + 1
    price = price_for(model, prompt_tokens=prompt_tokens) or _top_price()
    if price is None:
        return 0.0
    try:
        max_tokens = max(int(kwargs.get("max_tokens") or 0), 0)
    except (TypeError, ValueError):
        max_tokens = 0
    prompt_price = max(price.input, price.cache_write_1h)
    return (prompt_tokens * prompt_price + max_tokens * price.output) / 1_000_000


def reserve(
    model: str, kwargs: dict[str, Any], *, now: datetime | None = None, tell: bool = True,
) -> Reservation:
    """Before a call is sent: raise :class:`CostCapReached` when its worst
    case would pass a cap that is on, else put that worst case on its scopes
    in the same write transaction as the check (BEGIN IMMEDIATE), so calls
    made together can't all pass on the same spend. :func:`settle` turns it
    into the real cost; a call that never settles (a stream the person
    closed) stays counted at its worst case. With every cap off nothing is
    read and nothing reserved.

    The turn's cap (in memory) is checked and its worst case added first,
    under one lock; a refusal by any other cap gives it back. A reply check
    (:func:`_check_exempt`) is neither checked against nor charged to it."""
    session = _session()
    job = current_job()
    day = day_key(now)
    scopes = tuple(_scopes(session, job))
    limits = caps()
    if not limits.any_on or _is_local(model):
        return Reservation(scopes, day, 0.0)
    estimate = worst_case(model, kwargs)
    turn = current_turn() if limits.turn > 0 and not _exempt.get() else None
    turn_amount = 0.0
    if turn is not None:
        taken, before = turn.take(estimate, limits.turn)
        if not taken:
            refused = _refusal("turn", turn.id, limits.turn, before, datetime.now(UTC), worst_case=estimate)
            logger.warning("cost caps: refused a call - %s", refused)
            _record(refused)
            raise refused
        turn_amount = estimate
    if not limits.stored_on:
        return Reservation(scopes, day, 0.0, turn, turn_amount)
    person = _person_id(session)
    person_key = _person_key(session)
    try:
        exempt = person is not None and _is_principal(person)
        with contextlib.closing(_connect()) as conn:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(_SCHEMA)
                if limits.monthly > 0:
                    spent = _spent(conn, "total", "all", day, month=True)
                    if spent + estimate > limits.monthly:
                        raise _refusal("total", "all", limits.monthly, spent, _next_month(now), worst_case=estimate)
                if limits.job_daily > 0 and job:
                    spent = _spent(conn, "job", job, day)
                    if spent + estimate > limits.job_daily:
                        raise _refusal("job", job, limits.job_daily, spent, _next_day(now), worst_case=estimate)
                if limits.person_daily > 0 and person_key is not None and not exempt:
                    spent = _spent(conn, "person", person_key, day)
                    if spent + estimate > limits.person_daily:
                        raise _refusal(
                            "person", person_key, limits.person_daily, spent, _next_day(now), worst_case=estimate,
                        )
                for scope, key in scopes:
                    conn.execute(_UPSERT, (scope, key, day, estimate, 1))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    except CostCapReached as refused:
        if turn is not None:
            turn.adjust(-turn_amount)
        logger.warning("cost caps: refused a call - %s", refused)
        _record(refused)
        if tell:
            _tell_principal(refused, now)
        raise
    except BaseException as exc:
        if turn is not None:
            turn.adjust(-turn_amount)
        if not isinstance(exc, Exception):
            raise
        logger.exception("cost caps: couldn't read the counters - refusing the call")
        sick = _refusal("unreadable", type(exc).__name__, 0.0, 0.0, _next_day(now), worst_case=estimate)
        _record(sick)
        if tell:
            _tell_principal(sick, now)
        raise sick from exc
    return Reservation(scopes, day, estimate, turn, turn_amount)


def settle(reservation: Reservation, model: str, message: Any) -> None:
    """Once a call answered: replace its reserved worst case with what it
    cost (``message``'s usage, priced as its audit row is). A call that
    reserved nothing (every cap off) is counted here in full. A message
    without usage keeps the worst case. Never raises."""
    try:
        from openexecutive.audit.usage import _priced, usage_counts

        counts = usage_counts(message) if message is not None else None
        if counts is None:
            if reservation.amount == 0.0:
                _write(reservation.scopes, reservation.day, 0.0, calls=1)
            return
        if counts.get("cost_usd") is None:
            _priced(counts, model)
        cost = charge(model, counts)
        if reservation.turn is not None:
            reservation.turn.adjust(cost - reservation.turn_amount)
        calls = 1 if reservation.amount == 0.0 else 0
        _write(reservation.scopes, reservation.day, cost - reservation.amount, calls=calls)
    except Exception:  # noqa: BLE001
        logger.exception("cost caps: couldn't settle a %s call; its worst case stays counted", model)


def check(model: str, kwargs: dict[str, Any], *, now: datetime | None = None) -> None:
    """Raise :class:`CostCapReached` when a call with ``kwargs`` could pass a
    cap that is on, reserving nothing (a voice note, whose cost :func:`add`
    counts after)."""
    release(reserve(model, kwargs, now=now))


def _refusal(
    scope: RefusedScope, key: str, cap: float, spent: float, resets_at: datetime, *, worst_case: float = 0.0,
) -> CostCapReached:
    return CostCapReached(scope, key, cap, spent, resets_at, name=_name_of(scope, key), worst_case=worst_case)


def _record(exc: CostCapReached) -> None:
    """The refusal's audit row (``cost_cap_reached``, source ``cost_cap``):
    the scope and its key (a People id, an outside bucket, a job id or a
    turn id; never an email or a name), the figures, the reset and the
    session. The principal's alone. Never raises."""
    try:
        from openexecutive.audit import log_event

        session = _session()
        session_id = getattr(session, "session_id", None)
        log_event(
            EVENT,
            f"Cost cap reached: {exc.scope} at {exc.spent:.4f} of {exc.cap:.4f} USD",
            session_id=session_id if isinstance(session_id, str) else None,
            actor=SOURCE,
            details={
                "source": SOURCE,
                "scope": exc.scope,
                "key": exc.key,
                "cap_usd": round(exc.cap, 6),
                "spent_usd": round(exc.spent, 6),
                "worst_case_usd": round(exc.worst_case, 6),
                "resets_at": exc.resets_at.astimezone(UTC).isoformat(),
                "session_id": session_id if isinstance(session_id, str) else None,
            },
            principal_only=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning("cost caps: couldn't record a refusal", exc_info=True)


def _name_of(scope: str, key: str) -> str:
    if scope != "person":
        return key
    if key.startswith(OUTSIDE_PREFIX):
        return f"Senders not on People ({key[len(OUTSIDE_PREFIX):]})"
    try:
        from openexecutive.people.store import get_person

        person = get_person(int(key))
    except Exception:  # noqa: BLE001
        person = None
    return person.full_name if person is not None and person.full_name else f"Person {key}"


# --------------------------------------------------------------------------- #
# Telling the principal, once per cap and key a day
# --------------------------------------------------------------------------- #

_alert_tasks: set[asyncio.Task[None]] = set()


def _headline(exc: CostCapReached) -> str:
    if exc.scope == "person":
        return f"{exc.name} reached today's usage limit"
    if exc.scope == "job":
        return f"The job {exc.name} reached today's usage limit"
    if exc.scope == "total":
        return "This month's usage limit is reached"
    return "Usage limits can't be checked, so model calls are refused"


def _body(exc: CostCapReached) -> str:
    if exc.scope == "unreadable":
        return (
            "The usage counters couldn't be read, so every call a limit applies to "
            "is refused until they can. Check the server's log for the cause."
        )
    resets = exc.resets_at.astimezone(_zone()).strftime("%H:%M on %d %b %Y")
    what = {
        "person": f"{exc.name}'s calls are refused",
        "job": "The job stops, and its calls are refused",
        "total": "Every call is refused",
    }[exc.scope]
    return (
        f"Spent {exc.spent:.2f} of a {exc.cap:.2f} USD limit. {what} until "
        f"{resets}, when the limit resets."
    )


def _file_card(exc: CostCapReached, now: datetime | None) -> int | None:
    from openexecutive.alerts import store
    from openexecutive.alerts.models import PRIVATE_ALERT_TAG
    from openexecutive.people.store import find_principal_person

    day = day_key(now)
    principal = find_principal_person()
    return store.insert_alert(
        source=SOURCE,
        external_id=f"{exc.scope}:{exc.key}:{day}",
        severity="high",
        headline=_headline(exc),
        body=_body(exc),
        suggested_action="Raise the limit, or wait for it to reset.",
        topic_tags=["operations", PRIVATE_ALERT_TAG],
        dedup_key=f"private:{SOURCE}:{exc.scope}:{exc.key}:{day}",
        routed_to_person_id=principal.id if principal is not None else None,
    )


async def _alert_principal(exc: CostCapReached, now: datetime | None) -> None:
    """File the card and send it on the principal's usual channel. Never
    raises. A card already filed today for this cap and key sends nothing."""
    try:
        card_id = await asyncio.to_thread(_file_card, exc, now)
    except Exception:
        logger.exception("cost caps: couldn't file the card")
        return
    if card_id is None:
        return
    try:
        from openexecutive.orchestrator.schedule_tools import set_session
        from openexecutive.people.store import find_principal_person
        from openexecutive.scheduler.runner import deliver_to_person

        principal = await asyncio.to_thread(find_principal_person)
        if principal is None:
            return
        with set_session(None):
            # A fixed template: no name and no figures (those are on the
            # card), sent the way the briefs reach them, through the send
            # tools and their outbound guard (orchestrator.outbound_guard).
            sent = await deliver_to_person(principal, channel_text(exc), label="Usage limit")
        if not sent.ok:
            logger.warning("cost caps: card %s filed, not delivered: %s", card_id, sent.detail)
    except Exception:
        logger.exception("cost caps: couldn't deliver card %s", card_id)


_told: set[tuple[str, str, str]] = set()


def tell_principal(exc: CostCapReached) -> None:
    """Tell the principal of a refusal raised where it couldn't be told (a
    worker thread). Call it on the event loop."""
    _tell_principal(exc, None)


def channel_text(exc: CostCapReached) -> str:
    """The principal's message on their usual channel: one fixed sentence
    per scope, naming no one; the card on Home has the rest."""
    what = {
        "person": "Someone on People reached today's usage limit",
        "job": "An unattended job reached today's usage limit",
        "total": "This month's usage limit is reached",
    }.get(str(exc.scope), "Usage limits can't be checked, so model calls are refused")
    return f"{what}. The details are on your Home page."


def _tell_principal(exc: CostCapReached, now: datetime | None) -> None:
    # A turn's own cap makes no card: the asker sees the stop sentence, and
    # the audit row records it.
    if exc.scope == "turn":
        return
    told = (str(exc.scope), exc.key, day_key(now) if exc.scope != "unreadable" else _local(now).isoformat()[:13])
    if told in _told:
        return
    _told.add(told)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            asyncio.run(_alert_principal(exc, now))
        except Exception:
            logger.exception("cost caps: telling the principal failed")
        return
    # Without the caller's session or job: the alert is no turn's work.
    task = loop.create_task(_alert_principal(exc, now), context=_bare_context())
    _alert_tasks.add(task)
    task.add_done_callback(_alert_tasks.discard)


def _bare_context() -> Any:
    import contextvars

    ctx = contextvars.copy_context()
    ctx.run(_job.set, None)
    ctx.run(_spender.set, None)
    ctx.run(_turn.set, None)
    ctx.run(_exempt.set, False)
    return ctx
