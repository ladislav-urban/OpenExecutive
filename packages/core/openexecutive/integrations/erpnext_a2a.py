"""``POST /a2a/suppliers`` — OpenExecutive's A2A face for suppliers' agents.

Design "Next", item 3 (oe_connector's ``docs/erpnext-integration.md``,
"Decided" and "Built here"). A supplier's own AI agent asks, over A2A
(Agent2Agent, JSON-RPC ``message/send``), about that supplier's own submitted
Purchase Orders and Purchase Invoices in ERPNext. A narrow agent answers
(``erpnext_a2a_agent``), read-only, with no person approving the reply, and
every exchange is audited. No streaming, no push notifications and no tasks:
each message gets one reply ``Message``.

**The agent card** (``GET /.well-known/agent-card.json``) names only the two
skills, orders and invoices, and how a request is signed. It is served only
while A2A is set up.

**Who is asking.** The operator registers each supplier in
``ERPNEXT_A2A_SUPPLIERS``: a key id mapped to the Supplier's name in ERPNext
and its Ed25519 public key. Every request carries
``x-oe-a2a-assertion: v1.<payload>.<signature>``, as ERPNext's panel does
(``erpnext_panel``): base64url JSON claims ``{aud, body, exp, iat, jti,
kid}`` signed by the supplier's private key over ``v1.<payload>``, bound to
this install (``aud`` is its own endpoint URL, ``ERPNEXT_A2A_URL``, so a
request one customer received can't be replayed at another that registered
the same key), the SHA-256 of this exact body, at most ``MAX_LIFETIME_S``
seconds, and one use. The key is picked by the payload's ``kid`` and only it
is tried. The supplier is the one registered for the key that signed, never
anything in the message. An unknown key, a bad signature or a replay gets
nothing (401) and is audited (``erpnext_a2a_refused``; anyone can send those,
so at most ``REFUSAL_AUDITS_PER_MINUTE`` such rows a minute, the rest only
logged). Each supplier may ask ``RATE_LIMIT`` times an hour, one at a time,
and has its own ledger of used ``jti``s, so one key can't fill another's.
The ledgers, the rate limit and the one-at-a-time set live in this process:
run one API worker, as the image does.

**What answers** is ``erpnext_a2a_agent.answer``, inside
``erpnext_caller.supplier_run(supplier)``: the gateway signs its ERPNext
calls with ``kind: "supplier"`` and ``sub`` the Supplier's name, passes only
the supplier tools there and nothing else, and refuses those tools on every
other turn. oe_connector answers only a Supplier its operator listed in
``oe_connector_a2a_suppliers``, so this install alone cannot open ERPNext to
a supplier. The route answers only once the gateway has found all three
supplier tools matching their pinned digests (``erpnext_tool_pin``).

**Audit.** Every exchange leaves an ``erpnext_a2a_exchange`` row, private to
the principal: the supplier, the key id, the tools called and the lengths of
the message and the reply, never their text.

Requires per-person ERPNext access (``ERPNEXT_CALLER_PRIVATE_KEY``):
oe_connector answers a supplier only as a signed caller.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from openexecutive.api.caller import CallerKeysError, JtiLedger, parse_public_keys
from openexecutive.integrations.erpnext_panel import _b64url, _RateLimiter, _signs
from openexecutive.orchestrator import erpnext_caller

logger = logging.getLogger(__name__)

router = APIRouter()

PATH = "/a2a/suppliers"
CARD_PATH = "/.well-known/agent-card.json"
ASSERTION_HEADER = "x-oe-a2a-assertion"
VERSION = "v1"
MAX_LIFETIME_S = 60
CLOCK_SKEW_S = 30
A2A_PROTOCOL_VERSION = "0.3.0"

MAX_BODY_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 4000
TURN_TIMEOUT_S = 90.0
RATE_LIMIT = 30
RATE_WINDOW_S = 3600.0
REFUSAL_AUDITS_PER_MINUTE = 30

_MAX_ASSERTION_LEN = 4096
_KID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_JTI_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
# The payload's key id, read before the signature only to pick the one key to
# try; the claims are parsed as JSON only once that key has verified.
_KID_CLAIM_RE = re.compile(rb'"kid":"([A-Za-z0-9_-]{1,32})"')
# The card's version: not this server's release.
CARD_VERSION = "1.0"

# JSON-RPC and A2A error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
PUSH_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
# Not an A2A code: this server's own, for "not now".
RATE_LIMITED = -32000

# A2A methods this server knows and does not offer: no streaming, no push
# notifications, and no tasks to look up (each message is answered at once).
_UNSUPPORTED_METHODS = frozenset({
    "message/stream",
    "tasks/resubscribe",
    "tasks/pushNotificationConfig/set",
    "tasks/pushNotificationConfig/get",
    "tasks/pushNotificationConfig/list",
    "tasks/pushNotificationConfig/delete",
    "agent/getAuthenticatedExtendedCard",
})
_TASK_METHODS = frozenset({"tasks/get", "tasks/cancel"})


@dataclass(frozen=True)
class Partner:
    """A supplier the operator registered: its key id, its Supplier name in
    ERPNext, and the public key its agent signs with."""

    key_id: str
    supplier: str
    key: Ed25519PublicKey


class A2ARefused(Exception):
    """The request does not hold; ``reason`` is for the log and the audit."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SuppliersConfigError(ValueError):
    """``ERPNEXT_A2A_SUPPLIERS`` is malformed; the message never quotes a key."""


