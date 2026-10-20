"""Drafts an ERPNext alert suggests: a "Prepare" button on the alert's card.

ERPNext phase 4b (design: oe_connector's ``docs/erpnext-phase4-plan.md``,
Decisions #5). oe_connector sends two of its alerts with a ``suggest``
object, built from ERPNext's own fields and signed with the event: a stock
alert the Material Request ERPNext's own reorder would raise, an overdue
receivable the invoice to follow up. Nothing is checked or written when an
alert arrives, nobody being there to see it.

**On arrival** (``remember``, from ``integrations.erpnext_events`` once the
site's signature checked out): a suggestion of the right shape, naming the
same item and warehouse, or invoice, as the event's key, is kept by the
event's ref (``erpnext:<kind>:<key>``). A newer one for the same ref
replaces it. The alert text plays no part.

**Which alert it belongs to** (``suggestion_for``): only the alert that
event became, as ``erpnext_events`` raises it: private to the principal,
source ``erpnext``, ``dedup_key`` exactly ``private:<ref>`` (the pipeline's
``_make_private``) and ``external_id`` ``<ref>:<YYYY-MM>``. The model's
``create_alert`` may not use the ``erpnext`` source
(``alert_tools.RESERVED_SOURCES``), so no other alert can carry those.

**The button** (``GET /erpnext/alert-drafts``): the principal's open ERPNext
alerts that have one. ERPNext alerts are the principal's alone, as the checks
run with the service user's wider view, so nobody else is shown any.

**The tap** (``prepare``, ``POST /erpnext/alert-drafts/{alert_id}``): the
principal, known to the API as them (``verified.caller_refusal``: signed
sign-ins or local login). The draft is built from the stored suggestion only:
a restock as ERPNext sent it, a follow-up ToDo worded here from the invoice's
figures and assigned to the principal (the customer's name, theirs to write,
stays out). ERPNext checks it as the principal (``draft_check``, inside
``erpnext_caller.draft_check_as``), and the usual draft card is left
(``erpnext_drafts.propose``), to approve like any other.

**Once per alert** (``_taken``): the card carries the alert's id
(``from_alert``). While it waits, or once its draft is made, the alert offers
no button, and a tap answers with the waiting card or says the draft is
already in ERPNext. Dismissed, expired or failed, the alert offers it again.
oe_connector's next month's alert for the same document is a new alert.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.integrations import erpnext_drafts as drafts
from openexecutive.memory import episodic

logger = logging.getLogger(__name__)

# The alert kinds oe_connector suggests a draft for, and what each makes.
SUGGESTING: dict[str, str] = {
    "stock_below_reorder": "Material Request",
    "overdue_receivable": "ToDo",
}
LABELS: dict[str, str] = {"Material Request": "Prepare restock", "ToDo": "Prepare follow-up"}
# A suggestion older than this is dropped on the next write: oe_connector
# resends a still-matching alert monthly.
KEEP_FOR = timedelta(days=62)
AMOUNT_MAX = 1_000_000_000_000

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS erpnext_alert_suggestions (
    ref         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    suggestion  TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""


def _db_path(db_path: Path | None) -> Path:
    # Read at call time so a test's patch of episodic.DB_PATH takes effect.
    return db_path if db_path is not None else episodic.DB_PATH


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(db_path))
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# On arrival
# ---------------------------------------------------------------------------

def parse_suggestion(kind: str, key: str, raw: Any) -> dict[str, Any] | None:
    """``raw`` as it will be kept, or None when it isn't one this kind may
    suggest, isn't well formed, or names another document than ``key``."""
    if SUGGESTING.get(kind) is None or not isinstance(raw, dict) or raw.get("doctype") != SUGGESTING[kind]:
        return None
    if kind == "stock_below_reorder":
        raw_fields = raw.get("fields")
        if not isinstance(raw_fields, dict):
            return None
        try:
            fields = drafts._material_request(raw_fields)
        except ValueError:
            return None
        items = fields["items"]
        if len(items) != 1 or f"{items[0]['item_code']}@{items[0]['warehouse']}" != key:
            return None
        return {"doctype": "Material Request", "fields": fields}
    invoice, amount = raw.get("reference_name"), raw.get("outstanding_amount")
    currency, due = raw.get("currency"), raw.get("due_date")
    if (
        raw.get("reference_type") != "Sales Invoice"
        or not isinstance(invoice, str) or invoice != key or not drafts._DOC_NAME_RE.fullmatch(invoice)
        or isinstance(amount, bool) or not isinstance(amount, int | float) or not 0 < amount <= AMOUNT_MAX
        or not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency)
        or not isinstance(due, str) or not _DATE_RE.fullmatch(due)
    ):
        return None
    try:
        datetime.strptime(due, "%Y-%m-%d")
    except ValueError:
        return None
    return {
        "doctype": "ToDo", "reference_type": "Sales Invoice", "reference_name": invoice,
        "outstanding_amount": float(amount), "currency": currency, "due_date": due,
    }


