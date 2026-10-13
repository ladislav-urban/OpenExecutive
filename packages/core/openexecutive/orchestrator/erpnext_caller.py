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
- the person in ERPNext's own "Ask the Executive" panel
  (``Session.erpnext_panel_email``, set only by integrations/erpnext_panel.py
  from an assertion oe_connector signed): asked as that same ERPNext user,
  whose word ERPNext already is;
- anyone else is refused here, before ERPNext is asked. That includes every
  run nobody is watching (``Session.unattended``): a scheduled follow-up's
  text and recipient come from whoever scheduled it, so signing it as the
  service user would hand them the service user's view. The ``service``
  kind is kept for a later path whose audience is fixed (the principal's
  morning brief); oe_connector holds it to the reports its operator lists.

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
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

from openexecutive.api.caller_signing import load_private_key

logger = logging.getLogger(__name__)

# Kept in step with oe_connector/caller.py (its tests use the same vectors).
VERSION = "v1"
AUDIENCE = "oe_connector"
LIFETIME_S = 60
ASSERTION_ARG = "_oe_caller"

ErpnextCallerKind = Literal["user", "service"]


@dataclass(frozen=True)
class ErpnextCaller:
    kind: ErpnextCallerKind
    # The asker's email, lowercased; "" for ``service``.
    email: str = ""


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
) -> str:
    """One assertion for one call of ``tool`` (oe_connector's own name, e.g.
    ``run_report``) with exactly ``arguments``. Raises ``ValueError`` on a
    malformed key; the message never includes it."""
    if caller.kind == "user" and "@" not in caller.email:
        raise ValueError("a user is named by their email")
    if caller.kind == "service" and caller.email:
        raise ValueError("the service caller names no one")
    kid, key = load_private_key(setting)
    iat = int(time.time()) if now is None else now
    claims = {
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
    signing_input = f"{VERSION}.{_b64url(canonical_json(claims).encode('utf-8'))}"
    return f"{signing_input}.{_b64url(key.sign(signing_input.encode('ascii')))}"


def with_assertion(
    setting: str, caller: ErpnextCaller, *, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """A copy of ``arguments`` carrying a fresh assertion in ``ASSERTION_ARG``,
    replacing whatever the model put there."""
    plain = {k: v for k, v in arguments.items() if k != ASSERTION_ARG}
    return {**plain, ASSERTION_ARG: mint(setting, caller, tool=tool, arguments=plain)}