def parse_suppliers(raw: str) -> dict[str, Partner]:
    """``{"<key id>": {"supplier": "<name>", "key": "<public key>"}}``; empty
    is none, anything malformed raises ``SuppliersConfigError``."""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise SuppliersConfigError("ERPNEXT_A2A_SUPPLIERS is not JSON") from None
    if not isinstance(data, dict) or not data:
        raise SuppliersConfigError("ERPNEXT_A2A_SUPPLIERS must map key ids to suppliers")
    partners: dict[str, Partner] = {}
    seen_keys: set[str] = set()
    for kid, entry in data.items():
        if not isinstance(kid, str) or not _KID_RE.fullmatch(kid):
            raise SuppliersConfigError("each key id is 1-32 letters, digits, - or _")
        if not isinstance(entry, dict) or set(entry) != {"supplier", "key"}:
            raise SuppliersConfigError(f"key {kid} needs exactly supplier and key")
        supplier, key = entry["supplier"], entry["key"]
        if not erpnext_caller.valid_supplier(supplier):
            raise SuppliersConfigError(f"key {kid}: supplier must be the Supplier's name in ERPNext")
        if not isinstance(key, str):
            raise SuppliersConfigError(f"key {kid} is not an Ed25519 public key")
        if key.strip().rstrip("=") in seen_keys:
            # Pasted into a second entry by mistake, it would answer the first.
            raise SuppliersConfigError(f"key {kid} repeats another entry's public key")
        seen_keys.add(key.strip().rstrip("="))
        try:
            parsed = parse_public_keys(f"{kid}:{key.strip()}")
        except CallerKeysError:
            raise SuppliersConfigError(f"key {kid} is not an Ed25519 public key") from None
        partners[kid] = Partner(kid, supplier, parsed[kid])
    return partners


def suppliers() -> dict[str, Partner]:
    """``ERPNEXT_A2A_SUPPLIERS``; raises ``SuppliersConfigError`` when malformed."""
    from openexecutive.config import get_settings

    return parse_suppliers(get_settings().erpnext_a2a_suppliers or "")


_LOOPBACK_URL_RE = re.compile(r"http://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?/\S*")


def endpoint_url() -> str:
    """``ERPNEXT_A2A_URL``: this install's public URL of ``POST /a2a/suppliers``,
    the assertion's audience and the card's ``url``. "" when unset or not
    HTTPS (plain HTTP only on this machine), which keeps A2A off."""
    from openexecutive.config import get_settings

    url = (get_settings().erpnext_a2a_url or "").strip()
    if not url.endswith(PATH) or any(c.isspace() for c in url):
        return ""
    if url.startswith("https://") or _LOOPBACK_URL_RE.fullmatch(url):
        return url
    return ""


def _seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise A2ARefused("malformed")
    return value


