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
holds the seal the card recorded, and whether the document is still as
OpenExecutive made it (``unchanged``).

``unchanged_share`` is the pilot's value measure (oe_connector's plan item
4): of the drafts in the range now submitted in ERPNext, the share submitted
as OpenExecutive made them. ``unchanged`` compares a digest of the fields a
person can edit, stored when the draft was made, with the document as it is
when asked. So an edit undone before the export counts as unchanged, and a
change made after the submit, to a field that can still be edited then,
counts as changed. It is null for a draft made before the digest existed or
a deleted document, and those are left out of the share. It says nothing of
what changed. Records of other people's cards count too; the summary is
counts alone, never what a card said.

``gate`` measures the approval gate itself (the landscape review's
"Approvals turn into rubber stamps"): per person who decided cards, and over
everyone, how many were decided, approved and dismissed (expired and
replaced cards apart: nobody decided them), the approval rate and the time
to decide approved cards (median and 10th percentile), for the range and
the range of equal length just before it. ``flags`` names a person whose
approvals look like waving through (``_gate_flags``). Time to decide runs
from the card's proposal to when it closed, which for an approved card is
once ERPNext made the draft, a few seconds after the tap. It is when the
card was answered, not how long it was read: someone who decides on their
phone hours later looks slow but may not have read it, and a quick yes to a
card they asked for a minute ago can be fine. A flag is a prompt to look,
not a finding. It needs only who decided and when, which a record of
someone else's card keeps, so it reads no card content.

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
from collections import defaultdict
from datetime import UTC, datetime
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
# A card a person approved: made, or approved and then not made (failed).
# Not ``executed``: nobody tapped it, the class's mode acted.
_APPROVED = frozenset({"approved_unchanged", "approved_with_edit", "failed"})
# The gate's warning (``_gate_flags``). Approving more, faster: on at least
# GATE_MIN_DECIDED cards decided in the range, an approval rate of at least
# GATE_HIGH_APPROVAL_RATE and higher than the range before, with the median
# time to decide at most GATE_TIME_FALL of what it was then.
GATE_MIN_DECIDED = 10
GATE_HIGH_APPROVAL_RATE = 0.95
GATE_TIME_FALL = 0.5
# Fast and never changed (erpnext=1): at least GATE_MIN_SUBMITTED of the
# person's approved drafts now submitted, none changed, and a median time
# to decide under GATE_FAST_SECONDS.
GATE_MIN_SUBMITTED = 10
GATE_FAST_SECONDS = 30.0
GATE_NOTE = (
    "Time to decide runs from when a card was proposed to when it was answered (for an approved card, once "
    "ERPNext made the draft); it is not how long the card was read. Someone who decides on their phone hours "
    "later looks slow but may not have read it, and a quick yes to a card they asked for a minute ago can be "
    "fine. A flag is a prompt to look at how that person approves, not a finding."
)
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
    "unchanged",
    "last_error",
    "private",
    "operation",
    "target",
    "asked_by_person_id",
    "approver",
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
            # create, or update / comment on an existing document (erpnext_changes).
            "operation": payload.get("operation") or "create",
            "target": payload.get("name"),
            "proposed_at": card.created_at,
            "asked_by": payload.get("asked_by"),
            # A credit raise's preparer, by People id, and the person it went
            # to for approval (erpnext_credit); for other cards, its person.
            "asked_by_person_id": payload.get("asked_by_person_id"),
            "approver": _who(card.approver_person_id),
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
                for k in (
                    "in_log", "state", "modified", "modified_by", "chain_index", "row_hash", "seal_intact",
                    "unchanged",
                )
            }
            if not isinstance(got.get("unchanged"), bool):
                enriched["erpnext"]["unchanged"] = None  # only a real answer counts (``unchanged_share``)
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


def unchanged_share(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Of ``rows`` (from ``with_erpnext_state``) now submitted in ERPNext, how
    many are unchanged, changed or unknown (``unchanged`` null), and the share
    unchanged of those with a known answer (null when there are none)."""
    told = [
        (r.get("erpnext") or {}).get("unchanged") for r in rows
        if (r.get("erpnext") or {}).get("state") == "submitted"
    ]
    same, changed = told.count(True), told.count(False)
    return {
        "submitted": len(told),
        "unchanged": same,
        "changed": changed,
        "unknown": len(told) - same - changed,
        "share": round(same / (same + changed), 4) if same + changed else None,
    }


def _seconds(row: dict[str, Any]) -> float | None:
    """From the card's proposal to its answer, in seconds; None without both."""
    try:
        start = datetime.fromisoformat(str(row.get("proposed_at")))
        end = datetime.fromisoformat(str(row.get("resolved_at")))
    except (TypeError, ValueError):
        return None
    start, end = (at if at.tzinfo else at.replace(tzinfo=UTC) for at in (start, end))
    return max((end - start).total_seconds(), 0.0)


def _percentile(values: list[float], share: float) -> float | None:
    """The ``share`` percentile of ``values``, interpolated between the two
    nearest (the median at 0.5); None when there are none."""
    if not values:
        return None
    ordered = sorted(values)
    at = share * (len(ordered) - 1)
    low = int(at)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (at - low), 1)


