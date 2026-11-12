"""Who is asking ERPNext: the signed caller on every ``erpnext__*`` call.

ERPNext phase 2 (design: oe_connector's ``docs/erpnext-integration.md``,
"Identity and permissions"). The gateway reaches oe_connector as one service
user, so on its own every call would see what that user sees. With
``ERPNEXT_CALLER_PRIVATE_KEY`` set, the gateway adds a signed assertion of
who is asking to each call's arguments, after every gate has passed and long
after the model built the call; oe_connector checks it and runs the call as
that person's ERPNext user, with their roles and user permissions.

**Who is asking** (``caller_for``), from the turn's session only, never from
anything the model or the message says:

- the principal speaking on a surface that proved it
  (``content_trust.principal_speaking``), named by their People email, as
  in phase 1: what they see is theirs to share;
- a rostered teammate on a surface that verified them
  (``people_tools.teammate_on_verified_surface``), only in a conversation
  nobody else reads (``memory.history_chat.private_chat``: the web chat, a
  Slack or Discord DM, a Telegram chat), so their ERPNext view never lands
  in a shared thread; in the web chat only under signed callers
  (``api.caller.signing_on``), since without them the signed-in email is
  whatever the holder of the shared secret sends;
- on a turn a host origin's page sent (``Session.from_host``, the desk's
  own Chats), the person signed in to this app, under signed callers only,
  in a conversation nobody else reads: from the session's signed caller,
  never from anything the page or the model says (``_host_caller``; the
  owner's choice, Oct 29, 2026). Only ERPNext's reads reach this
  (``schedule_tools.host_allows_erpnext_read``);
- the person in ERPNext's own "Ask the Dynocortex" panel
  (``Session.erpnext_panel_email``, set only by integrations/erpnext_panel.py
  from an assertion oe_connector signed): asked as that same ERPNext user,
  whose word ERPNext already is;
- the morning brief's own ERP NUMBERS runs (``brief_run``, set only by
  ``briefing.erp_numbers`` around the gateway calls it makes itself, never
  by the model): ``service``, which oe_connector holds to the reports its
  operator lists. Their audience is fixed, the principal;
- a supplier's own agent over A2A (``supplier_run``, set only by
  integrations/erpnext_a2a.py around its narrow agent, for the supplier whose
  registered key signed the request): ``supplier``, ``sub`` the Supplier's
  name in ERPNext, never anything the model or the message says. oe_connector
  holds it to that supplier's own documents (its mcp/suppliers.py);
- anyone else is refused here, before ERPNext is asked. That includes every
  other run nobody is watching (``Session.unattended``): a scheduled
  follow-up's text and recipient come from whoever scheduled it, so signing
  it as the service user would hand them the service user's view.

**The assertion** is ``v1.<payload>.<signature>``, the format of
``api.caller_signing``: base64url JSON claims ``{args, aud, exp, iat, jti,
kid, kind, sub, tool}`` signed with Ed25519 over ``v1.<payload>``. It is good
for one call: that tool, those exact arguments (``args`` is the SHA-256 of
their canonical JSON), ``LIFETIME_S`` seconds, and its ``jti`` once. It rides
in the arguments as ``ASSERTION_ARG``, since extensible-mcp sends a URL
server one fixed Bearer token and no per-call header; oe_connector removes it
before anything else reads the arguments. The model never sees it: it is
added to the copy sent downstream, and any value the model wrote there is
replaced.

**Drafts** (phase 4, ``integrations.erpnext_drafts``). oe_connector's
``draft_*`` tools (``draft_check`` and the ones that insert a draft) need a
``user`` assertion that also carries ``purpose: "draft"``. The gateway signs
one only inside a marker set by code, never by the model, bound to one tool
and the digest of its exact arguments:

- ``draft_check_run``: the proposal path validating a draft for its card,
  asked as the turn's own caller (``caller_for``), ``draft_check`` only;
- ``draft_check_as``: a person's own verified tap preparing the card an
  ERPNext alert suggests, ``draft_check`` only, asked as that person (an API
  request, with no session);
- ``draft_recheck_as``: the card list reading a waiting credit-raise card
  again for its approver (``integrations.erpnext_credit.recheck``, after
  ``verified.caller_refusal`` proved the viewer is that approver),
  ``draft_check`` only, asked as them (an API request, with no session);
- ``draft_executor_run``: the card executor carrying out an approved card,
  asked as the approver it names (an API request, with no session).

Any other ``erpnext__draft*`` call is refused in the gateway
(``mcp_gateway._check_erpnext_draft``).

**Watch jobs** (phase 0's S5, ``openexecutive.watch``). A job's runner sets
``watch_run(job, tool, arguments)`` around each of its reads: bound to a job
that is registered and on, to one of the tools it lists, and to the digest of
that call's exact arguments, which must fit the read's listed shape. It is
refused while a turn's session is bound or any other marker is set, and
without ``ERPNEXT_CALLER_PRIVATE_KEY`` (``watch-needs-keys``): a job never
takes the brief's unsigned service path. Inside it ``caller_for`` answers
``watch`` with ``sub`` the job's id before any session logic; oe_connector
holds the call to the job's listed reads. A draft tool is never signed as
``watch`` (``mint``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from openexecutive.api.caller_signing import load_private_key

logger = logging.getLogger(__name__)

# Kept in step with oe_connector/caller.py (its tests use the same vectors).
VERSION = "v1"
AUDIENCE = "oe_connector"
LIFETIME_S = 60
ASSERTION_ARG = "_oe_caller"
# The claim oe_connector requires on every draft tool (its caller.py).
PURPOSE_DRAFT = "draft"
# Every oe_connector tool that can write starts with this (normalized gateway
# names). Matched as a prefix, so a draft tool oe_connector adds later is
# gated here with no list to keep in step.
DRAFT_TOOL_PREFIX = "erpnext__draft"
DRAFT_CHECK_TOOL = "erpnext__draft_check"

ErpnextCallerKind = Literal["user", "service", "supplier", "watch"]
# oe_connector's watch.JOB_ID (watch.registry.JOB_ID is the same pattern).
WATCH_JOB_ID = re.compile(r"[a-z0-9-]{1,40}")

# Set only by briefing.erp_numbers, around the report runs it makes itself.
_brief_run: ContextVar[bool] = ContextVar("erpnext_brief_run", default=False)


@contextmanager
def brief_run() -> Iterator[None]:
    """Mark the ERPNext calls made inside as the principal's morning brief's
    own fixed report runs (``service``). Code only: no tool sets it."""
    token = _brief_run.set(True)
    try:
        yield
    finally:
        _brief_run.reset(token)


def in_brief_run() -> bool:
    return _brief_run.get()


# Set only by integrations.erpnext_oversight, around its own audit call.
_oversight_run: ContextVar[bool] = ContextVar("erpnext_oversight_run", default=False)


@contextmanager
def oversight_run() -> Iterator[None]:
    """Mark the ERPNext call made inside as the principal's oversight export
    asking what became of its drafts (``service``; the gateway lets only
    ``erpnext__audit_drafts`` through then). Code only: no tool sets it."""
    token = _oversight_run.set(True)
    try:
        yield
    finally:
        _oversight_run.reset(token)


def in_oversight_run() -> bool:
    return _oversight_run.get()


# Set only by orchestrator.answer_check, around its own context call.
_answer_check_run: ContextVar[bool] = ContextVar("erpnext_answer_check_run", default=False)


@contextmanager
def answer_check_run() -> Iterator[None]:
    """Mark the ERPNext call made inside as the answer check's own read of
    the site's formats and field types (oe_connector's
    ``answer_check_context``; the gateway lets only that tool through then,
    and refuses it anywhere else). It keeps the turn's own caller: a person,
    or the morning brief's ``service``. Code only: no tool sets it."""
    token = _answer_check_run.set(True)
    try:
        yield
    finally:
        _answer_check_run.reset(token)