def verify_supplier_assertion(
    token: str,
    *,
    partners: dict[str, Partner],
    body: bytes,
    now: int,
    ledgers: dict[str, JtiLedger],
    audience: str,
) -> Partner:
    """The registered supplier whose key signed ``token`` for this one
    request ``body`` to ``audience`` (this install's endpoint URL); raises
    ``A2ARefused``. Its ``jti`` is spent, in that supplier's own ledger, only
    when every other check passes."""
    if not token or len(token) > _MAX_ASSERTION_LEN or not token.isascii():
        raise A2ARefused("malformed")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise A2ARefused("malformed")
    try:
        payload, signature = _b64url(parts[1]), _b64url(parts[2])
    except ValueError:
        raise A2ARefused("malformed") from None
    # The signature first, so no unsigned byte reaches the JSON parser; the
    # key id only picks the one key to try.
    named = _KID_CLAIM_RE.search(payload)
    signed_by = partners.get(named.group(1).decode("ascii")) if named else None
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    if signed_by is None or not _signs(signed_by.key, signature, signing_input):
        raise A2ARefused("signature")
    try:
        claims = json.loads(payload)
    except (ValueError, RecursionError):
        raise A2ARefused("malformed") from None
    if not isinstance(claims, dict) or claims.get("kid") != signed_by.key_id:
        raise A2ARefused("key_id")
    iat, exp = _seconds(claims.get("iat")), _seconds(claims.get("exp"))
    if not audience or claims.get("aud") != audience:
        raise A2ARefused("audience")
    if not 0 < exp - iat <= MAX_LIFETIME_S:
        raise A2ARefused("lifetime")
    if iat > now + CLOCK_SKEW_S:
        raise A2ARefused("not_yet_valid")
    if now > exp:
        raise A2ARefused("expired")
    digest = claims.get("body")
    if (
        not isinstance(digest, str)
        or not _DIGEST_RE.fullmatch(digest)
        or digest != hashlib.sha256(body).hexdigest()
    ):
        raise A2ARefused("body")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not _JTI_RE.fullmatch(jti):
        raise A2ARefused("malformed")
    try:
        ledgers.setdefault(signed_by.supplier, JtiLedger(limit=10_000)).claim(jti, exp, now)
    except Exception:
        raise A2ARefused("replayed") from None
    return signed_by


# The jtis each supplier has used, by supplier.
_ledgers: dict[str, JtiLedger] = {}
_rate = _RateLimiter(RATE_LIMIT, RATE_WINDOW_S)
# Refusals before a supplier is known are audited, but anyone can send one:
# at most this many rows a minute, the rest only logged. A known supplier's
# are bounded by its own rate limit, so they are always audited.
_refusal_audits = _RateLimiter(REFUSAL_AUDITS_PER_MINUTE, 60.0)
# One question in flight per supplier.
_in_flight: set[str] = set()


def _audit_refusal(reason: str, partner: Partner | None = None) -> None:
    logger.warning(
        "erpnext a2a refused reason=%s%s", reason, f" supplier={partner.supplier!r}" if partner else ""
    )
    if partner is None and not _refusal_audits.allow("refused", time.monotonic()):
        return
    from openexecutive.audit import log_event as audit_log

    details: dict[str, Any] = {"reason": reason}
    if partner is not None:
        details.update(supplier=partner.supplier, key_id=partner.key_id)
    audit_log(
        "erpnext_a2a_refused",
        f"Refused a supplier agent's A2A request ({reason})",
        actor="erpnext_a2a",
        details=details,
        private=True,
    )


def _ready() -> str | None:
    """Why A2A cannot answer on this install, else None."""
    from openexecutive.orchestrator.mcp_gateway import gateway_server_names, get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_SERVER

    if not erpnext_caller.signing_setting():
        return "ERPNEXT_CALLER_PRIVATE_KEY is not set (signed ERPNext callers)"
    if not endpoint_url():
        return "ERPNEXT_A2A_URL is not this install's https URL of /a2a/suppliers"
    if ERPNEXT_SERVER not in gateway_server_names(get_active_gateway()):
        return "the erpnext MCP server is not configured"
    return None


async def _tools_ready() -> bool:
    """Whether the gateway found all three supplier tools matching their pins."""
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_SUPPLIER_TOOLS

    gateway = get_active_gateway()
    return gateway is not None and await gateway.verified_erpnext_tools(sorted(ERPNEXT_SUPPLIER_TOOLS))


async def _answer(partner: Partner, message: str) -> Any:
    """The narrow agent's answer, its ERPNext calls signed as this supplier."""
    from openexecutive.integrations.erpnext_a2a_agent import answer
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway

    gateway = get_active_gateway()
    if gateway is None:
        raise RuntimeError("the MCP gateway is not running")

    async def call(name: str, arguments: dict[str, Any]) -> str:
        return await gateway.call_tool({"name": name, "arguments": arguments})

    from openexecutive.audit import cost_caps

    # A supplier's agent is off People: its calls share the A2A bucket, held to
    # the person cap, and a refusal tells it only that it can't answer.
    with erpnext_caller.supplier_run(partner.supplier), cost_caps.bind_spender(f"{cost_caps.OUTSIDE_PREFIX}a2a"):
        return await asyncio.wait_for(answer(partner.supplier, message, call), timeout=TURN_TIMEOUT_S)