def _approved(row: dict[str, Any]) -> bool:
    return row.get("status") in _APPROVED


def _decided(row: dict[str, Any]) -> bool:
    return _approved(row) or row.get("status") == "rejected"


def _gate_numbers(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Cards decided, approved and dismissed, the approval rate (null with
    none decided) and the time to decide approved cards, in seconds."""
    approved = [r for r in rows if _approved(r)]
    dismissed = sum(1 for r in rows if r.get("status") == "rejected")
    decided = len(approved) + dismissed
    times = [t for t in (_seconds(r) for r in approved) if t is not None]
    return {
        "decided": decided,
        "approved": len(approved),
        "dismissed": dismissed,
        "approval_rate": round(len(approved) / decided, 4) if decided else None,
        "median_seconds": _percentile(times, 0.5),
        "p10_seconds": _percentile(times, 0.1),
    }


def _by_person(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The decided cards in ``rows`` by who decided them."""
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("resolved_by") and _decided(row):
            found[str(row["resolved_by"])].append(row)
    return found


def _gate_flags(
    person: str, now: dict[str, Any], before: dict[str, Any] | None, rows: list[dict[str, Any]], erpnext: bool,
) -> list[dict[str, Any]]:
    """What in ``person``'s numbers (``now``, ``before``) and cards (``rows``)
    looks like waving cards through."""
    flags = []
    rate, median = now["approval_rate"], now["median_seconds"]
    if (
        before is not None and now["decided"] >= GATE_MIN_DECIDED
        and rate is not None and rate >= GATE_HIGH_APPROVAL_RATE
        and before["approval_rate"] is not None and rate > before["approval_rate"]
        and median is not None and before["median_seconds"] is not None
        and median <= before["median_seconds"] * GATE_TIME_FALL
    ):
        flags.append({
            "resolved_by": person, "flag": "approving_more_faster",
            "why": (
                f"Approved {rate:.0%} of {now['decided']} cards (before: {before['approval_rate']:.0%}), "
                f"deciding in a median {median:g}s (before: {before['median_seconds']:g}s)."
            ),
        })
    if erpnext:
        told = [
            (r.get("erpnext") or {}).get("unchanged") for r in rows
            if _approved(r) and (r.get("erpnext") or {}).get("state") == "submitted"
        ]
        same = told.count(True)
        if same >= GATE_MIN_SUBMITTED and False not in told and median is not None and median < GATE_FAST_SECONDS:
            flags.append({
                "resolved_by": person, "flag": "unchanged_and_fast",
                "why": f"All {same} approved drafts since submitted were unchanged, decided in a median {median:g}s.",
            })
    return flags


def gate(
    rows: list[dict[str, Any]], before: list[dict[str, Any]] | None = None, *, erpnext: bool = False,
) -> dict[str, Any]:
    """The approval gate's numbers over ``rows`` (the range's records) and
    ``before`` (the range of equal length just before it; None when the
    range has no start), over everyone and per person who decided cards,
    and the flags (``_gate_flags``; the unchanged one only for ``rows`` from
    ``with_erpnext_state``, ``erpnext``)."""
    everyone = {
        **_gate_numbers(rows),
        "expired": sum(1 for r in rows if r.get("outcome") == "expired"),
        "replaced": sum(1 for r in rows if str(r.get("outcome") or "").startswith("replaced")),
    }
    earlier = _by_person(before) if before is not None else {}
    people, flags = [], []
    for person, theirs in sorted(_by_person(rows).items()):
        now = _gate_numbers(theirs)
        then = _gate_numbers(earlier.get(person, [])) if before is not None else None
        people.append({"resolved_by": person, "range": now, "before": then})
        flags.extend(_gate_flags(person, now, then, theirs, erpnext))
    return {
        "everyone": {"range": everyone, "before": _gate_numbers(before) if before is not None else None},
        "people": people,
        "flags": flags,
        "thresholds": {
            "min_decided": GATE_MIN_DECIDED, "high_approval_rate": GATE_HIGH_APPROVAL_RATE,
            "time_fall": GATE_TIME_FALL, "min_submitted": GATE_MIN_SUBMITTED, "fast_seconds": GATE_FAST_SECONDS,
        },
        "note": GATE_NOTE,
    }


def _yes(value: Any) -> str:
    return "" if value is None else ("yes" if value else "NO")


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
            _yes(r.get("seal_in_log")), _yes(erp.get("unchanged")),
            r.get("last_error"), "yes" if r.get("private") else "",
            r.get("operation") or "create", r.get("target"),
            r.get("asked_by_person_id"), r.get("approver"),
        ]])
    return buffer.getvalue()
