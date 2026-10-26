"""``POST /webhook/erpnext/chat`` — ERPNext's "Ask the Dynocortex" panel.

ERPNext phase 2b (design: oe_connector's ``docs/erpnext-integration.md``,
"Chat inside ERPNext"). The panel in ERPNext's desk posts to a whitelisted
method in oe_connector, which forwards the question here with a signed
assertion naming the signed-in ERPNext user; the browser never holds any
OpenExecutive credential. The route answers synchronously with the reply.

**Who is asking.** oe_connector signs ``v1.<payload>.<signature>`` with the
Ed25519 key only it holds (site config ``oe_connector_panel_private_key``);
this API holds the public half (``ERPNEXT_PANEL_PUBLIC_KEYS``). The claims
``{aud, body, exp, iat, jti, kid, sub}`` bind it to this audience, the SHA-256
of this exact request body, ``MAX_LIFETIME_S`` seconds and one use; ``sub`` is
the ERPNext user's email. It must name a rostered team member (not a contact,
not archived) or the panel is refused, and shows itself disabled.

**What such a turn may do.** The email is ERPNext's word, and an ERPNext
administrator can change a user's email, so the panel is never a surface that
proves who is speaking: not the principal's (``content_trust``), not a
verified teammate's (``people_tools``). Its turn only asks: it is offered its
specialists and ERPNext's read tools, nothing else
(``schedule_tools.ERPNEXT_PANEL_TOOLS``), but for leaving that person an
ERPNext draft card when drafts are offered them (phase 4b,
``erpnext_drafts.offer_refusal``): a card nothing writes until they approve
it in the OpenExecutive app, signed in, so ERPNext's word alone never
creates anything, and those ERPNext calls run as that
same ERPNext user (``erpnext_caller``, ``Session.erpnext_panel_email``). It
reads no one's memory: no person, episodic, briefing or standing-fact context,
and its specialists read no department's memory. Nothing it says is mirrored
anywhere (``Session.read_erpnext`` is set from its first turn). It does read
the company knowledge base, as a teammate on any channel does.

The panel needs per-person ERPNext access (``ERPNEXT_CALLER_PRIVATE_KEY``):
without it every answer would be the service user's view, so the route
answers 503.

Conversations live in this process (``_Conversations``), keyed by the person
and the panel's conversation id, and end on restart or after an idle hour.

**Showing what the answer talks about** (oe_connector's
``docs/erpnext-panel-views-plan.md``). The reply also carries ``views``: the
``view`` argument of each ``erpnext__show_in_erpnext`` call the turn made, in
order, and ``call``: the turn's last ``list_records``, ``run_report`` or
``get_record`` call, for oe_connector to fall back on. The gateway notes them
(``note_erpnext_call``) once a call succeeded. Both are data for oe_connector,
which checks every view again as the signed-in ERPNext user and builds each
route itself; nothing here is shown or followed.

**Opening a card it left** (oe_connector's ``docs/erpnext-single-ui-plan.md``,
step 4). The reply also carries ``cards``: the decision id of each ERPNext
draft card ``propose_erpnext_draft`` left or found waiting on this turn, in
order, at most ``MAX_CARDS`` (``note_panel_card``), never read from the
model's text. oe_connector shows a chip for each that opens the card's page
in its hosted view, where this app shows it only to its own person, signed
in, and approving it stays there.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from openexecutive.api.caller import CallerKeysError, JtiLedger, parse_public_keys

logger = logging.getLogger(__name__)

router = APIRouter()

PATH = "/webhook/erpnext/chat"
ASSERTION_HEADER = "x-oe-panel-assertion"
# Kept in step with oe_connector/panel.py (its tests use the same vector).
VERSION = "v1"
AUDIENCE = "openexecutive-erpnext-panel"
MAX_LIFETIME_S = 120
CLOCK_SKEW_S = 30

MAX_BODY_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 4000
# How long one answer may take before the panel is told to try again.
TURN_TIMEOUT_S = 150.0
# Questions per person per hour: each runs the Executive's model.
RATE_LIMIT = 30
RATE_WINDOW_S = 3600.0

_MAX_ASSERTION_LEN = 4096
_MAX_EMAIL_LEN = 320
_KID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_JTI_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+")
_CONVERSATION_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Page context values: a DocType, document or report name, as ERPNext names
# them. Anything else is dropped rather than quoted to the model.
_PAGE_VALUE_RE = re.compile(r"[\w .,:/()&'+-]{1,140}")
# One of Dynocortex's own screens beside the panel, by this app's path for it
# (`/watchlist`, `/settings/status`): the host sends it where no ERPNext page is open.
_SCREEN_RE = re.compile(r"/[a-z0-9_/-]{0,139}")

_ledger = JtiLedger()


class PanelRefused(Exception):
    """The request does not hold; ``reason`` is for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _b64url(text: str) -> bytes:
    if not _B64URL_RE.fullmatch(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signs(key: Ed25519PublicKey, signature: bytes, data: bytes) -> bool:
    try:
        key.verify(signature, data)
    except InvalidSignature:
        return False
    return True


def _seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PanelRefused("malformed")
    return value


def verify_panel_assertion(
    token: str,
    *,
    keys: dict[str, Ed25519PublicKey],
    body: bytes,
    now: int,
    ledger: JtiLedger,
    audience: str = AUDIENCE,
) -> str:
    """The ERPNext user's email ``token`` vouches for, for this one request
    ``body``; raises ``PanelRefused``. Its ``jti`` is spent only when every
    other check passes.

    With another ``audience`` (``integrations.erpnext_events``) the site
    itself is speaking: ``sub`` must be "" and "" is returned. The audience
    keeps one route's assertion from being replayed at the other."""
    if not token or len(token) > _MAX_ASSERTION_LEN or not token.isascii():
        raise PanelRefused("malformed")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise PanelRefused("malformed")
    try:
        payload, signature = _b64url(parts[1]), _b64url(parts[2])
    except ValueError:
        raise PanelRefused("malformed") from None
    # The signature first, so no unsigned byte reaches the JSON parser.
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    signed_by = next((kid for kid, key in keys.items() if _signs(key, signature, signing_input)), None)
    if signed_by is None:
        raise PanelRefused("signature")
    try:
        claims = json.loads(payload)
    except (ValueError, RecursionError):
        raise PanelRefused("malformed") from None
    if not isinstance(claims, dict) or claims.get("kid") != signed_by:
        raise PanelRefused("key_id")
    iat, exp = _seconds(claims.get("iat")), _seconds(claims.get("exp"))
    if claims.get("aud") != audience:
        raise PanelRefused("audience")
    if not 0 < exp - iat <= MAX_LIFETIME_S:
        raise PanelRefused("lifetime")
    if iat > now + CLOCK_SKEW_S:
        raise PanelRefused("not_yet_valid")
    if now > exp:
        raise PanelRefused("expired")
    digest = claims.get("body")
    if (
        not isinstance(digest, str)
        or not _DIGEST_RE.fullmatch(digest)
        or digest != hashlib.sha256(body).hexdigest()
    ):
        raise PanelRefused("body")
    jti, sub = claims.get("jti"), claims.get("sub")
    if not isinstance(jti, str) or not _JTI_RE.fullmatch(jti):
        raise PanelRefused("malformed")
    if audience != AUDIENCE:
        if sub != "":
            raise PanelRefused("subject")
    elif (
        not isinstance(sub, str)
        or len(sub) > _MAX_EMAIL_LEN
        or sub != sub.strip().lower()
        or not _EMAIL_RE.fullmatch(sub)
    ):
        raise PanelRefused("subject")
    try:
        ledger.claim(jti, exp, now)
    except Exception:
        raise PanelRefused("replayed") from None
    return sub


class _RateLimiter:
    """At most ``limit`` questions per key in any ``window`` seconds."""

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float) -> bool:
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True


