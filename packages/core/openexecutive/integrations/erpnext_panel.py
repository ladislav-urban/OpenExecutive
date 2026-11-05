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
``docs/finished_work/erpnext-panel-views-plan.md``). The reply also carries ``views``: the
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
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import contextmanager
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
    with_subject: bool | None = None,
) -> str:
    """The ERPNext user's email ``token`` vouches for, for this one request
    ``body``; raises ``PanelRefused``. Its ``jti`` is spent only when every
    other check passes.

    With another ``audience`` (``integrations.erpnext_events``) the site
    itself is speaking: ``sub`` must be "" and "" is returned, unless
    ``with_subject`` says that audience names a user too, as the demo admin
    console's do (``integrations.erpnext_demo_people``). The audience keeps
    one route's assertion from being replayed at another."""
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
    names_user = audience == AUDIENCE if with_subject is None else with_subject
    if not names_user:
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


def _page_value(page: dict[str, Any], key: str) -> str:
    """``page[key]`` folded as ``wrap_untrusted`` will fold it, if it fits
    the page's value rule, else ""."""
    raw = page.get(key)
    if not isinstance(raw, str) or len(raw) > 560:
        return ""
    # Folded first, as wrap_untrusted will: the cap is on what the model reads.
    text = unicodedata.normalize("NFKC", raw).strip()
    return text if _PAGE_VALUE_RE.fullmatch(text) else ""


def page_ref(page: Any) -> dict[str, str] | None:
    """Where a conversation was asked, from the page beside ERPNext's docked
    panel, by the values ``page_block`` keeps: ``{report}``, ``{doctype,
    name}`` or ``{doctype}``, never filters or sort; None beside one of
    Dynocortex's own screens or when nothing passes. Shown as text, and it
    grants nothing (``sessions.origin_ref``)."""
    if not isinstance(page, dict):
        return None
    doctype, name, report = (_page_value(page, key) for key in ("doctype", "name", "report"))
    if report:
        return {"report": report}
    if doctype and name:
        return {"doctype": doctype, "name": name}
    if doctype:
        return {"doctype": doctype}
    return None


def page_block(page: Any) -> str:
    """What the person has open in ERPNext, from the fields the panel sends,
    as an untrusted block for the user turn: document names can be written by
    outsiders (a web-form lead, a customer's name). Unknown or odd values are
    left out. ``screen`` names one of Dynocortex's own screens instead, by
    this app's path for it (SCREEN_RULE says what the turn can't see there).
    Beside a list or a report, ``filters`` and (a list's) ``sort`` add what
    narrows it; beside Home, ``card`` adds the headline of the card in focus,
    as text the browser sent, never a card read from this app's store."""
    if not isinstance(page, dict):
        return ""

    doctype, name, report = (_page_value(page, key) for key in ("doctype", "name", "report"))
    from openexecutive.orchestrator.content_trust import wrap_untrusted

    lines: list[str] = []
    if report:
        lines.append(f"Open in ERPNext: the report {report}.")
    elif doctype and name:
        lines.append(f"Open in ERPNext: {doctype} {name}.")
    elif doctype:
        lines.append(f"Open in ERPNext: the {doctype} list.")
    else:
        screen = page.get("screen")
        if not isinstance(screen, str) or not _SCREEN_RE.fullmatch(screen) or "//" in screen:
            return ""
        lines.append(f"Open in Dynocortex: its own screen {screen}, not an ERPNext page.")
        card = _printable(page.get("card"), MAX_CARD_CHARS) if screen == HOME_SCREEN else ""
        if card:
            lines.append(f"Home card in focus (its headline as Home shows it): {card}")
    # A list or a report, never a form: what narrows the rows on screen.
    if report or (doctype and not name):
        filters = page_filters(page.get("filters"))
        if filters:
            lines.append(
                "Filters on screen: "
                + "; ".join(f"{field} {op} {text}" for field, op, text in filters)
                + "."
            )
        sort = page.get("sort")
        if not report and isinstance(sort, str) and _SORT_RE.fullmatch(sort):
            lines.append(f"Sorted by {sort}.")
    return wrap_untrusted("\n".join(lines), source="erpnext", author="erpnext_page")


