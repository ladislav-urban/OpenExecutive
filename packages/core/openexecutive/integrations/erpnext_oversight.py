"""One oversight record per ERPNext draft: the evidence that a person
reviewed and approved each one.

oe_connector's plan item 6 (``docs/erpnext-integration.md``, "Next"). Each
ERPNext draft OpenExecutive proposes is an ``erpnext_draft`` approval card
(``integrations.erpnext_drafts``). This module turns those cards into
records an auditor can read, one per card:

- who asked for it, where (chat, ERPNext's panel or an alert) and in which
  conversation, when, and why;
- what the card showed (``view``) and the fingerprint of the draft it
  showed;
- who approved or dismissed it, and when, and what came of it (made,
  dismissed, expired, replaced because ERPNext's reading changed, not made);
- the ERPNext document it became, and its seal in oe_connector's hash-chained
  "OE Draft" log (``row_hash``, ``chain_index``).

``with_erpnext_state`` adds what became of each draft in ERPNext since:
still a draft, submitted, cancelled or deleted, and when and by whom it was
last changed. It asks oe_connector's ``audit_drafts`` inside
``erpnext_caller.oversight_run``, signed as the service caller, the only way
the gateway lets that tool through. It also says whether the log still
holds the seal the card recorded.

A card is its person's alone (``approver_only``), the principal's
included. So a record of someone else's card (a teammate's, or one asked for
in ERPNext's panel) keeps who asked, when, who decided, what came of it, the
document and its seal, and leaves out what the card said: ``why``, ``card``,
``fields``, ``fingerprint`` (a digest of the card's content, so a guess could
be checked against it), ``conversation`` and ``last_error`` are empty and ``private`` is
true. Those were read as that person in ERPNext, from their own conversation.

Served by ``GET /erpnext/drafts/oversight`` (``api/routes/erpnext_drafts.py``)
as JSON or CSV, to the principal alone.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# What a record's outcome says, from the card's status (and why it closed).
_OUTCOMES = {
    "proposed": "waiting for approval",
    "executing": "being made",
    "approved_unchanged": "made",
    "approved_with_edit": "made",
    "executed": "made",
    "rejected": "dismissed",
}
# What a record of someone else's card leaves out (``records``).
_PRIVATE: dict[str, Any] = {
    "why": None, "card": {}, "fields": {}, "fingerprint": None, "conversation": None, "from_alert": None,
    "last_error": None,
}
# One audit call answers at most this many cards (oe_connector's MAX_KEYS).
_AUDIT_BATCH = 500

CSV_COLUMNS = (
    "decision_id",
    "doctype",
    "proposed_at",
    "asked_by",
    "asked_in",
    "conversation",
    "why",
    "card",
    "fingerprint",
    "outcome",
    "resolved_at",
    "resolved_by",
    "erpnext_document",
    "erpnext_url",
    "row_hash",
    "chain_index",
    "erpnext_state",
    "erpnext_modified",
    "erpnext_modified_by",
    "seal_in_log",
    "last_error",
    "private",
)


def _json(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _outcome(card: Any, final: dict[str, Any]) -> str:
    from openexecutive.integrations.erpnext_drafts import CHANGED_SINCE_PROPOSED

    if card.status == "closed_externally":
        return {"expired": "expired", "changed": "replaced: ERPNext's reading changed"}.get(
            card.reversal_reason or "", "closed"
        )
    if card.status == "failed":
        reason = str(final.get("last_error") or "")
        return "not made: changed since proposed" if CHANGED_SINCE_PROPOSED in reason else "not made: failed"
    return _OUTCOMES.get(card.status, card.status)


def _who(person_id: int | None) -> str | None:
    if person_id is None:
        return None
    from openexecutive.people.store import get_person

    person = get_person(person_id)
    if person is None:
        return f"person {person_id}"
    return f"{person.full_name} <{person.email}>" if person.email else person.full_name


def _card_text(view: Any) -> str:
    """What the card showed, as one line: its fields, lines and notes."""
    if not isinstance(view, dict):
        return ""
    parts = [f"{f.get('label')}: {f.get('value')}" for f in view.get("fields") or [] if isinstance(f, dict)]
    for line in view.get("lines") or []:
        if isinstance(line, dict):
            parts.append(" ".join(str(line.get(k, "")) for k in ("item_code", "qty", "warehouse", "rate", "amount")).strip())
    parts.extend(str(n) for n in view.get("notes") or [])
    return " | ".join(p for p in parts if p)


def _in_range(stamp: str, since: datetime | None, until: datetime | None) -> bool:
    try:
        at = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return since is None and until is None
    if at.tzinfo is None and (since or until):
        at = at.replace(tzinfo=(since or until).tzinfo)  # type: ignore[union-attr]
    return (since is None or at >= since) and (until is None or at < until)


def records(
    *, since: datetime | None = None, until: datetime | None = None, reader: int | None = None,
) -> list[dict[str, Any]]:
    """Every ERPNext draft card proposed in [since, until), oldest first. A
    card whose approver isn't ``reader`` comes without what it said
    (``_PRIVATE``)."""
    from openexecutive.integrations.erpnext_drafts import DECISION_CLASS
    from openexecutive.memory.decision_ledger import list_instances

    out = []
    for card in list_instances(DECISION_CLASS, limit=100000):
        if not _in_range(card.created_at, since, until):
            continue
        payload, final = _json(card.proposed_payload_json), _json(card.final_payload_json)
        made = final.get("created")
        created: dict[str, Any] = made if isinstance(made, dict) else {}
        asked_in = payload.get("asked_in") or ("alert" if payload.get("from_alert") is not None else "chat")
        out.append({
            "decision_id": card.id,
            "card_key": card.idempotency_key,
            "doctype": payload.get("doctype"),
            "proposed_at": card.created_at,
            "asked_by": payload.get("asked_by"),
            "asked_in": asked_in,
            "conversation": card.originating_session_id,
            "from_alert": payload.get("from_alert"),
            "why": payload.get("why"),
            "card": payload.get("view") or {},
            "fields": payload.get("fields") or {},
            "fingerprint": payload.get("fingerprint"),
            "status": card.status,
            "outcome": _outcome(card, final or payload),
            "resolved_at": card.resolved_at,
            "resolved_by": _who(card.resolver_person_id),
            "erpnext_document": (
                {"doctype": created.get("doctype"), "name": created.get("name"), "url": created.get("url")}
                if created.get("name") else None
            ),
            "seal": (
                {"row_hash": created.get("row_hash"), "chain_index": created.get("chain_index")}
                if created.get("row_hash") else None
            ),
            "attempts": int((final or payload).get("attempts") or 0),
            "last_error": (final or payload).get("last_error"),
            "private": False,
        })
        if reader is None or card.approver_person_id != reader:
            out[-1].update(_PRIVATE, private=True)
    return sorted(out, key=lambda r: (str(r["proposed_at"]), r["decision_id"]))


async def _audit(keys: list[str]) -> dict[str, Any]:
    from openexecutive.integrations.erpnext_drafts import _call, _result
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.prompts.connected_systems import ERPNEXT_AUDIT_TOOL

    found: dict[str, Any] = {}
    for start in range(0, len(keys), _AUDIT_BATCH):
        with erpnext_caller.oversight_run():
            raw = await _call(ERPNEXT_AUDIT_TOOL, {"idempotency_keys": keys[start:start + _AUDIT_BATCH]})
        result = _result(raw)
        if result.get("ok") is not True or not isinstance(result.get("drafts"), dict):
            raise RuntimeError("ERPNext did not answer the oversight audit")
        found.update(result["drafts"])
    return found


async def with_erpnext_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows`` with what became of each made draft in ERPNext (``erpnext``),
    and whether the log still holds the card's seal (``seal_in_log``): the
    row hashes again to its stored hash (``seal_intact``), that hash is the
    card's, and the row still names the card's document. Raises when ERPNext
    can't be asked."""
    keys = [r["card_key"] for r in rows if r.get("erpnext_document") and isinstance(r.get("card_key"), str)]
    state = await _audit(keys) if keys else {}
    out = []
    for row in rows:
        got = state.get(str(row.get("card_key"))) if row.get("erpnext_document") else None
        enriched = {**row, "erpnext": None, "seal_in_log": None}
        if isinstance(got, dict):
            enriched["erpnext"] = {
                k: got.get(k)
                for k in ("in_log", "state", "modified", "modified_by", "chain_index", "row_hash", "seal_intact")
            }
            if row.get("seal"):
                document = row["erpnext_document"]
                enriched["seal_in_log"] = (
                    got.get("in_log") is True
                    and got.get("seal_intact") is True
                    and got.get("row_hash") == row["seal"]["row_hash"]
                    and (got.get("doctype"), got.get("name")) == (document.get("doctype"), document.get("name"))
                )
        out.append(enriched)
    return out


def _cell(value: Any) -> Any:
    """A CSV cell a spreadsheet won't run: text from the model or ERPNext that
    starts like a formula (= + - @, a tab or a return) gets a leading quote."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def to_csv(rows: list[dict[str, Any]]) -> str:
    """The records as CSV, one row per draft (``CSV_COLUMNS``), every cell
    safe to open in a spreadsheet (``_cell``)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        doc, seal, erp = r.get("erpnext_document") or {}, r.get("seal") or {}, r.get("erpnext") or {}
        writer.writerow([_cell(value) for value in [
            r.get("decision_id"), r.get("doctype"), r.get("proposed_at"), r.get("asked_by"), r.get("asked_in"),
            r.get("conversation"), r.get("why"), _card_text(r.get("card")), r.get("fingerprint"),
            r.get("outcome"), r.get("resolved_at"), r.get("resolved_by"),
            doc.get("name"), doc.get("url"), seal.get("row_hash"), seal.get("chain_index"),
            erp.get("state"), erp.get("modified"), erp.get("modified_by"),
            "" if r.get("seal_in_log") is None else ("yes" if r["seal_in_log"] else "NO"),
            r.get("last_error"), "yes" if r.get("private") else "",
        ]])
    return buffer.getvalue()