@dataclass
class _Conversation:
    session: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched: float = 0.0
    # The ERPNext email that started it: a conversation is never continued
    # under another (the People entry's email changed, another ERPNext user
    # in the same browser).
    email: str = ""


class _Conversations:
    """The panel's open conversations, least recently used first."""

    def __init__(self, limit: int = 500, idle_s: float = 3600.0) -> None:
        self._items: OrderedDict[str, _Conversation] = OrderedDict()
        self._limit = limit
        self._idle_s = idle_s

    def get(self, session_id: str, email: str, make: Any, now: float) -> _Conversation:
        for key in [k for k, c in self._items.items() if now - c.touched > self._idle_s and not c.lock.locked()]:
            del self._items[key]
        conversation = self._items.get(session_id)
        if conversation is not None and conversation.email != email and not conversation.lock.locked():
            del self._items[session_id]
            conversation = None
        if conversation is None:
            conversation = _Conversation(session=make(), email=email)
            self._items[session_id] = conversation
            while len(self._items) > self._limit:
                oldest = next(iter(self._items))
                if self._items[oldest].lock.locked():
                    break
                del self._items[oldest]
        conversation.touched = now
        self._items.move_to_end(session_id)
        return conversation


_rate = _RateLimiter(RATE_LIMIT, RATE_WINDOW_S)
_conversations = _Conversations()
# One question in flight per person: each holds an ERPNext worker until it
# is answered.
_in_flight: set[str] = set()