def agent_card(rpc_url: str) -> dict[str, Any]:
    """The A2A agent card: the two skills and how a request is signed."""
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": "Supplier desk",
        "description": (
            "Answers a registered supplier's own agent about that supplier's submitted purchase "
            "orders and purchase invoices: status, dates, quantities and amounts. Read-only; it "
            "changes, confirms or promises nothing. Every request must be signed with the key the "
            "company registered for the supplier."
        ),
        "url": rpc_url,
        "preferredTransport": "JSONRPC",
        "version": CARD_VERSION,
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {
            "supplierSignature": {
                "type": "apiKey",
                "in": "header",
                "name": ASSERTION_HEADER,
                "description": (
                    "v1.<payload>.<signature>: base64url JSON claims {aud, body, exp, iat, jti, kid} "
                    "with aud this card's url, body the SHA-256 (hex) of the exact request body, "
                    f"exp - iat at most {MAX_LIFETIME_S} seconds and a jti used once, signed with "
                    "the supplier's registered Ed25519 key over v1.<payload>."
                ),
            }
        },
        "security": [{"supplierSignature": []}],
        "skills": [
            {
                "id": "purchase_orders",
                "name": "Your purchase orders",
                "description": (
                    "Your submitted purchase orders from the company, open ones or all: dates, status, "
                    "totals, what is received and billed, and one order's lines."
                ),
                "tags": ["purchase-order", "delivery"],
                "examples": [
                    "Which of your purchase orders to us are still open?",
                    "What is still to be received on PUR-ORD-2026-00012?",
                ],
            },
            {
                "id": "invoices",
                "name": "Your invoices",
                "description": (
                    "Your submitted invoices to the company: bill numbers, due dates, totals and what "
                    "is still unpaid."
                ),
                "tags": ["invoice", "payment"],
                "examples": ["Which of our invoices are unpaid, and when are they due?"],
            },
        ],
    }


@router.get(CARD_PATH)
async def erpnext_a2a_agent_card() -> JSONResponse:
    try:
        configured = bool(suppliers())
    except SuppliersConfigError:
        configured = False
    if not configured or _ready() is not None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(agent_card(endpoint_url()))


def _rpc_error(request_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}, status_code=status
    )


def _message_text(params: Any) -> tuple[str | None, str | None, int, str]:
    """``(text, context id, 0, "")`` from ``message/send`` params, or
    ``(None, None, code, message)`` for the JSON-RPC error to answer."""
    if not isinstance(params, dict) or not isinstance(params.get("message"), dict):
        return None, None, INVALID_PARAMS, "params.message is required"
    config = params.get("configuration")
    if isinstance(config, dict) and config.get("pushNotificationConfig") is not None:
        return None, None, PUSH_NOT_SUPPORTED, "Push notifications are not supported"
    message = params["message"]
    if message.get("role") != "user" or not isinstance(message.get("parts"), list):
        return None, None, INVALID_PARAMS, "message needs role user and parts"
    if not isinstance(message.get("messageId"), str) or not _ID_RE.fullmatch(message["messageId"]):
        return None, None, INVALID_PARAMS, "message.messageId is required"
    if message.get("taskId") is not None:
        return None, None, TASK_NOT_FOUND, "This agent keeps no tasks"
    context_id = message.get("contextId")
    if context_id is not None and (not isinstance(context_id, str) or not _ID_RE.fullmatch(context_id)):
        return None, None, INVALID_PARAMS, "message.contextId is not valid"
    texts: list[str] = []
    for part in message["parts"]:
        if not isinstance(part, dict) or part.get("kind") != "text":
            return None, None, CONTENT_TYPE_NOT_SUPPORTED, "Only text parts are supported"
        if not isinstance(part.get("text"), str):
            return None, None, INVALID_PARAMS, "a text part needs text"
        texts.append(part["text"])
    text = "\n".join(texts).strip()
    if not text or len(text) > MAX_MESSAGE_CHARS:
        return None, None, INVALID_PARAMS, f"Send a question of up to {MAX_MESSAGE_CHARS} characters"
    return text, context_id or str(uuid.uuid4()), 0, ""


