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
- the person in ERPNext's own "Ask the Dynocortex" panel
  (``Session.erpnext_panel_email``, set only by integrations/erpnext_panel.py
  from an assertion oe_connector signed): asked as that same ERPNext user,
  whose word ERPNext already is;
- the morning brief's own ERP NUMBERS runs (``brief_run``, set only by
  ``briefing.erp_numbers`` around the gateway calls it makes itself, never
  by the model): ``service``, which oe_connector holds to the reports its
  operator lists. Their audience is fixed, the principal;
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
- ``draft_executor_run``: the card executor carrying out an approved card,
  asked as the approver it names (an API request, with no session).

Any other ``erpnext__draft*`` call is refused in the gateway
(``mcp_gateway._check_erpnext_draft``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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

ErpnextCallerKind = Literal["user", "service"]

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


@dataclass(frozen=True)
class ErpnextCaller:
    kind: ErpnextCallerKind
    # The asker's email, lowercased; "" for ``service``.
    email: str = ""


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


def caller_for(session: Any) -> ErpnextCaller:
    """Who this turn asks ERPNext as; raises ``CallerRefused`` when no one
    may. Fails closed: no session or a failed roster read refuses."""
    from openexecutive.orchestrator.content_trust import principal_speaking
    from openexecutive.orchestrator.people_tools import teammate_on_verified_surface

    if in_brief_run():
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
    if caller.kind == "user" and "@" not in caller.email:
        raise ValueError("a user is named by their email")
    if caller.kind == "service" and caller.email:
        raise ValueError("the service caller names no one")
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
        "sub": caller.email,
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
