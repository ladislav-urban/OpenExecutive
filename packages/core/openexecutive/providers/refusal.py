"""When the model provider refuses to answer for billing or a spend limit.

The pilot ran out of Anthropic credit (Nov 3, 2026). From then on every
call came back ``400 invalid_request_error: Your credit balance is too low
to access the Anthropic API``. Every panel and chat turn said "Something went
wrong; please try again", and nobody was told. A refusal like that won't pass
on a retry, so this module:

* tells one apart from any other failure (:func:`classify`), by the SDK's
  typed fields: a ``billing_error`` type or HTTP 402 is billing. The credit
  and spend-limit refusals come as a plain 400 ``invalid_request_error``, so
  for those, and only those, the error's own message decides;
* gives every route the same reply for it (:func:`refusal_reply`), naming the
  product as the install does (``utils.product_name``);
* tells the principal, at most once an hour (:func:`note_refusal`): a card on
  their Home, private to them, and a message on their usual channel, the way
  the briefs reach them;
* stops calling the provider for a short while after a refusal
  (:func:`fail_fast`), so one turn's specialists, reviewers and tool calls
  don't each spend a request on the same refusal. The first call that answers
  clears it (:func:`note_success`).

State is per process. Across a restart, the hour is read back from the last
card filed.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

logger = logging.getLogger(__name__)

Cause = Literal["billing", "spend_limit"]

SOURCE = "provider_refused"
ALERT_EVERY = timedelta(hours=1)
# How long after a refusal calls fail without being sent. Short, so a top-up
# is noticed within the minute without a restart.
FAIL_FAST_S = 60.0

# The two refusals that arrive as a plain 400 invalid_request_error. Matched
# on the provider's own message, lower-cased, because the type says nothing.
_BILLING_PHRASES = ("credit balance is too low",)
_SPEND_LIMIT_PHRASES = ("api usage limits", "usage limit", "spend limit")


class ProviderRefused(Exception):
    """A call not sent because the provider just refused one for ``cause``."""

    def __init__(self, cause: Cause, provider: str = "Anthropic") -> None:
        super().__init__(f"{provider} refused for {cause}")
        self.cause: Cause = cause
        self.provider = provider


@dataclass(frozen=True)
class Refusal:
    cause: Cause
    provider: str
    at: datetime


_lock = threading.Lock()
_last: Refusal | None = None
_blocked_until = 0.0
_last_alert_at: datetime | None = None
_alert_tasks: set[asyncio.Task[None]] = set()


def _message_of(exc: BaseException) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"].lower()
    return str(getattr(exc, "message", "") or "").lower()


def _classify_one(exc: BaseException) -> tuple[Cause, str] | None:
    if isinstance(exc, ProviderRefused):
        return exc.cause, exc.provider
    import anthropic

    if isinstance(exc, anthropic.APIStatusError):
        if getattr(exc, "type", None) == "billing_error" or exc.status_code == 402:
            return "billing", "Anthropic"
        if exc.status_code == 400 and getattr(exc, "type", None) in (None, "invalid_request_error"):
            text = _message_of(exc)
            if any(p in text for p in _BILLING_PHRASES):
                return "billing", "Anthropic"
            if any(p in text for p in _SPEND_LIMIT_PHRASES):
                return "spend_limit", "Anthropic"
        return None
    import httpx

    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 402:
        # OpenRouter's "out of credits" (openai_compatible raises it as is).
        return "billing", "OpenRouter"
    return None


def classify(exc: BaseException) -> tuple[Cause, str] | None:
    """``(cause, provider)`` when ``exc``, or what it wraps, is a billing or
    spend-limit refusal; else None. Follows ``__cause__``/``__context__``
    and exception groups, since a route sees what the turn re-raised."""
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo:
        cur = todo.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        hit = _classify_one(cur)
        if hit is not None:
            return hit
        if isinstance(cur, BaseExceptionGroup):
            todo.extend(cur.exceptions)
        for nxt in (cur.__cause__, cur.__context__):
            if nxt is not None:
                todo.append(nxt)
    return None


def reply_text() -> str:
    from openexecutive.utils.product_name import product_name

    return f"{product_name()} can't answer right now; the team has been told."


def _held_back(exc: BaseException) -> bool:
    """Whether ``exc`` is, or wraps, a call :func:`fail_fast` never sent."""
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo:
        cur = todo.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, ProviderRefused):
            return True
        if isinstance(cur, BaseExceptionGroup):
            todo.extend(cur.exceptions)
        todo.extend(nxt for nxt in (cur.__cause__, cur.__context__) if nxt is not None)
    return False


def refusal_reply(exc: BaseException) -> str | None:
    """The reply a route gives for ``exc`` when it is a refusal, else None.

    A refusal the provider itself sent is noted, so the principal hears of it
    whatever made the call (OpenRouter's has no provider hook). A call that
    :func:`fail_fast` held back is not noted again: that would push the
    window out on every new question, and after a top-up nothing would ever
    be sent to find out the account answers again."""
    hit = classify(exc)
    if hit is None:
        return None
    if not _held_back(exc):
        note_refusal(hit[0], provider=hit[1])
    return reply_text()


def last_refusal() -> Refusal | None:
    """The newest refusal since this process last got an answer, if any."""
    return _last


def fail_fast() -> None:
    """Raise :class:`ProviderRefused` while the last refusal is fresh."""
    last = _last
    if last is not None and time.monotonic() < _blocked_until:
        raise ProviderRefused(last.cause, last.provider)


def note_success() -> None:
    """A call answered: the provider is taking requests again."""
    global _last, _blocked_until
    if _last is None:
        return
    with _lock:
        if _last is not None:
            logger.info("provider answered again after a %s refusal", _last.cause)
        _last = None
        _blocked_until = 0.0


def note_refusal(cause: Cause, *, provider: str = "Anthropic", now: datetime | None = None) -> None:
    """Record a refusal and, at most once an hour, tell the principal. Never
    raises; the alert goes out in the background when a loop is running."""
    global _last, _blocked_until, _last_alert_at
    at = now or datetime.now(UTC)
    with _lock:
        fresh = _last is None or time.monotonic() >= _blocked_until
        _last = Refusal(cause, provider, at)
        _blocked_until = time.monotonic() + FAIL_FAST_S
        due = _last_alert_at is None or at - _last_alert_at >= ALERT_EVERY
        if due:
            # Taken before the send is known to have gone: a send the
            # outbound guard holds (quiet hours, the per-recipient cap) or a
            # chat outage leaves only the card for the hour, never a burst.
            _last_alert_at = at
    if fresh:
        logger.error("%s refused a request for %s; not retrying", provider, cause)
    if not due:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            asyncio.run(alert_principal(cause, provider, at))
        except Exception:
            logger.exception("provider refusal: telling the principal failed")
        return
    task = loop.create_task(alert_principal(cause, provider, at))
    _alert_tasks.add(task)
    task.add_done_callback(_alert_tasks.discard)


def _headline(cause: Cause, provider: str) -> str:
    if cause == "billing":
        return f"{provider} stopped answering: the account is out of credit"
    return f"{provider} stopped answering: the spend limit is reached"


def _body(cause: Cause, provider: str, at: datetime) -> str:
    when = at.astimezone(UTC).strftime("%H:%M UTC on %d %b %Y")
    why = (
        "the account's credit balance is too low"
        if cause == "billing"
        else "the account's spend limit is reached"
    )
    fix = (
        "Add credit to the account"
        if cause == "billing"
        else "Raise the spend limit, or wait for it to reset"
    )
    console = " at console.anthropic.com" if provider == "Anthropic" else ""
    return (
        f"{provider} refused a request at {when} because {why}. Until that "
        f"changes, every chat, panel and channel question gets “{reply_text()}”, "
        f"and scheduled work that needs the model doesn't run. {fix}{console}; "
        "the next question then answers as usual."
    )


def _alerted_within(at: datetime) -> bool:
    """Whether a card went out in the hour before ``at`` (survives a restart)."""
    from openexecutive.alerts import store

    with store._get_conn() as conn:
        row = conn.execute(
            # last_seen_at: a repeat folded into the card (coalesce_alert)
            # bumps it, never created_at.
            "SELECT MAX(COALESCE(last_seen_at, created_at)) FROM alerts WHERE source = ?",
            (SOURCE,),
        ).fetchone()
    if not row or not row[0]:
        return False
    try:
        last = datetime.fromisoformat(row[0])
    except ValueError:
        return False
    return at - last < ALERT_EVERY


def _file_card(cause: Cause, provider: str, at: datetime) -> int | None:
    from openexecutive.alerts import store
    from openexecutive.alerts.models import PRIVATE_ALERT_TAG
    from openexecutive.people.store import find_principal_person

    if _alerted_within(at):
        return None
    principal = find_principal_person()
    body = _body(cause, provider, at)
    # The `private:` prefix keeps it apart from shared cards, as the
    # pipeline's private alerts are (alerts.pipeline._make_private).
    dedup_key = f"private:{SOURCE}:{cause}"
    hit = store.coalesce_alert(source=SOURCE, dedup_key=dedup_key, severity="urgent", body=body)
    if hit is not None:
        return hit[0]
    return store.insert_alert(
        source=SOURCE,
        external_id=f"{cause}:{at.isoformat()}",
        severity="urgent",
        headline=_headline(cause, provider),
        body=body,
        suggested_action="Add credit or raise the spend limit."
        if cause == "billing"
        else "Raise the spend limit.",
        topic_tags=["operations", PRIVATE_ALERT_TAG],
        dedup_key=dedup_key,
        routed_to_person_id=principal.id if principal is not None else None,
    )


async def alert_principal(cause: Cause, provider: str, at: datetime) -> None:
    """File the principal's card and send it on their usual channel. Never
    raises.

    Not pushed on the live alert bus (``dispatcher.dispatch_web``): that bus
    goes to every open browser, so a private card never rides it
    (``alerts.pipeline._make_private``). The principal's Home shows it on its
    next load; /today and /alerts keep it from everyone else."""
    try:
        card_id = await asyncio.to_thread(_file_card, cause, provider, at)
    except Exception:
        logger.exception("provider refusal: could not file the card")
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
        text = f"{_headline(cause, provider)}.\n\n{_body(cause, provider, at)}"
        # No session: nothing sent here is a turn's outbound context.
        with set_session(None):
            sent = await deliver_to_person(principal, text, label="Action needed")
        if not sent.ok:
            logger.warning("provider refusal: card %s filed, not delivered: %s", card_id, sent.detail)
    except Exception:
        logger.exception("provider refusal: could not deliver card %s", card_id)


def reset_for_tests() -> None:
    global _last, _blocked_until, _last_alert_at
    with _lock:
        _last = None
        _blocked_until = 0.0
        _last_alert_at = None