# The screen and the filters on it (oe_connector's panel-first plan, C): what
# the person narrowed the list or report to, and on Home the card they clicked,
# by its headline only. Agreed with oe_connector's `panel._page`, which checks
# the same before it signs; anything that doesn't fit is dropped, one entry at
# a time. All of it is the person's or the page's text, so untrusted.
MAX_FILTERS = 10
MAX_FILTER_VALUE_CHARS = 140
MAX_CARD_CHARS = 200
FILTER_OPS = frozenset({
    "=", "!=", ">", "<", ">=", "<=", "like", "not like", "in", "not in",
    "between", "is", "is not", "timespan",
})
# Home, by this app's path for it (oe_connector's NATIVE_SCREENS): the card
# headline counts only beside it.
HOME_SCREEN = "/"
_FIELD_RE = re.compile(r"[a-z_][a-z0-9_]{0,63}")
_SORT_RE = re.compile(r"[a-z_][a-z0-9_]{0,63} (?:asc|desc)")


def _printable(raw: Any, limit: int) -> str:
    """``raw`` with its whitespace collapsed, if it is printable text of at
    most ``limit`` characters, else "". Measured after the NFKC fold
    ``wrap_untrusted`` applies, so a compatibility character that unfolds
    into many (U+FDFA into 18) can't stretch what the model reads."""
    if not isinstance(raw, str) or len(raw) > 4 * limit:
        return ""
    text = " ".join(unicodedata.normalize("NFKC", raw).split())
    return text if 0 < len(text) <= limit and text.isprintable() else ""