async def _read_capped(request: Request) -> bytes | None:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


@router.post(PATH)
async def erpnext_a2a_rpc(request: Request) -> JSONResponse:
    try:
        partners = suppliers()
    except SuppliersConfigError as exc:
        logger.error("%s; refusing A2A", exc)
        partners = {}
    audience = endpoint_url()
    if not partners or not audience:
        return _rpc_error(None, INTERNAL_ERROR, "A2A is not set up here.", 503)
    tokens = request.headers.getlist(ASSERTION_HEADER)
    try:
        # Before reading the body: an unsigned request costs nothing more.
        if len(tokens) != 1:
            raise A2ARefused("missing" if not tokens else "duplicate_header")
        body = await _read_capped(request)
        if body is None:
            raise A2ARefused("too_large")
        partner = verify_supplier_assertion(
            tokens[0].strip(), partners=partners, body=body, now=int(time.time()),
            ledgers=_ledgers, audience=audience,
        )
    except A2ARefused as refused:
        _audit_refusal(refused.reason)
        return _rpc_error(None, INVALID_REQUEST, "This request is not signed by a registered supplier.", 401)
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return _rpc_error(None, PARSE_ERROR, "The request is not JSON.")
    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or not isinstance(data.get("method"), str):
        return _rpc_error(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 request.")
    request_id = data.get("id")
    if not (request_id is None or isinstance(request_id, (str, int)) and not isinstance(request_id, bool)):
        return _rpc_error(None, INVALID_REQUEST, "The id must be a string or a number.")
    method = data["method"]
    if method in _UNSUPPORTED_METHODS:
        return _rpc_error(request_id, UNSUPPORTED_OPERATION, "Streaming and push notifications are not supported.")
    if method in _TASK_METHODS:
        return _rpc_error(request_id, TASK_NOT_FOUND, "This agent keeps no tasks.")
    if method != "message/send":
        return _rpc_error(request_id, METHOD_NOT_FOUND, "Method not found.")
    text, context_id, code, problem = _message_text(data.get("params"))
    if text is None or context_id is None:
        return _rpc_error(request_id, code, problem)
    unready = _ready()
    if unready is not None:
        logger.warning("erpnext a2a unavailable: %s", unready)
        return _rpc_error(request_id, INTERNAL_ERROR, "A2A is not set up here.", 503)
    supplier = partner.supplier
    if supplier in _in_flight:
        _audit_refusal("busy", partner)
        return _rpc_error(request_id, RATE_LIMITED, "Your last question is still being answered.", 429)
    if not _rate.allow(supplier, time.monotonic()):
        _audit_refusal("rate_limited", partner)
        return _rpc_error(request_id, RATE_LIMITED, "Too many questions this hour; try again later.", 429)
    _in_flight.add(supplier)
    try:
        if not await _tools_ready():
            _audit_refusal("tools_unverified", partner)
            return _rpc_error(request_id, INTERNAL_ERROR, "This agent cannot answer right now.", 503)
        try:
            result = await _answer(partner, text)
        except TimeoutError:
            _audit_refusal("timeout", partner)
            return _rpc_error(request_id, INTERNAL_ERROR, "That took too long to answer; ask again.", 504)
        except Exception as exc:
            logger.exception("erpnext a2a: the turn failed for supplier %r", supplier)
            from openexecutive.providers.refusal import refusal_reply

            unavailable = refusal_reply(exc)
            _audit_refusal("unavailable" if unavailable is not None else "failed", partner)
            if unavailable is not None:
                # Out of credit or over a spend limit: asking again won't help.
                return _rpc_error(request_id, INTERNAL_ERROR, unavailable, 503)
            return _rpc_error(request_id, INTERNAL_ERROR, "Something went wrong answering that.", 500)
    finally:
        _in_flight.discard(supplier)
    from openexecutive.audit import log_event as audit_log

    audit_log(
        "erpnext_a2a_exchange",
        f"Answered supplier {supplier}'s agent over A2A ({len(result.tools)} ERPNext call(s))",
        actor="erpnext_a2a",
        details={
            "supplier": supplier,
            "key_id": partner.key_id,
            "tools": result.tools,
            "message_len": len(text),
            "reply_len": len(result.reply),
        },
        private=True,
    )
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "kind": "message",
            "role": "agent",
            "messageId": str(uuid.uuid4()),
            "contextId": context_id,
            "parts": [{"kind": "text", "text": result.reply}],
        },
    })