def remember(kind: str, key: str, raw: Any, *, db_path: Path | None = None) -> bool:
    """Keep the suggestion an event carried, by its alert's ref; whether one
    was kept. Never raises: an alert is taken whatever happens here."""
    suggestion = parse_suggestion(kind, key, raw)
    if suggestion is None:
        return False
    now = datetime.now(UTC)
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO erpnext_alert_suggestions (ref, kind, suggestion, received_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ref) DO UPDATE SET kind = excluded.kind, suggestion = excluded.suggestion, "
                "received_at = excluded.received_at",
                (f"erpnext:{kind}:{key}", kind, json.dumps(suggestion), now.isoformat()),
            )
            conn.execute(
                "DELETE FROM erpnext_alert_suggestions WHERE received_at < ?", ((now - KEEP_FOR).isoformat(),)
            )
    except sqlite3.Error:
        logger.warning("erpnext_alert_drafts: couldn't keep a suggestion", exc_info=True)
        return False
    return True


def _ref_of(alert: Any) -> tuple[str, str, str] | None:
    """``(ref, kind, key)`` of the event ``alert`` was raised from, when it
    is an ERPNext event's alert as ``erpnext_events`` raises it; else None."""
    from openexecutive.alerts.models import is_private_alert

    dedup_key = getattr(alert, "dedup_key", "") or ""
    if getattr(alert, "source", None) != "erpnext" or not is_private_alert(alert):
        return None
    for kind in SUGGESTING:
        prefix = f"private:erpnext:{kind}:"
        if dedup_key.startswith(prefix) and len(dedup_key) > len(prefix):
            ref = dedup_key[len("private:"):]
            if re.fullmatch(re.escape(ref) + r":\d{4}-\d{2}", getattr(alert, "external_id", "") or ""):
                return ref, kind, dedup_key[len(prefix):]
    return None


def suggestion_for(alert: Any, *, db_path: Path | None = None) -> dict[str, Any] | None:
    """What ``alert`` suggests, read back and checked again, or None."""
    found = _ref_of(alert)
    if found is None:
        return None
    ref, kind, key = found
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT kind, suggestion FROM erpnext_alert_suggestions WHERE ref = ?", (ref,)
            ).fetchone()
    except sqlite3.Error:
        logger.warning("erpnext_alert_drafts: couldn't read a suggestion", exc_info=True)
        return None
    if row is None or row[0] != kind:
        return None
    try:
        parsed = json.loads(row[1])
    except ValueError:
        return None
    # Kept in the shape oe_connector sends, so it is checked again as sent.
    return parse_suggestion(kind, key, parsed)


# ---------------------------------------------------------------------------
# The draft it makes
# ---------------------------------------------------------------------------

def draft_for(suggestion: dict[str, Any], principal_email: str) -> tuple[str, dict[str, Any], str]:
    """The DocType, the fields as ``propose_erpnext_draft`` would take them,
    and the card's one line, for ``suggestion``."""
    if suggestion["doctype"] == "ToDo":
        invoice = suggestion["reference_name"]
        description = (
            f"Follow up overdue invoice {invoice}: {suggestion['outstanding_amount']:,.2f} {suggestion['currency']} "
            f"outstanding, due {suggestion['due_date']}."
        )
        fields = {
            "description": description,
            "allocated_to": principal_email,
            "priority": "High",
            "reference_type": "Sales Invoice",
            "reference_name": invoice,
        }
        return "ToDo", fields, f"Follow up overdue invoice {invoice} (ERPNext alert)"
    fields = suggestion["fields"]
    line = fields["items"][0]
    return "Material Request", fields, f"Restock {line['item_code']} in {line['warehouse']} (ERPNext alert)"