def page_filters(raw: Any) -> list[tuple[str, str, str]]:
    """The panel's ``filters``, ``[field, op, value]`` each, the first
    ``MAX_FILTERS`` of them, keeping only the entries that fit."""
    if not isinstance(raw, list):
        return []
    kept: list[tuple[str, str, str]] = []
    for entry in raw[:MAX_FILTERS]:
        if not isinstance(entry, list) or len(entry) != 3:
            continue
        field, op, value = entry
        if not isinstance(field, str) or not _FIELD_RE.fullmatch(field):
            continue
        if not isinstance(op, str) or op not in FILTER_OPS:
            continue
        text = _printable(value, MAX_FILTER_VALUE_CHARS)
        if not text or (op in ("is", "is not") and text not in ("set", "not set")):
            continue
        kept.append((field, op, text))
    return kept


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
    "workspace ({'kind': 'workspace', 'workspace': 'Stock'}). For a trend "
    "over days, weeks or months, or a breakdown by a field (\"receivables by "
    "due week\", \"orders by territory\"), a chart ({'kind': 'chart', "
    "'doctype': 'Sales Invoice', 'measure': 'sum', 'on': 'outstanding_amount', "
    "'over': {'time': 'due_date', 'every': 'week', 'from': ..., 'to': ...}, "
    "'filters': {'company': ..., 'outstanding_amount': ['>', 0]}, 'type': "
    "'bar'}, or 'over': {'group': 'territory', 'top': 10}): over a Date field "
    "or grouped by a Link or Select field, pie, donut or percentage only for a "
    "group's shares of a count or sum; a report that draws its own chart, such "
    "as Profit and Loss, stays the report, and a single figure gets no chart. "
    "Never write "
    "routes or links. Call it before you write your answer, and don't write "
    "that you opened or showed anything: the person sees it open beside the "
    "panel."
)
# Both panel blocks carry it too: beside one of Dynocortex's own screens the
# panel names the screen, but its turn reads none of what the screen shows.
SCREEN_RULE = (
    "When the page block says one of Dynocortex's own screens is open (its "
    "Home, Watch list, People, Settings and the like), you can't see what that "
    "screen shows: not their briefing, watches, people, memory or settings. "
    "Say so in a sentence, answer what you can from ERPNext and your "
    "specialists, and suggest Conversations for the rest. They already have that "
    "screen open beside you, so never send them to it, or to the "
    "OpenExecutive app for it: say the screen beside the panel shows it. "
    "Never guess what the screen shows, and describe it only from this turn's "
    "page block and what you open on this turn: once you open a report beside "
    "the panel, the list that was there before is gone, so never point at it. "
    "When the page block names the Home "
    "card in focus, its headline is all you have of it: answer about what it "
    "names from ERPNext and your specialists. "
)
# Both panel blocks carry it: a panel conversation goes on only while its
# earlier turns are on screen (oe_connector#386: a panel that opens empty
# starts a new one), so a follow-up is read with them. Replaces the rule that
# each answer stood alone (ladislav-urban/OpenExecutive#144), which broke
# follow-ups in the website retake of Nov 3, 2026 ("I'm assuming that's the
# invoice you mean", a supplier from step 1 forgotten by step 5).
FOLLOW_UP_RULE = (
    "The person sees the earlier turns of this conversation in the panel. So "
    "read a follow-up with them: \"that invoice\", \"each supplier\", "
    "\"these invoices\" mean what the earlier questions and answers named, "
    "all of it (every supplier, delivery, set of hours or milestone raised), "
    "never only the last one. Don't ask which one they mean, or write that "
    "you're assuming it, when the conversation already says. Read every figure "
    "the answer gives from ERPNext on this turn rather than repeat it from an "
    "earlier answer, over the same period and filters the earlier answer used "
    "unless they ask for another, and never write that you haven't re-read or "
    "checked something ERPNext can show you; read it. Never narrate a "
    "correction (\"earlier I said\", \"that was wrong\", \"let me state it "
    "plainly\"), and never add a \"Correction\" line, bullet or note: when a "
    "figure differs from one you gave before, give the right one plainly, with "
    "its arithmetic worked out before you write it, never retried in the reply; "
    "what still holds you don't restate. Never say you said something the "
    "earlier turns don't show. "
)
# Both panel blocks carry it: the panel is docked beside the page, so a wide
# table wraps into something unreadable (the website retake of Nov 3, 2026).
TABLE_RULE = (
    "Keep a table to the columns the question needs, four at most, so it fits "
    "the panel; wider detail goes in the place you open beside it. "
)
PANEL_CONTEXT_BLOCK = (
    "This message comes from the \"Ask the Dynocortex\" panel inside ERPNext, "
    "a chat docked on every ERPNext page; people there call you Dynocortex, "
    "and that is you. "
    "Here you can only read ERPNext (its tools run as this person's own ERPNext "
    "user) and consult your specialists; you cannot send, schedule, save or "
    "change anything, or reach any other system. You cannot prepare ERPNext "
    "drafts for this person either, so never offer to: when they want a "
    "document, tell them how to create it in ERPNext themselves. Keep answers "
    "short enough for a side panel. If they ask for something you cannot do "
    "here, say so and suggest the OpenExecutive app. "
    + FOLLOW_UP_RULE
    + TABLE_RULE
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
    + FOLLOW_UP_RULE
    + TABLE_RULE
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


# The web chat route's name for it (F1): a fresh one per host page turn.
TurnViews = _TurnViews

# Set by the route around one turn; the turn's tasks share the holder.
_turn_views: ContextVar[_TurnViews | None] = ContextVar("erpnext_panel_turn_views", default=None)


@contextmanager
def collect_views(views: _TurnViews) -> Iterator[_TurnViews]:
    """Bind ``views`` as the turn's holder for what ``note_erpnext_call``
    keeps, around a web chat turn the docked panel sent (F1). Saved and put
    back rather than reset by token, as ``schedule_tools.set_session`` is, so
    it holds across the stream's task hops."""
    previous = _turn_views.get()
    _turn_views.set(views)
    try:
        yield views
    finally:
        _turn_views.set(previous)


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


def _not_set_up() -> str:
    """The panel's "not set up" message, in the product's name when the
    install has one (OE_PRODUCT_NAME): the person in ERPNext sees one product."""
    from openexecutive.utils.product_name import configured_product_name

    product = configured_product_name()
    if product is None:
        return "The ERPNext panel is not set up on OpenExecutive."
    return f"{product}'s answers are not set up here yet."


def _not_on_roster() -> str:
    """Why someone off the People list gets no answer, likewise."""
    from openexecutive.utils.product_name import configured_product_name

    product = configured_product_name()
    if product is None:
        return (
            "Your ERPNext email is not on OpenExecutive's People page, so the Executive "
            "cannot answer you here. Ask the person who runs OpenExecutive to add you."
        )
    return (
        f"Your account isn't set up for answers in {product} yet. Ask the person "
        f"who runs {product} to add you."
    )


def _in_product_words(block: str) -> str:
    """A panel block naming the product where it names the web app, when the
    install has a name (OE_PRODUCT_NAME): "suggest the Dynocortex app". The
    block itself names ERPNext throughout (its tools do too), so it also says
    what to call it to the person, which the model otherwise echoes."""
    from openexecutive.utils.product_name import configured_product_name

    product = configured_product_name()
    if product is None:
        return block
    return block.replace("OpenExecutive", product).rstrip() + (
        f" The person uses one product, {product}: in your reply call it {product}, "
        "and its records \"the ERP\", never by the names of the parts it is built "
        "from, though this block and the tools use them. "
    )


def in_product_reply(reply: str) -> str:
    """The panel's reply in the product's words, when the install has a name:
    the same backstop the web chat's stream uses (``connected_systems``)."""
    from openexecutive.prompts.connected_systems import in_product_reply as named

    return named(reply)


def context_block(session: Any) -> str:
    """The panel's channel block: with drafts for this person, or without."""
    from openexecutive.integrations.erpnext_drafts import offered

    block = PANEL_DRAFTS_CONTEXT_BLOCK if offered(session) else PANEL_CONTEXT_BLOCK
    return _in_product_words(block)


def _person_for(email: str) -> Any:
    from openexecutive.people.store import find_person_by_email

    person = find_person_by_email(email)
    if person is None or not person.active or person.kind != "team" or person.id is None:
        return None
    return person


async def _answer(person: Any, email: str, conversation_id: str, message: str, page: Any) -> str:
    from openexecutive.audit import log_event as audit_log
    from openexecutive.config import get_settings
    from openexecutive.knowledge.retriever import follow_up_query, retrieve
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
        # A demo person's turn (people.demo): the narrow tools, and the org
        # block's demo variant, with no other demo person and nobody's
        # addresses. The panel names its asker by email only, so the turn
        # can't take it from a bound speaker as the web chat's does. Fails
        # closed, and never turns back off for a cached conversation.
        from openexecutive.people.demo import is_demo_person

        if getattr(person, "demo", False) is True or await asyncio.to_thread(is_demo_person, person.id):
            session.demo_caller = True
        audit_log(
            "integration_inbound",
            f"Inbound erpnext panel from person {person.id}: {message[:160]}",
            actor="erpnext_panel",
            session_id=session_id,
            # For the audit log alone: a panel turn reaches nobody's brief
            # (briefing.live_signals skips the actor and this mark).
            details={
                "channel": "erpnext", "person_id": person.id, "text_len": len(message),
                "in_brief": False,
            },
        )
        # A short follow-up is searched with the question before it; the
        # company's standing rules come whatever the question.
        retrieved_context = await asyncio.to_thread(
            retrieve,
            query=follow_up_query(message, session.conversation_history),
            max_chars=get_settings().knowledge_chat_max_chars,
            standing=True,
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
        return _error(503, "not_configured", _not_set_up())
    if not keys:
        return _error(503, "not_configured", _not_set_up())
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
        return _error(503, "not_configured", _not_set_up())
    person = await asyncio.to_thread(_person_for, email)
    if person is None:
        return _error(
            403,
            "not_on_roster",
            _not_on_roster(),
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
    # A demo person's cap is the day's (people.demo, below), not the hour's: a
    # prospect working through the guides asks many in a row (owner, Oct 29).
    demo_asker = getattr(person, "demo", False) is True
    if not demo_asker and not _rate.allow(email, time.monotonic()):
        return _error(429, "rate_limited", "You have asked a lot this hour; try again later.")
    if getattr(person, "demo", False) is True:
        # A demo person's questions a day count here as in the web chat, theirs
        # and every demo person's together (people.demo).
        from openexecutive.people import demo as people_demo

        if (demo_refusal := await people_demo.admit(person.id)) is not None:
            return _error(429, "daily_limit", demo_refusal)
    _in_flight.add(email)
    shown = _TurnViews()
    views_token = _turn_views.set(shown)
    # The answer check's turn (orchestrator.answer_check), bound here so the
    # reply can say to open the report it read.
    from openexecutive.orchestrator import answer_check

    checked = answer_check.AnswerCheckTurn()
    try:
        with answer_check.collect(checked):
            reply = await _answer(person, email, conversation_id, message.strip(), data.get("page"))
    except TimeoutError:
        return _error(504, "timeout", "That took too long to answer; try a narrower question.")
    except PanelRefused:
        return _error(409, "conversation_busy", "Start a new chat and ask again.")
    except Exception as exc:
        logger.exception("erpnext panel: the turn failed for person %s", person.id)
        from openexecutive.providers.refusal import refusal_reply

        unavailable = refusal_reply(exc)
        if unavailable is not None:
            # Out of credit or over a spend limit: trying again won't help.
            return _error(503, "unavailable", unavailable)
        return _error(500, "failed", "Something went wrong answering that; please try again.")
    finally:
        _turn_views.reset(views_token)
        _in_flight.discard(email)
    # What the reply says is on screen, held to the page or what it opened.
    from openexecutive.integrations.panel_screen import drop_stale_screen

    reply, _stale = drop_stale_screen(reply, data.get("page"), shown.views)
    payload = shown.payload()
    if checked.whole_replaced and shown.call is not None:
        # The reply says to open the report: the panel opens the last read.
        payload["open_call"] = True
    return JSONResponse({"reply": in_product_reply(reply), **payload})