def in_answer_check_run() -> bool:
    return _answer_check_run.get()


# Set only by integrations.erpnext_a2a, around its narrow agent's turn: the
# Supplier whose registered key signed the request.
_supplier_run: ContextVar[str] = ContextVar("erpnext_supplier_run", default="")
# oe_connector's own bound on a supplier caller's `sub` (its caller.py).
MAX_SUPPLIER_LEN = 140


def valid_supplier(name: object) -> bool:
    """Whether ``name`` is a Supplier name oe_connector takes as ``sub``."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= MAX_SUPPLIER_LEN
        and name == name.strip()
        and name.isprintable()
    )


@contextmanager
def supplier_run(supplier: str) -> Iterator[None]:
    """Mark the ERPNext calls made inside as answering ``supplier``'s own
    agent over A2A (``supplier``; the gateway lets only the supplier tools
    through then, and nothing else). Code only: no tool sets it."""
    if not valid_supplier(supplier):
        raise ValueError("a supplier caller is named by its Supplier in ERPNext")
    token = _supplier_run.set(supplier)
    try:
        yield
    finally:
        _supplier_run.reset(token)


def in_supplier_run() -> str:
    """The Supplier this A2A turn answers, or "" outside one."""
    return _supplier_run.get()


@dataclass(frozen=True)
class ErpnextCaller:
    kind: ErpnextCallerKind
    # The asker's email, lowercased; "" for ``service`` and ``supplier``.
    email: str = ""
    # The Supplier's name in ERPNext, for ``supplier`` only.
    supplier: str = ""
    # The job's id, for ``watch`` only.
    job: str = ""


@dataclass(frozen=True)
class DraftCall:
    """One draft call code is about to make: the gateway tool, the digest of
    its exact arguments, and who it runs as (None: the turn's own caller)."""

    tool: str
    digest: str
    caller: ErpnextCaller | None = None


_draft_call: ContextVar[DraftCall | None] = ContextVar("erpnext_draft_call", default=None)


@contextmanager
def _marked(call: DraftCall) -> Iterator[None]:
    token = _draft_call.set(call)
    try:
        yield
    finally:
        _draft_call.reset(token)


def draft_check_run(arguments: dict[str, Any]) -> Any:
    """Mark the one ``erpnext__draft_check`` call with exactly ``arguments``
    that the proposal path makes to fill in its card. Code only."""
    return _marked(DraftCall(DRAFT_CHECK_TOOL, arguments_digest(arguments)))


def draft_check_as(arguments: dict[str, Any], email: str) -> Any:
    """Mark the one ``erpnext__draft_check`` call with exactly ``arguments``
    that a person's own verified tap makes, asked as ``email`` with no turn:
    preparing the card an ERPNext alert suggests
    (``integrations.erpnext_alert_drafts``, after ``verified.caller_refusal``
    proved the tapper is that person). Code only."""
    asker = email.strip().lower()
    if "@" not in asker:
        raise ValueError("a draft is checked as a person's own ERPNext user")
    return _marked(DraftCall(DRAFT_CHECK_TOOL, arguments_digest(arguments), ErpnextCaller("user", asker)))


def draft_recheck_as(arguments: dict[str, Any], approver_email: str) -> Any:
    """Mark the one ``erpnext__draft_check`` call with exactly ``arguments``
    that the card list makes to read a waiting credit-raise card again, as
    its approver ``approver_email`` with no turn
    (``integrations.erpnext_credit.recheck``, only after
    ``verified.caller_refusal`` proved the viewer is that approver). It reads
    and never writes: draft_check saves nothing. Code only."""
    email = approver_email.strip().lower()
    if "@" not in email:
        raise ValueError("a card is read again as its approver's own ERPNext user")
    return _marked(DraftCall(DRAFT_CHECK_TOOL, arguments_digest(arguments), ErpnextCaller("user", email)))


def draft_executor_run(tool: str, arguments: dict[str, Any], approver_email: str) -> Any:
    """Mark the one draft call (``tool`` with exactly ``arguments``) the card
    executor makes for an approved card, run as ``approver_email``. Code only."""
    email = approver_email.strip().lower()
    if "@" not in email or not tool.startswith(DRAFT_TOOL_PREFIX + "_") or tool == DRAFT_CHECK_TOOL:
        raise ValueError("a draft is created by a draft tool, as the person who approved it")
    return _marked(DraftCall(tool, arguments_digest(arguments), ErpnextCaller("user", email)))


def draft_call_for(tool: str, arguments: dict[str, Any]) -> DraftCall | None:
    """The marker covering exactly this call (normalized ``tool``, the
    arguments without any assertion), or None."""
    call = _draft_call.get()
    if call is None or call.tool != tool or call.digest != arguments_digest(arguments):
        return None
    return call


@dataclass(frozen=True)
class WatchCall:
    """One read a watch job's runner is about to make: the job, the gateway
    tool and the digest of its exact arguments."""

    job: str
    tool: str
    digest: str


_watch_call: ContextVar[WatchCall | None] = ContextVar("erpnext_watch_call", default=None)


class WatchRefused(Exception):
    """A watch marker that may not be set; ``reason`` is a short code for the
    run's record and the log, never ERPNext's text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def other_marker_bound() -> bool:
    """Whether any marker but a watch read's is set: the brief's, the
    oversight export's, the answer check's, a supplier's or a draft's."""
    return bool(
        _brief_run.get()
        or _oversight_run.get()
        or _answer_check_run.get()
        or _supplier_run.get()
        or _draft_call.get() is not None
    )


def _watch_today() -> date:
    from datetime import datetime

    from openexecutive.memory.workspace_settings import get_user_timezone

    return datetime.now(get_user_timezone()).date()


@contextmanager
def watch_run(job_id: str, tool: str, arguments: dict[str, Any], *, today: date | None = None) -> Iterator[None]:
    """Mark the one ERPNext read (gateway ``tool`` with exactly
    ``arguments``) a watch job's runner makes. Code only: no tool sets it.

    Raises ``WatchRefused`` when a turn's session or any other marker is
    bound, without per-person access, or unless ``job_id`` is registered and
    on and ``tool`` with ``arguments`` fits one of its listed reads
    (``today``, the run's day, bounds a ``$date``). It saves the marker it
    found and puts it back on exit, so it can't leak into what runs after."""
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.watch import registry
    from openexecutive.watch.settings import job_enabled

    if current_session.get() is not None:
        raise WatchRefused("watch-in-turn")
    if other_marker_bound() or _watch_call.get() is not None:
        raise WatchRefused("watch-with-other-marker")
    setting = signing_setting()
    if not setting:
        raise WatchRefused("watch-needs-keys")
    try:
        load_private_key(setting)
    except ValueError:
        raise WatchRefused("key-malformed") from None
    job = registry.get(job_id)
    if job is None:
        raise WatchRefused("watch-unknown-job")
    if not job_enabled(job.id):
        raise WatchRefused("watch-job-off")
    if tool not in job.tools():
        raise WatchRefused("watch-tool-not-listed")
    plain = {k: v for k, v in arguments.items() if k != ASSERTION_ARG}
    if registry.read_for(job, tool, plain, today or _watch_today()) is None:
        raise WatchRefused("watch-arguments")
    prior = _watch_call.get()
    _watch_call.set(WatchCall(job.id, tool, arguments_digest(plain)))
    try:
        yield
    finally:
        _watch_call.set(prior)


def watch_call() -> WatchCall | None:
    """The watch read being made, or None outside one."""
    return _watch_call.get()


class CallerRefused(Exception):
    """This turn may not ask ERPNext; ``message`` is for the model."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.message = message
        # A short code for the audit row and the log.
        self.reason = reason


def signing_setting() -> str:
    """``ERPNEXT_CALLER_PRIVATE_KEY``, stripped; "" when phase 2 is off."""
    from openexecutive.config import get_settings

    return (get_settings().erpnext_caller_private_key or "").strip()


def _email_of(person: Any) -> str:
    email = getattr(person, "email", None)
    return email.strip().lower() if isinstance(email, str) and "@" in email else ""


# A host turn's refusals (``_host_caller``) point to what works instead,
# naming neither this app nor ERPNext: the person sees one product.
_HOST_LOOKUP_REFUSED = (
    "Records can't be looked up in this chat right now. Say so in a sentence, "
    "without naming systems, and suggest asking in the Ask panel instead. Do "
    "not retry."
)


def _host_caller(session: Any) -> ErpnextCaller:
    """Who a host origin's turn asks ERPNext as: the person signed in to this
    app, and only them. The page proves nothing about who wrote the turn, so
    nothing it sends names anyone; the session's signed caller does
    (``api.caller``, verified per request), and the conversation must be
    private to them."""
    from openexecutive.api.caller import signing_on
    from openexecutive.memory.history_chat import private_chat
    from openexecutive.people.store import get_person

    if not (
        getattr(session, "from_web_chat", False) is True
        and getattr(session, "web_caller_signed_in", False) is True
        and signing_on()
    ):
        raise CallerRefused(_HOST_LOOKUP_REFUSED, "host_unsigned")
    pid = getattr(session, "caller_person_id", None)
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise CallerRefused(_HOST_LOOKUP_REFUSED, "host_no_person")
    try:
        person = get_person(pid)
        private = private_chat(session)
    except Exception:
        logger.warning("erpnext_caller: host caller lookup failed — refusing", exc_info=True)
        raise CallerRefused(_HOST_LOOKUP_REFUSED, "lookup_failed") from None
    if person is None or not person.active or person.kind != "team":
        raise CallerRefused(_HOST_LOOKUP_REFUSED, "host_no_person")
    if not private:
        raise CallerRefused(
            "Figures are only given in a private conversation with the person who "
            "asked, and others can read this one. Say so without naming systems.",
            "shared_conversation",
        )
    email = _email_of(person)
    if not email:
        raise CallerRefused(_HOST_LOOKUP_REFUSED, "host_no_email")
    return ErpnextCaller("user", email)


def caller_for(session: Any) -> ErpnextCaller:
    """Who this turn asks ERPNext as; raises ``CallerRefused`` when no one
    may. Fails closed: no session or a failed roster read refuses."""
    from openexecutive.orchestrator.content_trust import principal_speaking
    from openexecutive.orchestrator.people_tools import teammate_on_verified_surface

    # A watch job's read first, before any session logic: it is `watch`
    # only alone, with no turn bound and no other marker set.
    watch = _watch_call.get()
    if watch is not None:
        if session is not None or other_marker_bound():
            raise CallerRefused(
                "A watch job's read runs on its own, never inside a turn or another run.",
                "watch-not-alone",
            )
        return ErpnextCaller("watch", job=watch.job)
    supplier = in_supplier_run()
    if supplier:
        return ErpnextCaller("supplier", supplier=supplier)
    if in_brief_run() or in_oversight_run():
        return ErpnextCaller("service")
    if session is None:
        raise CallerRefused("ERPNext is not available outside a conversation.", "no_session")
    panel_email = getattr(session, "erpnext_panel_email", "")
    if isinstance(panel_email, str) and "@" in panel_email and getattr(session, "unattended", False) is not True:
        return ErpnextCaller("user", panel_email.strip().lower())
    if getattr(session, "unattended", False) is True:
        raise CallerRefused(
            "ERPNext is not available in a run nobody is watching, such as a "
            "scheduled follow-up. Say so; the person can ask again in a chat.",
            "unattended",
        )
    if getattr(session, "from_host", False) is True:
        return _host_caller(session)
    try:
        if principal_speaking(session):
            from openexecutive.people.store import find_principal_person

            email = _email_of(find_principal_person())
            if not email:
                raise CallerRefused(
                    "ERPNext answers each person as their own ERPNext user, found by "
                    "email, and the principal's People entry has no email. Say so, and "
                    "that adding the email they sign in to ERPNext with fixes it.",
                    "principal_no_email",
                )
            return ErpnextCaller("user", email)
        teammate = teammate_on_verified_surface(session)
    except CallerRefused:
        raise
    except Exception:
        logger.warning("erpnext_caller: speaker lookup failed — refusing", exc_info=True)
        raise CallerRefused(
            "ERPNext is not available right now: who is asking could not be checked.",
            "lookup_failed",
        ) from None
    if teammate is None:
        raise CallerRefused(
            "ERPNext data is shared only with people the Executive has verified, "
            "on their own turn. Say you cannot look it up for this person; do not "
            "retry with another tool.",
            "unverified",
        )
    from openexecutive.memory.history_chat import private_chat

    if not private_chat(session):
        raise CallerRefused(
            "ERPNext figures are only given in a private conversation with the "
            "person who asked, and others can read this one. Ask them to message "
            "you directly or use the web app.",
            "shared_conversation",
        )
    if getattr(session, "from_web_chat", False) is True:
        from openexecutive.api.caller import signing_on

        if not (signing_on() and getattr(session, "web_caller_signed_in", False) is True):
            raise CallerRefused(
                "ERPNext answers teammates in the web app only when sign-ins are "
                "signed (CALLER_ASSERTION_PUBLIC_KEYS). Say you cannot look it up "
                "for them here; an operator can turn signed sign-ins on.",
                "web_unsigned",
            )
    email = _email_of(teammate)
    if not email:
        raise CallerRefused(
            "ERPNext answers each person as their own ERPNext user, found by email, "
            "and this person's People entry has no email. Say you cannot look it up.",
            "teammate_no_email",
        )
    return ErpnextCaller("user", email)


def canonical_json(value: Any) -> str:
    """The one spelling both sides hash: sorted keys, no spaces, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def arguments_digest(arguments: dict[str, Any]) -> str:
    """SHA-256 (hex) of the call's arguments, the assertion itself left out."""
    plain = {k: v for k, v in arguments.items() if k != ASSERTION_ARG}
    return hashlib.sha256(canonical_json(plain).encode("utf-8")).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint(
    setting: str,
    caller: ErpnextCaller,
    *,
    tool: str,
    arguments: dict[str, Any],
    now: int | None = None,
    jti: str | None = None,
    purpose: str | None = None,
) -> str:
    """One assertion for one call of ``tool`` (oe_connector's own name, e.g.
    ``run_report``) with exactly ``arguments``; ``purpose`` adds that claim
    (``PURPOSE_DRAFT``, a person's only). Raises ``ValueError`` on a
    malformed key; the message never includes it."""
    if caller.kind == "watch":
        if caller.email or caller.supplier or not WATCH_JOB_ID.fullmatch(caller.job):
            raise ValueError("a watch caller is named by its job's id")
        if purpose is not None or tool.startswith("draft"):
            raise ValueError("a watch job only reads: no draft tool, no purpose")
    elif caller.job:
        raise ValueError("only a watch caller names a job")
    if caller.kind == "user" and "@" not in caller.email:
        raise ValueError("a user is named by their email")
    if caller.kind == "service" and (caller.email or caller.supplier):
        raise ValueError("the service caller names no one")
    if caller.kind == "supplier" and (caller.email or not valid_supplier(caller.supplier)):
        raise ValueError("a supplier caller is named by its Supplier in ERPNext")
    if caller.kind == "user" and caller.supplier:
        raise ValueError("a user is named by their email")
    if purpose is not None and (purpose != PURPOSE_DRAFT or caller.kind != "user"):
        raise ValueError("only a person's draft carries a purpose")
    kid, key = load_private_key(setting)
    iat = int(time.time()) if now is None else now
    claims: dict[str, Any] = {
        "args": arguments_digest(arguments),
        "aud": AUDIENCE,
        "exp": iat + LIFETIME_S,
        "iat": iat,
        "jti": jti or secrets.token_urlsafe(18),
        "kid": kid,
        "kind": caller.kind,
        "sub": (
            caller.supplier if caller.kind == "supplier" else caller.job if caller.kind == "watch" else caller.email
        ),
        "tool": tool,
    }
    if purpose is not None:
        claims["purpose"] = purpose
    signing_input = f"{VERSION}.{_b64url(canonical_json(claims).encode('utf-8'))}"
    return f"{signing_input}.{_b64url(key.sign(signing_input.encode('ascii')))}"


def with_assertion(
    setting: str,
    caller: ErpnextCaller,
    *,
    tool: str,
    arguments: dict[str, Any],
    purpose: str | None = None,
) -> dict[str, Any]:
    """A copy of ``arguments`` carrying a fresh assertion in ``ASSERTION_ARG``,
    replacing whatever the model put there."""
    plain = {k: v for k, v in arguments.items() if k != ASSERTION_ARG}
    return {**plain, ASSERTION_ARG: mint(setting, caller, tool=tool, arguments=plain, purpose=purpose)}