async def prepare(alert: Any, *, caller: Any) -> tuple[int, bool]:
    """Leave the principal the draft card ``alert`` suggests; its decision id
    and whether it is new. Raises ``erpnext_drafts.ApproveRefused``."""
    from openexecutive.delegation.verified import NOT_YOURS, caller_refusal
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.people.store import find_principal_person

    refused = drafts.ApproveRefused
    if not drafts.drafts_on():
        raise refused(409, "drafts_off", "ERPNext drafts are turned off here, or ERPNext isn't connected.")
    person = find_principal_person()
    email = (getattr(person, "email", None) or "").strip().lower()
    if person is None or person.id is None or "@" not in email:
        raise refused(409, "no_email", "Your People entry has no email, so ERPNext can't tell who you are.")
    why_not = caller_refusal(caller, email)
    if why_not == NOT_YOURS:
        raise refused(403, "not_yours", "Only the account owner can prepare the drafts ERPNext alerts suggest.")
    if why_not is not None:
        raise refused(
            409, "caller_signing_required",
            "Preparing a draft needs signed sign-ins on this server, so that nobody else can do it for you.",
        )
    suggestion = suggestion_for(alert)
    if suggestion is None:
        raise refused(404, "no_suggestion", "This alert has no draft to prepare.")
    try:
        _already(person.id, alert)
    except _Waiting as waiting_card:
        return waiting_card.decision_id, False
    doctype, raw_fields, why = draft_for(suggestion, email)
    fields = drafts.check_fields(doctype, raw_fields)
    if isinstance(fields, str):
        raise refused(409, "not_offered", f"ERPNext {doctype} drafts aren't offered here: {fields}")
    _room_for(person.id, doctype, fields)
    check_args = {"doctype": doctype, "fields": fields}
    try:
        with erpnext_caller.draft_check_as(check_args, email):
            raw = await drafts._call(erpnext_caller.DRAFT_CHECK_TOOL, check_args)
    except Exception:
        logger.warning("erpnext_alert_drafts: draft_check failed", exc_info=True)
        raise refused(502, "check_failed", "ERPNext didn't answer the check. Try again in a moment.") from None
    checked = drafts._result(raw)
    if checked.get("ok") is not True:
        raise refused(502, "not_accepted", f"ERPNext did not accept this draft: {drafts._refusal_text(raw)}")
    view = drafts.card_view(doctype, checked.get("summary"), fields)
    # Read again after the check, with no await before the card is stored,
    # so two taps at once leave one card, never pass OPEN_CARDS_MAX, and the
    # alert never gets a second.
    try:
        _already(person.id, alert)
    except _Waiting as waiting_card:
        return waiting_card.decision_id, False
    waiting = _room_for(person.id, doctype, fields)
    decision_id, new, _ = drafts.propose(
        person, doctype, fields, view, why, asked_by=email, session_id=None, waiting=waiting,
        from_alert=getattr(alert, "id", None), fingerprint=drafts.fingerprint_of(checked),
    )
    if not new:
        _tag(decision_id, getattr(alert, "id", None))
    if new:
        drafts.audit("erpnext_draft_proposed", f"Left person {person.id} an ERPNext {doctype} draft card from an alert", {
            "person_id": person.id, "decision_id": decision_id, "doctype": doctype, "alert_id": getattr(alert, "id", None),
        })
    return decision_id, new


# A card that still counts for its alert: waiting, being made, or made.
_TAKEN = ("proposed", "executing", "approved_unchanged", "approved_with_edit", "executed")


class _Waiting(Exception):
    """The alert's card is waiting: answer with it."""

    def __init__(self, decision_id: int) -> None:
        super().__init__(decision_id)
        self.decision_id = decision_id


