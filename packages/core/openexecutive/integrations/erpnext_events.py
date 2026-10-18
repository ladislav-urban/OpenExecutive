"""``POST /webhook/erpnext/events`` — alerts ERPNext raises on its own.

ERPNext phase 3 (design: oe_connector's ``docs/erpnext-integration.md``,
"ERPNext pushes events"). oe_connector checks its business rules (an
overdue receivable, stock below its reorder level on a top-selling item, a
purchase invoice far above the supplier's usual size) and posts what it
finds here; each event goes through the alerts pipeline's triage like any
inbound message.

**Who is speaking.** The site itself, with the key it signs the panel with
(``oe_connector_panel_private_key``; ``ERPNEXT_PANEL_PUBLIC_KEYS`` here):
the same ``v1.<payload>.<signature>`` assertion over the exact body, but for
``AUDIENCE`` and naming no one (``sub`` is ""), so a panel assertion is no
good here and this one is no good there.

**Who sees it.** oe_connector computes the events as its read-only service
user, whose view is wider than most people's in ERPNext, so every alert is
private to the principal (``AlertEvent.private``): on their board only,
never broadcast or pushed to anyone else, as ERPNext's figures are in chat
without per-person access. Each event names its rule and its document, and
``dedup_hint`` keeps one open alert per rule and document.

The event text is ERPNext's data, partly written by outsiders (a customer's
name), and reaches triage as the body of an inbound event, as an email's
does. Fields are length-capped and the kinds are a fixed list.

**A suggested draft** (phase 4b): a stock or overdue-receivable event may
carry ``suggest``, the draft ERPNext's own fields make for it. It never
reaches triage: ``erpnext_alert_drafts.remember`` keeps a well-formed one by
the alert's ref, for the principal's "Prepare" button.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from openexecutive.api.caller import CallerKeysError, JtiLedger
from openexecutive.integrations import erpnext_panel
from openexecutive.integrations.erpnext_panel import (
    PanelRefused,
    panel_keys,
    verify_panel_assertion,
)

logger = logging.getLogger(__name__)

router = APIRouter()

PATH = "/webhook/erpnext/events"
ASSERTION_HEADER = erpnext_panel.ASSERTION_HEADER
# Kept in step with oe_connector/alerts.py.
AUDIENCE = "openexecutive-erpnext-events"

# The rules oe_connector checks, and what each is about, for triage.
KINDS: dict[str, str] = {
    "overdue_receivable": "an overdue customer invoice",
    "stock_below_reorder": "stock below its reorder level on a top-selling item",
    "large_purchase_invoice": "a purchase invoice far above the supplier's usual size",
    "budget_exceeded": "a budget line whose actual this fiscal year is above its budget",
    "weekly_margin_drop": "last week's gross margin well below the weeks before it",
}
# The alerts pipeline triages 60 events a minute from every source together
# (pipeline._rate_limited) and drops the rest. ERPNext gets a third of that,
# and a 429 when it is full, so oe_connector retries rather than losing them
# and other sources keep room.
MAX_EVENTS = 20
EVENTS_PER_MINUTE = 20
EVENTS_PER_HOUR = 300
MAX_BODY_BYTES = 128 * 1024

# A document name, or item@warehouse.
_KEY_RE = re.compile(r"[\w .,:/()&'+@-]{1,140}")
_MAX_TITLE = 200
_MAX_TEXT = 1500

_ledger = JtiLedger()
_recent: deque[float] = deque()


def _room(count: int, now: float) -> bool:
    """Whether ``count`` more events fit this minute and this hour; if so,
    they are counted."""
    while _recent and now - _recent[0] > 3600:
        _recent.popleft()
    last_minute = sum(1 for t in _recent if now - t <= 60)
    if last_minute + count > EVENTS_PER_MINUTE or len(_recent) + count > EVENTS_PER_HOUR:
        return False
    _recent.extend([now] * count)
    return True


def _text(value: Any, cap: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:cap]


def parse_events(data: Any) -> list[dict[str, Any]]:
    """The well-formed events in ``data``; raises ``ValueError`` when it is
    not ``{"events": [...]}`` or holds too many. A single malformed event is
    skipped, not the batch."""
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("expected {\"events\": [...]}")
    if len(data["events"]) > MAX_EVENTS:
        raise ValueError(f"at most {MAX_EVENTS} events per request")
    events: list[dict[str, Any]] = []
    for raw in data["events"]:
        if not isinstance(raw, dict):
            continue
        kind, key = raw.get("kind"), raw.get("key")
        title, text = _text(raw.get("title"), _MAX_TITLE), _text(raw.get("text"), _MAX_TEXT)
        if kind not in KINDS or not isinstance(key, str) or not _KEY_RE.fullmatch(key) or not title:
            continue
        # The draft it suggests, if any: kept as sent, checked by
        # erpnext_alert_drafts.remember before it is stored.
        events.append({"kind": kind, "key": key, "title": title, "text": text, "suggest": raw.get("suggest")})
    return events


def _schedule(event: dict[str, Any]) -> None:
    from openexecutive.alerts.models import AlertEvent
    from openexecutive.alerts.pipeline import schedule_evaluation
    from openexecutive.orchestrator.content_trust import wrap_untrusted

    ref = f"erpnext:{event['kind']}:{event['key']}"
    # The text names customers and suppliers in their own words (a web-form
    # sign-up names itself), so it reaches triage as untrusted content.
    details = wrap_untrusted(event["text"] or event["title"], source="erpnext", author="erpnext_alert")
    schedule_evaluation(
        AlertEvent(
            source="erpnext",
            # One a month per rule and document (oe_connector resends at most
            # monthly), so a document can raise again after its alert is
            # dismissed; dedup_hint still keeps one open alert per document.
            external_id=f"{ref}:{time.strftime('%Y-%m')}",
            subject=event["title"],
            body=(
                f"ERPNext's rule for {KINDS[event['kind']]} matched. These are ERPNext's own "
                f"figures, checked by its read-only service user.\n\n{details}"
            ),
            user="ERPNext",
            dedup_hint=ref,
            # Computed as the service user: the principal's alone.
            private=True,
        )
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


@router.post(PATH)
async def erpnext_events(request: Request) -> JSONResponse:
    try:
        keys = panel_keys()
    except CallerKeysError:
        logger.error("ERPNEXT_PANEL_PUBLIC_KEYS is malformed; refusing ERPNext events")
        return _error(503, "not_configured", "ERPNext events are not set up on OpenExecutive.")
    if not keys:
        return _error(503, "not_configured", "ERPNext events are not set up on OpenExecutive.")
    body = await _read_capped(request)
    if body is None:
        return _error(413, "too_large", "Too many events in one request.")
    tokens = request.headers.getlist(ASSERTION_HEADER)
    try:
        if len(tokens) != 1:
            raise PanelRefused("missing" if not tokens else "duplicate_header")
        verify_panel_assertion(
            tokens[0].strip(), keys=keys, body=body, now=int(time.time()), ledger=_ledger, audience=AUDIENCE
        )
    except PanelRefused as refused:
        logger.warning("erpnext events refused reason=%s", refused.reason)
        return _error(401, "unauthorized", "This request was not signed by the ERPNext site.")
    try:
        events = parse_events(json.loads(body))
    except (ValueError, RecursionError) as bad:
        return _error(400, "bad_request", str(bad)[:200])
    if not _room(len(events), time.monotonic()):
        return _error(
            429,
            "rate_limited",
            f"At most {EVENTS_PER_MINUTE} events a minute and {EVENTS_PER_HOUR} an hour; send the rest later.",
        )
    from openexecutive.audit import log_event as audit_log

    for event in events:
        audit_log(
            "integration_inbound",
            f"Inbound erpnext event {event['kind']}: {event['title'][:160]}",
            actor="erpnext_events",
            details={"channel": "erpnext", "kind": event["kind"], "key": event["key"]},
            private=True,
        )
        if event["suggest"] is not None:
            from openexecutive.integrations.erpnext_alert_drafts import remember

            remember(event["kind"], event["key"], event["suggest"])
        _schedule(event)
    return JSONResponse({"accepted": len(events)})