def panel_keys() -> dict[str, Ed25519PublicKey]:
    """``ERPNEXT_PANEL_PUBLIC_KEYS``; raises ``CallerKeysError`` when malformed."""
    from openexecutive.config import get_settings

    return parse_public_keys(get_settings().erpnext_panel_public_keys or "")


def _ready() -> str | None:
    """Why the panel cannot answer on this install, else None."""
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.orchestrator.mcp_gateway import gateway_server_names, get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_SERVER

    if not erpnext_caller.signing_setting():
        return "ERPNEXT_CALLER_PRIVATE_KEY is not set (per-person ERPNext access)"
    if ERPNEXT_SERVER not in gateway_server_names(get_active_gateway()):
        return "the erpnext MCP server is not configured"
    return None


def page_block(page: Any) -> str:
    """What the person has open in ERPNext, from the fields the panel sends,
    as an untrusted block for the user turn: document names can be written by
    outsiders (a web-form lead, a customer's name). Unknown or odd values are
    left out. ``screen`` names one of Dynocortex's own screens instead, by
    this app's path for it (SCREEN_RULE says what the turn can't see there)."""
    if not isinstance(page, dict):
        return ""

    def value(key: str) -> str:
        raw = page.get(key)
        return raw.strip() if isinstance(raw, str) and _PAGE_VALUE_RE.fullmatch(raw.strip()) else ""

    doctype, name, report = value("doctype"), value("name"), value("report")
    if report:
        where = f"the report {report}"
    elif doctype and name:
        where = f"{doctype} {name}"
    elif doctype:
        where = f"the {doctype} list"
    else:
        screen = page.get("screen")
        if not isinstance(screen, str) or not _SCREEN_RE.fullmatch(screen) or "//" in screen:
            return ""
        from openexecutive.orchestrator.content_trust import wrap_untrusted

        return wrap_untrusted(
            f"Open in Dynocortex: its own screen {screen}, not an ERPNext page.",
            source="erpnext",
            author="erpnext_page",
        )
    from openexecutive.orchestrator.content_trust import wrap_untrusted

    return wrap_untrusted(f"Open in ERPNext: {where}.", source="erpnext", author="erpnext_page")


# Both panel blocks end with it: the panel opens what the answer is about beside
# it, from views this tool names (oe_connector checks each one).
SHOW_RULE = (
    "When your answer is about something in ERPNext (the list you ranked, the "
    "report you ran, the document you described), also call "
    "erpnext__show_in_erpnext with it, with the same filters and sort, once per "
    "place and the main one first, so ERPNext opens it beside the panel. Pass "
    "{'view': {'kind': ..., ...}}. Show the place that fits the question, not "
    "the nearest report: for \"my largest orders\", the list sorted as you "
    "ranked it ({'kind': 'list', 'doctype': 'Sales Order', 'filters': "
    "{'company': ..., 'docstatus': 1}, 'sort': {'field': 'base_grand_total', "
    "'order': 'desc'}}); for the chart of accounts, the tree ({'kind': 'tree', "
    "'doctype': 'Account'}); for \"where do I find the stock reports\", the "
    "workspace ({'kind': 'workspace', 'workspace': 'Stock'}). Never write "
    "routes or links."
)
# Both panel blocks carry it too: beside one of Dynocortex's own screens the
# panel names the screen, but its turn reads none of what the screen shows.
SCREEN_RULE = (
    "When the page block says one of Dynocortex's own screens is open (its "
    "Home, Watch list, People, Settings and the like), you can't see what that "
    "screen shows: not their briefing, watches, people, memory or settings. "
    "Say so in a sentence, answer what you can from ERPNext and your "
    "specialists, and suggest Chats for the rest. Never guess what the "
    "screen shows. "
)
PANEL_CONTEXT_BLOCK = (
    "This message comes from the \"Ask the Dynocortex\" panel inside ERPNext, "
    "a chat docked on every ERPNext page; people there call you Dynocortex, "
    "and that is you. "
    "Here you can only read ERPNext (its tools run as this person's own ERPNext "
    "user) and consult your specialists; you cannot send, schedule, save or "
    "change anything, or reach any other system. Keep answers short enough for "
    "a side panel. If they ask for something you cannot do here, say so and "
    "suggest the OpenExecutive app. "
    + SCREEN_RULE
    + SHOW_RULE
)
# The same, for a person this turn may prepare ERPNext drafts for (phase 4b,
# integrations.erpnext_drafts.offered): proposing is the one thing it may
# leave, and only as a card approved in the OpenExecutive app.
PANEL_DRAFTS_CONTEXT_BLOCK = (
    "This message comes from the \"Ask the Dynocortex\" panel inside ERPNext, "
    "a chat docked on every ERPNext page; people there call you Dynocortex, "
    "and that is you. "
    "Here you can read ERPNext (its tools run as this person's own ERPNext "
    "user), consult your specialists, and, when they ask in their own words, "
    "leave them an ERPNext draft card with propose_erpnext_draft. Tell them "
    "the card waits for their approval in OpenExecutive, signed in, that the "
    "panel may show a button under your reply to open it, and that nothing is "
    "in ERPNext until they approve it. You cannot send, schedule or save anything, or "
    "reach any other system. Keep answers short enough for a side panel. If "
    "they ask for something you cannot do here, say so and suggest the "
    "OpenExecutive app. "
    + SCREEN_RULE
    + SHOW_RULE
)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