def _taken(person_id: int) -> dict[int, Any]:
    """``person_id``'s cards that still count, by the alert they came from:
    a card tagged ``from_alert``, or one from before cards were tagged that
    its ``erpnext_draft_proposed`` audit row ties to an alert. One query (and
    one for the audit rows), after ``open_cards`` has closed any waiting card
    past its time."""
    from types import SimpleNamespace

    from openexecutive.memory.decision_ledger import _db_path as ledger_path
    from openexecutive.memory.episodic import _get_conn

    drafts.open_cards(person_id)
    legacy: dict[int, int] = {}
    statuses = ",".join("?" * len(_TAKEN))
    with _get_conn(ledger_path()) as conn:
        try:
            for decision_id, alert_id in conn.execute(
                "SELECT json_extract(details_json, '$.decision_id'), json_extract(details_json, '$.alert_id') "
                "FROM audit_log WHERE event_type = 'erpnext_draft_proposed' "
                "AND json_extract(details_json, '$.alert_id') IS NOT NULL"
            ):
                if isinstance(decision_id, int) and isinstance(alert_id, int):
                    legacy[decision_id] = alert_id
        except sqlite3.Error:
            logger.warning("erpnext_alert_drafts: couldn't read the proposal audit rows", exc_info=True)
        rows = conn.execute(
            "SELECT id, status, final_payload_json, json_extract(proposed_payload_json, '$.from_alert') "
            f"FROM decision_instances WHERE decision_class = ? AND approver_person_id = ? AND status IN ({statuses}) "
            "ORDER BY id",
            (drafts.DECISION_CLASS, person_id, *_TAKEN),
        ).fetchall()
    out: dict[int, Any] = {}
    for card_id, status, final_json, tagged in rows:
        alert_id = tagged if isinstance(tagged, int) else legacy.get(card_id)
        if isinstance(alert_id, int):
            out.setdefault(alert_id, SimpleNamespace(id=card_id, status=status, final_payload_json=final_json))
    return out


def _already(person_id: int, alert: Any) -> None:
    """Raise ``_Waiting`` when ``alert``'s card waits, ``ApproveRefused``
    when its draft was made; nothing when it may have one."""
    alert_id = getattr(alert, "id", None)
    card = _taken(person_id).get(alert_id) if isinstance(alert_id, int) else None
    if card is None:
        return
    if card.status in ("proposed", "executing"):
        raise _Waiting(int(card.id))
    try:
        made = json.loads(card.final_payload_json or "{}").get("created") or {}
    except ValueError:
        made = {}
    name = made.get("name") if isinstance(made, dict) else None
    raise drafts.ApproveRefused(
        409, "already_prepared",
        f"This alert's draft is already in ERPNext{f' ({name})' if isinstance(name, str) else ''}. "
        "Review it there.",
    )


def _tag(decision_id: int, alert_id: Any) -> None:
    """The same draft already waited on a card no alert was tied to (the
    model proposed those exact fields, or an earlier alert for the same
    document): tie it to this alert too, so the alert offers no second."""
    from openexecutive.memory.decision_ledger import get_decision_instance, update_open_payload

    card = get_decision_instance(decision_id)
    payload = drafts._payload(card) if card is not None else {}
    if card is None or not isinstance(alert_id, int) or "from_alert" in payload:
        return
    if not update_open_payload(decision_id, {**payload, "from_alert": alert_id}, expected_json=card.proposed_payload_json):
        logger.info("erpnext_alert_drafts: card %d moved on before it was tied to alert %d", decision_id, alert_id)


def _room_for(person_id: int, doctype: str, fields: dict[str, Any]) -> list[Any]:
    """``person_id``'s waiting cards; raises when they are full and none of
    them is this same draft."""
    waiting = drafts.open_cards(person_id)
    digest = drafts._digest(person_id, doctype, fields)
    if len(waiting) >= drafts.OPEN_CARDS_MAX and not any(drafts._payload(c).get("digest") == digest for c in waiting):
        raise drafts.ApproveRefused(
            409, "too_many", f"You already have {drafts.OPEN_CARDS_MAX} draft cards waiting. Clear some first.",
        )
    return waiting


def open_suggestions(*, limit: int = 200) -> list[tuple[int, str]]:
    """``(alert_id, label)`` for each open ERPNext alert with a suggestion
    and no card that still counts (``_taken``)."""
    from openexecutive.alerts import store
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    taken = _taken(principal.id) if principal is not None and principal.id is not None else {}
    out: list[tuple[int, str]] = []
    for alert in store.list_alerts(limit=limit):
        if alert.id is None or alert.source != "erpnext" or alert.status not in ("unread", "read") or alert.id in taken:
            continue
        suggestion = suggestion_for(alert)
        if suggestion is not None:
            out.append((alert.id, LABELS[suggestion["doctype"]]))
    return out