async def _read_capped(request: Request) -> bytes | None:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


# What a panel turn showed: its show_in_erpnext views, its last read call and
# the draft cards it left.
MAX_VIEWS = 5
MAX_CARDS = 5
MAX_VIEW_JSON = 4000
_READ_CALLS = {
    "erpnext__list_records": "list_records",
    "erpnext__run_report": "run_report",
    "erpnext__get_record": "get_record",
}


@dataclass
class _TurnViews:
    views: list[dict[str, Any]] = field(default_factory=list)
    call: dict[str, Any] | None = None
    cards: list[int] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.views:
            out["views"] = self.views
        if self.call is not None:
            out["call"] = self.call
        if self.cards:
            out["cards"] = self.cards
        return out


# Set by the route around one turn; the turn's tasks share the holder.
_turn_views: ContextVar[_TurnViews | None] = ContextVar("erpnext_panel_turn_views", default=None)


def _small(value: Any) -> bool:
    try:
        return len(json.dumps(value, ensure_ascii=False)) <= MAX_VIEW_JSON
    except (TypeError, ValueError):
        return False


def note_erpnext_call(normalized: str, arguments: Any, *, failed: bool) -> None:
    """Called by the gateway after an ``erpnext__*`` call: on a panel turn,
    keep what it showed. Nothing for a failed call, or outside the panel."""
    from openexecutive.orchestrator.erpnext_caller import ASSERTION_ARG
    from openexecutive.prompts.connected_systems import ERPNEXT_SHOW_TOOL

    turn = _turn_views.get()
    if turn is None or failed or not isinstance(arguments, dict):
        return
    plain = {k: v for k, v in arguments.items() if k != ASSERTION_ARG}
    if normalized == ERPNEXT_SHOW_TOOL:
        view = plain.get("view")
        if isinstance(view, dict) and len(turn.views) < MAX_VIEWS and _small(view):
            turn.views.append(view)
    elif normalized in _READ_CALLS and _small(plain):
        turn.call = {"tool": _READ_CALLS[normalized], "arguments": plain}


def note_panel_card(decision_id: int) -> None:
    """Called by ``erpnext_drafts`` once a card is stored or found waiting: on
    a panel turn, keep its id, in the order left. Nothing outside the panel."""
    turn = _turn_views.get()
    if turn is None or decision_id in turn.cards or len(turn.cards) >= MAX_CARDS:
        return
    turn.cards.append(decision_id)


def context_block(session: Any) -> str:
    """The panel's channel block: with drafts for this person, or without."""
    from openexecutive.integrations.erpnext_drafts import offered

    return PANEL_DRAFTS_CONTEXT_BLOCK if offered(session) else PANEL_CONTEXT_BLOCK


def _person_for(email: str) -> Any:
    from openexecutive.people.store import find_person_by_email

    person = find_person_by_email(email)
    if person is None or not person.active or person.kind != "team" or person.id is None:
        return None
    return person


async def _answer(person: Any, email: str, conversation_id: str, message: str, page: Any) -> str:
    from openexecutive.audit import log_event as audit_log
    from openexecutive.config import get_settings
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.orchestrator.session import Session

    session_id = f"erpnext:{person.id}:{conversation_id}"

    def make() -> Session:
        profile = load_or_create_profile()
        return Session(
            session_id=session_id,
            company_profile=profile if not profile.is_empty() else None,
            origin_channel="erpnext",
            erpnext_panel_email=email,
            # Nothing from this conversation reaches memory other people
            # read, whatever a turn calls.
            read_erpnext=True,
        )

    conversation = _conversations.get(session_id, email, make, time.monotonic())
    if conversation.email != email:
        raise PanelRefused("conversation_busy")
    async with conversation.lock:
        session = conversation.session
        audit_log(
            "integration_inbound",
            f"Inbound erpnext panel from person {person.id}: {message[:160]}",
            actor="erpnext_panel",
            session_id=session_id,
            details={"channel": "erpnext", "person_id": person.id, "text_len": len(message)},
        )
        retrieved_context = await asyncio.to_thread(
            retrieve, query=message, max_chars=get_settings().knowledge_chat_max_chars
        )
        where = page_block(page)
        reply = await asyncio.wait_for(
            Executive(mcp_gateway=get_active_gateway()).chat(
                user_message=f"{where}\n\n{message}" if where else message,
                session=session,
                retrieved_context=retrieved_context,
                channel_context_block=context_block(session),
                memory_text=message,
                # No standing facts: they include teammates' own, "(per
                # <name>)", and ERPNext's word is all that names this person.
                standing_facts="",
            ),
            timeout=TURN_TIMEOUT_S,
        )
        return reply


@router.post(PATH)
async def erpnext_panel_chat(request: Request) -> JSONResponse:
    try:
        keys = panel_keys()
    except CallerKeysError:
        logger.error("ERPNEXT_PANEL_PUBLIC_KEYS is malformed; refusing the ERPNext panel")
        return _error(503, "not_configured", "The ERPNext panel is not set up on OpenExecutive.")
    if not keys:
        return _error(503, "not_configured", "The ERPNext panel is not set up on OpenExecutive.")
    body = await _read_capped(request)
    if body is None:
        return _error(413, "too_large", "That message is too long.")
    tokens = request.headers.getlist(ASSERTION_HEADER)
    try:
        if len(tokens) != 1:
            raise PanelRefused("missing" if not tokens else "duplicate_header")
        email = verify_panel_assertion(
            tokens[0].strip(), keys=keys, body=body, now=int(time.time()), ledger=_ledger
        )
    except PanelRefused as refused:
        logger.warning("erpnext panel refused reason=%s", refused.reason)
        return _error(401, "unauthorized", "This request was not signed by the ERPNext site.")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return _error(400, "bad_request", "The request is not JSON.")
    if not isinstance(data, dict):
        return _error(400, "bad_request", "The request is not a JSON object.")
    unready = _ready()
    if unready is not None:
        logger.warning("erpnext panel unavailable: %s", unready)
        return _error(503, "not_configured", "The ERPNext panel is not set up on OpenExecutive.")
    person = await asyncio.to_thread(_person_for, email)
    if person is None:
        return _error(
            403,
            "not_on_roster",
            "Your ERPNext email is not on OpenExecutive's People page, so the Executive "
            "cannot answer you here. Ask the person who runs OpenExecutive to add you.",
        )
    if data.get("check") is True:
        return JSONResponse({"enabled": True, "name": person.full_name})

    message = data.get("message")
    conversation_id = data.get("conversation")
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
        return _error(400, "bad_request", f"Ask a question of up to {MAX_MESSAGE_CHARS} characters.")
    if not isinstance(conversation_id, str) or not _CONVERSATION_RE.fullmatch(conversation_id):
        return _error(400, "bad_request", "The conversation id is not valid.")
    if email in _in_flight:
        return _error(429, "busy", "The Executive is still answering your last question.")
    if not _rate.allow(email, time.monotonic()):
        return _error(429, "rate_limited", "You have asked a lot this hour; try again later.")
    if getattr(person, "demo", False) is True:
        # A demo person's questions a day count here as in the web chat (people.demo).
        from openexecutive.config import get_settings
        from openexecutive.people import demo as people_demo

        limit = get_settings().demo_daily_questions
        if not await asyncio.to_thread(people_demo.take_question, person.id, limit):
            return _error(429, "daily_limit", people_demo.limit_message(limit))
    _in_flight.add(email)
    shown = _TurnViews()
    views_token = _turn_views.set(shown)
    try:
        reply = await _answer(person, email, conversation_id, message.strip(), data.get("page"))
    except TimeoutError:
        return _error(504, "timeout", "That took too long to answer; try a narrower question.")
    except PanelRefused:
        return _error(409, "conversation_busy", "Start a new chat and ask again.")
    except Exception:
        logger.exception("erpnext panel: the turn failed for person %s", person.id)
        return _error(500, "failed", "Something went wrong answering that; please try again.")
    finally:
        _turn_views.reset(views_token)
        _in_flight.discard(email)
    return JSONResponse({"reply": reply, **shown.payload()})
