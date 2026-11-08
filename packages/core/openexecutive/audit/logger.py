"""SQLite-backed audit log writer + reader.

Design notes:
- Stores into the same `episodic_memory.db` SQLite file used by alerts/episodic
  memory so there is one place to look. New table only — no schema migrations
  for existing tables.
- `summary` is the searchable, human-readable column (max ~300 chars). It is
  what users grep for in the UI. `details_json` carries opaque structured
  fields (token counts, durations, channel refs) and is not searched.
- Writes swallow exceptions: an audit failure must never break a chat turn or
  a tool call. We log a warning and move on.
- A row can be private to the principal (`private_to_principal`): every row
  a turn about their private mail writes, and a row on their own turn that
  names one of their contacts (`people_tools.audit_row_private_to_principal`,
  decided at write time — the `audit.context` scopes included, for rows
  written before a turn binds its session), plus any row a caller marks
  `private=True`. The read API (`api.routes.audit`) leaves those rows out
  for anyone else.
- A row can also belong to one team member alone (`private_to_person`):
  what their own mailbox touched under Act as me (`audit.rows_for_person`, or
  a turn that read their mail). It is private and, unlike the principal's,
  left out of every read here unless the caller asks for that person's rows
  (``owned_by``) — the principal included. Only the usage totals count it.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("EPISODIC_DB_PATH") or "./episodic_memory.db")

# Cap summaries so a runaway tool input/output doesn't bloat the index.
_SUMMARY_MAX_LEN = 300

# Cap stored JSON so detail blobs stay reasonable in size.
_DETAILS_MAX_LEN = 4000

# Cap the un-truncated drill-down payload. memory_snapshot can carry whole
# episodic context dumps and knowledge_retrieval can carry many chunk
# previews — without a cap an active user generates GB-scale audit rows.
# 64 KB per row keeps a year of high-volume usage under a couple GB while
# still being large enough to capture realistic per-turn context.
_FULL_MAX_LEN = 64 * 1024

# Recognised event types — kept as a constant for validation and UI filter
# rendering. New types can be added without a migration, but they should be
# listed here so consumers know what to expect.
EVENT_TYPES: tuple[str, ...] = (
    "chat_turn",
    "answer_check",  # a reply's figures held to ERPNext's results that turn (counts only; orchestrator/answer_check.py)
    "answer_check_changes",  # the sentences that check changed, before and after, with each figure and why; private, text kept 30 days
    "erpnext_carried",  # a conversation took in an earlier chat that read ERPNext (a past-chat hit); it then counts as having read it (audit/erpnext_reads.py)
    "specialist_consult",
    "tool_invocation",
    "scheduled_action",
    "alert",
    "integration_inbound",
    "chat_card_sent",  # a card waiting outside any chat was sent to its person's chat app (integrations.channel_cards)
    "auth_login",
    "auth_logout",
    # Instrumentation events — surface what the agent saw on each turn so
    # /audit/session/{id} can render a flow chart with the full context.
    "knowledge_retrieval",  # RAG retrieve() — chunks, scores, source files
    "cache_event",          # per Anthropic-call token usage + cache stats
    "memory_snapshot",      # episodic context + company profile at turn entry
    "committee_review",     # committee-reviewed draft + critiques (pre-existing emit, now declared)
    "peer_memory",          # Honcho per-person memory — prefetch + sync_turn outcomes
    "memory_extraction",    # episodic extractor — proposed / stored / dropped per pass
    "attunement",           # open loops opened / closed / expired, 👍/👎 on replies
    "executive_paused",     # operator paused autonomous work (scheduler/pause.py)
    "executive_resumed",    # operator resumed it; held work released
    "scheduled_action_cancelled",  # a person cancelled a pending action (DELETE /scheduled/{id})
    "workspace_settings_changed",  # solo/team mode, the user's time zone or the principal's role changed (PUT /workspace)
    "decision_class_mode_changed",  # propose ↔ auto_execute for a decision class (PUT /decisions/classes/…)
    # Act as me (delegation/) — every one written private to the principal.
    "delegation_settings_changed",  # a person turned Act as me on or off (PUT /delegation)
    "delegation_gmail_verified",    # a person's own Gmail was checked and found usable when turning it on
    "delegation_voice_changed",     # "How I write" learned, edited, locked, reset or its signature re-read
    "delegation_voice_described",   # a person had their style written from their own words (nothing saved yet)
    "delegation_drafted",           # ghostwrite_email saved a draft in a person's own Gmail
    "delegation_draft_replaced",    # earlier chat drafts deleted: a redraft or the Executive's own send replaced them
    "delegation_inbox_changed",     # a person turned "Draft replies to my inbox" on or off (PUT /delegation/inbox)
    "delegation_inbox_scanned",     # the inbox watcher checked a person's inbox (counts only)
    "delegation_reply_drafted",     # it drafted a reply in their Gmail and left a card for them
    "delegation_reply_dismissed",   # they dismissed a card; details.draft says whether the draft was deleted
    "delegation_reply_closed",      # a card closed because Gmail settled it (sent, deleted, replied, expired)
    "delegation_reply_sent",        # the person tapped Send and their draft went, exactly as it was in Gmail
    "delegation_chat_draft_sent",   # the person tapped Send under a draft chat wrote as them, and it went as written
    "delegation_reminder_set",      # remind_me stored a reminder for the person who asked (no text in the row)
    "delegation_reminder_sent",     # a reminder went to the person who set it, as its stored text
    "delegation_actions_proposed",  # propose_actions left a person a card of actions to approve (kinds only)
    "delegation_actions_approved",  # the person approved an action card; details.outcomes says what each did
    "delegation_actions_dismissed", # the person dismissed an action card
    "delegation_actions_allowed",   # Approve + allow (in training): those actions may happen on their own from now on
    "delegation_actions_handled",   # a card of actions they allowed was carried out on its own (in training)
    "delegation_actions_held",      # a card of allowed actions waited for them after all; details.reason says why
    "delegation_training_changed",  # a person put Act as me settings in or out of training (PUT /delegation/training)
    "delegation_disclosure_changed",  # a person changed the AI line on replies sent as them (PUT /delegation/disclosure)
    "delegation_calendar_changed",  # a person turned Read my calendar on or off (PUT /delegation/calendar, or off with Act as me)
    "delegation_invite_carded",     # an invite from someone they know became a card to accept or decline (delegation.invites)
    "delegation_invite_answered",   # the person tapped Accept or Decline; it answered in their own calendar
    "delegation_invite_handled",    # Handle it for me accepted an invite on its own (no clash, a sender they know)
    "delegation_invite_dismissed",  # the person dismissed an invite card (nothing answered)
    "fact_retired",                 # the principal (or the teammate who recorded it) retired a standing fact from the Pulse page (memory/facts.py)
    "fact_reviewed",                # the principal approved or declined a teammate's proposed standing fact
    "fact_approval_changed",        # the principal turned "needs my approval" on or off for a teammate's standing facts
    "fact_confirmation",            # an emailed standing-fact change held, confirmed, cancelled or refused (integrations/fact_confirmation.py)
    # People (people/store.py) — private to the principal.
    "person_suspended",  # someone was taken out of every access decision, reversibly (details.by: principal | directory)
    "person_restored",   # a suspended person was brought back with everything they had
    "demo_person_archived",  # the demo admin console archived a demo person (integrations/erpnext_demo_people.py)
    "demo_person_archive_refused",  # it asked to archive someone it may not, or a non-principal signed it
    "demo_admin_refused",  # a demo admin console list request signed for someone other than the principal
    "principal_set",     # `openexecutive principal` named the principal (people/principal.py)
    # The directory sync (integrations/directory_sync.py) — private, never the token.
    "directory_sync",              # a run that changed something or stopped: counts, and why it stopped
    "directory_person_created",    # a team Person made for a new member of the group
    "directory_person_updated",    # a managed Person's name or email followed the directory (field names only)
    "directory_person_linked",     # an existing Person linked to their directory identity (details.relinked: re-pointed)
    "directory_person_unlinked",   # the directory no longer manages a Person (openexecutive directory-unlink)
    "directory_person_suspended",  # a managed Person no longer an active member: suspended, web sessions ended
    "directory_person_restored",   # back in the group: restored with everything they had
    "content_flagged",  # the content check flagged something a chat turn read (orchestrator/outside_content.py), with which item it was
    "grounding",  # unattended prose held back / refused / rewritten for naming people or figures not in its inputs
    # ERPNext drafts (integrations/erpnext_drafts.py) — written private to the card's person.
    "erpnext_draft_proposed",   # propose_erpnext_draft left a card; details name the DocType and card, never its fields
    "erpnext_draft_created",    # the person approved a card and ERPNext made the unsubmitted draft (details.name)
    "erpnext_draft_failed",     # an approved card's draft wasn't made; the card waits again, retried with the same key
    "erpnext_draft_dismissed",  # the person dismissed a draft card
    "erpnext_oversight_exported",  # the principal exported the drafts' oversight record (range, format, count); private
    # ERPNext A2A (integrations/erpnext_a2a.py) — private to the principal.
    "erpnext_a2a_exchange",  # a supplier's agent was answered: supplier, key id, tools called, lengths only
    "erpnext_a2a_refused",   # a request was refused: unknown key, bad signature, replay, rate limit (details.reason)
)


@dataclass(slots=True)
class AuditEvent:
    """One row in the audit_log table."""

    id: int
    ts: str
    event_type: str
    session_id: str | None
    turn_id: str | None
    actor: str | None
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    # Un-truncated drill-down payload. Only populated by `get(id)`; list/query
    # paths leave this as None so the scan view stays small over the wire.
    full: dict[str, Any] | None = None
    # Department slug owning this event. Added by the Departments feature
    # (Phase 1) so per-department check-ins can filter audit history.
    department: str | None = None
    # Readable by the principal alone (see the module docstring).
    private: bool = False
    # The team member this row belongs to alone, or None.
    private_to_person: int | None = None


@contextmanager
def _get_conn(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    # 5s busy_timeout + WAL so concurrent writers (FastAPI worker, scheduler
    # task, email poller task, Slack/Telegram webhook threads) don't hit
    # "database is locked". Audit writes swallow exceptions, so silent loss
    # under contention would be undetectable.
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _escape_like(s: str) -> str:
    """Escape LIKE metacharacters so user input can't match wildcards."""
    return s.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    raw_details = row["details_json"] or "{}"
    try:
        details = json.loads(raw_details)
        if not isinstance(details, dict):
            details = {"value": details}
    except (ValueError, TypeError):
        details = {"raw": raw_details}
    # `department` is an additive column on older DBs. Index access raises
    # IndexError if it was never SELECTed; use a guard so old call sites
    # (the LIST query, which still SELECTs explicit columns) keep working.
    try:
        department: str | None = row["department"]
    except (IndexError, KeyError):
        department = None
    try:
        private = bool(row["private_to_principal"])
    except (IndexError, KeyError):
        private = False
    try:
        owner = row["private_to_person"]
    except (IndexError, KeyError):
        owner = None
    return AuditEvent(
        id=int(row["id"]),
        ts=str(row["ts"]),
        event_type=str(row["event_type"]),
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        actor=row["actor"],
        summary=str(row["summary"]),
        details=details,
        department=department,
        private=private or owner is not None,
        private_to_person=int(owner) if owner is not None else None,
    )


def _owner_clause(
    owned_by: int | None, params: list[Any], only_owned: bool = False, all_owners: bool = False
) -> str:
    """Leave out every team member's own rows but ``owned_by``'s — and, when
    ``only_owned``, every row that is not theirs. ``all_owners`` keeps every
    member's rows (the read API shows the principal their actions, redacted)."""
    if all_owners:
        return "1"
    if only_owned:
        if owned_by is None:
            return "0"
        params.append(int(owned_by))
        return "private_to_person = ?"
    if owned_by is None:
        return "private_to_person IS NULL"
    params.append(int(owned_by))
    return "(private_to_person IS NULL OR private_to_person = ?)"


def _mailbox_scope() -> bool:
    """Whether rows written now are someone's own mailbox work (a scope
    ``rows_for_person`` opened, or a turn that read their mail). Fails
    closed: a check that cannot run answers yes."""
    try:
        from openexecutive.audit.context import rows_owner
        from openexecutive.delegation.settings import turn_touched_delegate_mail

        return rows_owner() is not None or turn_touched_delegate_mail()
    except Exception:
        logger.warning("audit.mailbox_scope_check_failed — treated as mailbox work", exc_info=True)
        return True


def _row_owner(private_to_person: int | None) -> int | None:
    """The team member a row written now belongs to alone — see
    ``people_tools.audit_row_owner``. Fails closed: a check that cannot run
    keeps an owner it was given or found rather than show the row to the
    principal."""
    try:
        from openexecutive.orchestrator.people_tools import audit_row_owner

        return audit_row_owner(private_to_person)
    except Exception:
        logger.warning("audit.owner_check_failed — row kept to its owner", exc_info=True)
        if private_to_person is not None:
            return private_to_person
        from openexecutive.audit.context import rows_owner

        return rows_owner()


def _private_to_principal(
    summary: str, details: dict[str, Any] | None, full: dict[str, Any] | None
) -> bool:
    """Whether the row being written is the principal's alone — see
    ``people_tools.audit_row_private_to_principal``. Fails closed: a check
    that cannot run keeps the row to the principal rather than show it to
    everyone."""
    try:
        from openexecutive.orchestrator.people_tools import audit_row_private_to_principal

        return audit_row_private_to_principal(summary, details, full)
    except Exception:
        logger.warning("audit.private_check_failed — row kept to the principal", exc_info=True)
        return True


# Token + cost fields summed by usage_summary(). Tokens are integer counts;
# cost is fractional USD. Kept together so totals/by_day/by_model stay aligned.
_USAGE_INT_FIELDS: tuple[str, ...] = (
    "calls",
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "web_search_requests",
)


# The audit rows of one run_script each: chat's ``kind: script`` row and a
# workflow step's ``tool: run_script`` row.
_SCRIPT_ROWS = (
    "((event_type = 'tool_invocation' AND json_extract(details_json,'$.kind') = 'script')"
    " OR (event_type = 'workflow_tool_call' AND json_extract(details_json,'$.tool') = 'run_script'))"
)


def _zero_scripts() -> dict[str, int]:
    return {"scripts": 0, "ok": 0, "calls": 0, "turns_avoided": 0, "duration_ms": 0, "in_workflows": 0}


_PYTHON_JOB_FIELDS = (
    "jobs", "ok", "saved_runs", "attachments", "duration_ms", "cpu_ms", "peak_mb_max",
    "bytes_in", "bytes_out", "turns", "input_tokens", "output_tokens",
    "cache_read_input_tokens",
)


def _zero_python_jobs() -> dict[str, float | int]:
    return {**{k: 0 for k in _PYTHON_JOB_FIELDS}, "cost_usd": 0.0}


# The ERPNext work `erpnext_usage()` counts, in the order a turn is claimed:
# a turn that left a draft card is a draft, a turn of ERPNext's panel
# (session `erpnext:<person>:<conversation>`) a panel question, any other
# turn that called an `erpnext__*` tool a question. The morning brief and
# the triage of ERPNext's alerts run unattended, each under a turn id of its
# own (`brief-…`, `erpnext-alert:…`).
ERPNEXT_USAGE_KINDS: tuple[str, ...] = (
    "draft", "panel_question", "question", "morning_brief", "alert_triage",
)

# `{scope}` is the window and, for a caller who may not read rows private to
# the principal, that filter too: a private row (an `erpnext_draft_proposed`
# row always is) must not say what a turn was.
_ERPNEXT_TURNS = """
    SELECT turn_id, 0 AS rank FROM audit_log
     WHERE event_type = 'erpnext_draft_proposed' AND turn_id IS NOT NULL {scope}
    UNION ALL
    SELECT turn_id, 1 FROM audit_log
     WHERE event_type = 'cache_event' AND session_id LIKE 'erpnext:%' AND turn_id IS NOT NULL {scope}
    UNION ALL
    SELECT turn_id, 2 FROM audit_log
     WHERE event_type = 'tool_invocation' AND turn_id IS NOT NULL
       AND json_extract(details_json,'$.kind') = 'mcp'
       AND json_extract(details_json,'$.tool') LIKE 'erpnext\\_\\_%' ESCAPE '\\' {scope}
    UNION ALL
    SELECT turn_id, 3 FROM audit_log
     WHERE event_type = 'cache_event' AND turn_id LIKE 'brief-%' {scope}
    UNION ALL
    SELECT turn_id, 4 FROM audit_log
     WHERE event_type = 'cache_event' AND turn_id LIKE 'erpnext-alert:%' {scope}
"""

_TOKEN_FIELDS = (
    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens",
)


def _nearest_rank(values: list[float], share: float) -> float:
    """The nearest-rank percentile of ``values`` (non-empty, sorted)."""
    index = max(int(-(-share * len(values) // 1)) - 1, 0)
    return values[min(index, len(values) - 1)]


def _row_cost(details: dict[str, Any], table: Any) -> tuple[float | None, bool]:
    """A ``cache_event`` row's cost and whether it was estimated on read: a
    row written before rows recorded their cost is priced from its tokens
    now, at today's table."""
    cost = details.get("cost_usd")
    if cost is not None:
        try:
            value = float(cost)
        except (TypeError, ValueError, OverflowError):
            return None, False
        # "nan" or "inf" would turn the kind's every figure into NaN.
        return (value, False) if math.isfinite(value) else (None, False)
    if details.get("cost_source"):
        return None, False  # recorded as unpriced
    from openexecutive.audit.pricing import cost_from_tokens

    estimate = cost_from_tokens(str(details.get("model") or ""), details, table=table)
    return estimate, estimate is not None


def _kind_summary(kind: str, turns: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "kind": kind, "count": len(turns), "calls": 0,
        **dict.fromkeys(_TOKEN_FIELDS, 0), "web_search_requests": 0,
        "cost_usd": 0.0, "estimated_calls": 0, "unpriced_calls": 0,
    }
    for turn in turns:
        for key in ("calls", *_TOKEN_FIELDS, "web_search_requests",
                    "estimated_calls", "unpriced_calls"):
            totals[key] += turn[key]
        totals["cost_usd"] += turn["cost_usd"]
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    tokens = sorted(float(t["tokens"]) for t in turns)
    costs = sorted(float(t["cost_usd"]) for t in turns)
    totals["tokens"] = {
        "median": _nearest_rank(tokens, 0.5) if tokens else 0,
        "p90": _nearest_rank(tokens, 0.9) if tokens else 0,
    }
    totals["cost_per"] = {
        "median": round(_nearest_rank(costs, 0.5), 6) if costs else 0.0,
        "p90": round(_nearest_rank(costs, 0.9), 6) if costs else 0.0,
    }
    return totals


def _zero_usage() -> dict[str, float | int]:
    return {**{k: 0 for k in _USAGE_INT_FIELDS}, "cost_usd": 0.0}


def _row_to_usage(row: sqlite3.Row) -> dict[str, float | int]:
    # On an empty match the totals query still returns one row (COUNT(*)=0) but
    # every SUM(...) column is SQL NULL — the `or 0` / `or 0.0` coalescing is
    # what zeroes those out, so this stays correct with no caller-side None.
    out: dict[str, float | int] = {k: int(row[k] or 0) for k in _USAGE_INT_FIELDS}
    out["cost_usd"] = float(row["cost_usd"] or 0.0)
    return out


# SUM expression shared by every usage_summary() query. Token fields cast to
# INTEGER and cost to REAL; COALESCE(..., 0) tolerates NULL/garbled JSON the
# same way _compute_cost_summary's _as_int does in the per-session path.
_USAGE_SUM_COLS = """
    COUNT(*) AS calls,
    SUM(COALESCE(CAST(json_extract(details_json,'$.input_tokens') AS INTEGER),0)) AS input_tokens,
    SUM(COALESCE(CAST(json_extract(details_json,'$.cache_read_input_tokens') AS INTEGER),0)) AS cache_read_input_tokens,
    SUM(COALESCE(CAST(json_extract(details_json,'$.cache_creation_input_tokens') AS INTEGER),0)) AS cache_creation_input_tokens,
    SUM(COALESCE(CAST(json_extract(details_json,'$.output_tokens') AS INTEGER),0)) AS output_tokens,
    SUM(COALESCE(CAST(json_extract(details_json,'$.web_search_requests') AS INTEGER),0)) AS web_search_requests,
    SUM(COALESCE(CAST(json_extract(details_json,'$.cost_usd') AS REAL),0)) AS cost_usd
"""


class AuditLogger:
    """Synchronous SQLite writer/reader for audit events.

    Safe to call from async code: SQLite writes here are sub-millisecond and
    don't justify the complexity of a thread/queue. If a write fails, we log
    and return — auditing never blocks or breaks the caller.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self.initialize_db()

    def initialize_db(self) -> None:
        with _get_conn(self._db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    actor TEXT,
                    summary TEXT NOT NULL,
                    details_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
                CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type);
            """)
            # Additive migrations. PRAGMA-guarded so a second boot is a no-op
            # (no migration tooling in this repo). Concurrent workers can race
            # the ALTER; SQLite raises OperationalError("duplicate column name")
            # on the loser — treat as success.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(audit_log)")}
            for column, ddl in (
                ("full_json", "ALTER TABLE audit_log ADD COLUMN full_json TEXT"),
                # Department tag — added by the Departments feature (Phase 1).
                ("department", "ALTER TABLE audit_log ADD COLUMN department TEXT"),
                # 1 = readable by the principal alone (see the module docstring).
                (
                    "private_to_principal",
                    "ALTER TABLE audit_log ADD COLUMN private_to_principal "
                    "INTEGER NOT NULL DEFAULT 0",
                ),
                # The team member the row belongs to alone (see the module
                # docstring); NULL for every other row.
                (
                    "private_to_person",
                    "ALTER TABLE audit_log ADD COLUMN private_to_person INTEGER",
                ),
            ):
                if column in cols:
                    continue
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

    def log(
        self,
        event_type: str,
        summary: str,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
        full: dict[str, Any] | None = None,
        department: str | None = None,
        private: bool = False,
        private_to_person: int | None = None,
        principal_only: bool = False,
    ) -> int | None:
        """Insert one audit row. Returns row id, or None on failure.

        `full` is an un-truncated drill-down payload (entire user messages,
        full tool inputs/results, full specialist queries). It is never
        returned by the list/query path — only by `get(id)`.

        `department` tags the row with the owning department slug so
        per-department check-ins can filter audit history by `department=`.

        `private=True` marks the row the principal's alone. Without it the
        row is still private when the turn writing it is (see
        `_private_to_principal`) — a caller can add privacy, never remove it.

        `private_to_person` names the team member the row belongs to alone;
        without it the row is still theirs when it is written for their own
        mailbox (see `_row_owner`). It is ignored for the principal, whose
        private rows stay as above.
        """
        try:
            # A turn another AI app asked to act (``audit.context.via_app``):
            # every row says which app asked.
            from openexecutive.audit.context import rows_via

            via = rows_via()
            if via and not (details and "via" in details):
                details = {**(details or {}), "via": via}
            # ``principal_only``: the principal's alone, whoever's turn wrote
            # it, never a team member's own row (the answer check's record of
            # what it changed: the owner, Nov 8, 2026).
            # Never over someone's own mailbox work, which is theirs alone,
            # the principal included: the usual rule then (fails closed).
            if principal_only and _mailbox_scope():
                principal_only = False
            owner = None if principal_only else _row_owner(private_to_person)
            is_private = principal_only or (
                bool(private) or owner is not None or _private_to_principal(summary, details, full)
            )
            safe_summary = _truncate(str(summary), _SUMMARY_MAX_LEN)
            details_json: str | None = None
            if details:
                try:
                    details_json = json.dumps(details, default=str)
                except (TypeError, ValueError):
                    details_json = json.dumps({"repr": repr(details)})
                if len(details_json) > _DETAILS_MAX_LEN:
                    details_json = json.dumps({
                        "truncated": True,
                        "preview": _truncate(details_json, _DETAILS_MAX_LEN),
                    })
            full_json: str | None = None
            if full:
                try:
                    full_json = json.dumps(full, default=str)
                except Exception:
                    # `full` is opaque structured data from tool results /
                    # MCP responses — it can be cyclic, contain objects whose
                    # repr() itself raises, or trigger RecursionError. None
                    # of those should ever break the calling chat turn.
                    try:
                        full_json = json.dumps({"repr": repr(full)})
                    except Exception:
                        full_json = json.dumps({"unserializable": True})
                if full_json is not None and len(full_json) > _FULL_MAX_LEN:
                    # Preserve a usable head + flag truncation so the UI can
                    # render the drawer without choking on a multi-MB blob.
                    full_json = json.dumps({
                        "truncated": True,
                        "original_size": len(full_json),
                        "preview": full_json[: _FULL_MAX_LEN - 200],
                    })
            ts_value = _now()
            with _get_conn(self._db_path) as conn:
                cur = conn.execute(
                    """
                    INSERT INTO audit_log
                        (ts, event_type, session_id, turn_id, actor, summary, details_json,
                         full_json, department, private_to_principal, private_to_person)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts_value,
                        event_type,
                        session_id,
                        turn_id,
                        actor,
                        safe_summary,
                        details_json,
                        full_json,
                        department,
                        1 if is_private else 0,
                        owner,
                    ),
                )
                row_id = int(cur.lastrowid or 0)
            return row_id
        except Exception:
            logger.warning("audit.log_failed event_type=%s", event_type, exc_info=True)
            return None

    def drop_full(self, event_type: str, older_than: datetime) -> int:
        """Clear the ``full`` payload of ``event_type`` rows written before
        ``older_than``: the row and its counts stay, its text goes (a row
        whose text is kept for a limited time). Returns how many changed;
        never raises."""
        try:
            with _get_conn(self._db_path) as conn:
                cur = conn.execute(
                    "UPDATE audit_log SET full_json = NULL"
                    " WHERE event_type = ? AND ts < ? AND full_json IS NOT NULL",
                    (event_type, older_than.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
                )
                return int(cur.rowcount or 0)
        except Exception:
            logger.warning("audit.drop_full_failed event_type=%s", event_type, exc_info=True)
            return 0

    def get(self, event_id: int) -> AuditEvent | None:
        """Fetch a single row by id, including the un-truncated `full` payload."""
        if not self._db_path.exists():
            return None
        try:
            with _get_conn(self._db_path) as conn:
                row = conn.execute(
                    "SELECT id, ts, event_type, session_id, turn_id, actor, summary, "
                    "details_json, full_json, department, private_to_principal, "
                    "private_to_person FROM audit_log WHERE id = ?",
                    (event_id,),
                ).fetchone()
        except Exception:
            logger.warning("audit.get_failed id=%s", event_id, exc_info=True)
            return None
        if row is None:
            return None
        event = _row_to_event(row)
        raw_full = row["full_json"]
        if raw_full:
            try:
                parsed = json.loads(raw_full)
                event.full = parsed if isinstance(parsed, dict) else {"value": parsed}
            except (ValueError, TypeError):
                event.full = {"raw": raw_full}
        return event

    def query(
        self,
        *,
        event_type: str | None = None,
        session_id: str | None = None,
        actor: str | None = None,
        q: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
        department: str | None = None,
        include_private: bool = True,
        owned_by: int | None = None,
        only_owned: bool = False,
        all_owners: bool = False,
    ) -> list[AuditEvent]:
        """Rows matching every filter given, newest first. ``include_private``
        False leaves out the rows private to the principal — filtered in SQL,
        so a page is still full and ``count`` agrees with it. A team member's
        own rows are left out unless ``owned_by`` is them; ``only_owned``
        keeps ``owned_by``'s own rows alone (none with no ``owned_by``)."""
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)

        clauses: list[str] = []
        params: list[Any] = []
        clauses.append(_owner_clause(owned_by, params, only_owned, all_owners))
        if not include_private:
            clauses.append("private_to_principal = 0")
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if department:
            # Filtered in SQL, not over the newest page: department rows are a
            # sliver of the log, so a Python filter over `limit` rows misses
            # them on a busy day (the department check-in's skip rule).
            clauses.append("department = ?")
            params.append(department)
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        if q:
            clauses.append(r"summary LIKE ? ESCAPE '\'")
            params.append(f"%{_escape_like(q)}%")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, ts, event_type, session_id, turn_id, actor, summary, details_json, "
            f"department, private_to_principal, private_to_person FROM audit_log {where} "
            "ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        if not self._db_path.exists():
            return []
        with _get_conn(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def count(
        self,
        *,
        event_type: str | None = None,
        session_id: str | None = None,
        actor: str | None = None,
        q: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_private: bool = True,
        owned_by: int | None = None,
        only_owned: bool = False,
        all_owners: bool = False,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        clauses.append(_owner_clause(owned_by, params, only_owned, all_owners))
        if not include_private:
            clauses.append("private_to_principal = 0")
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        if q:
            clauses.append(r"summary LIKE ? ESCAPE '\'")
            params.append(f"%{_escape_like(q)}%")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        if not self._db_path.exists():
            return 0
        with _get_conn(self._db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM audit_log {where}", params
            ).fetchone()
        return int(row["n"]) if row else 0

    def _script_summary(self, where: str, params: list[Any]) -> dict[str, int]:
        """Totals over the run_script rows (chat ``kind: script`` and workflow
        ``tool: run_script``) a window holds. ``turns_avoided`` is an upper
        bound: each call past a script's first would otherwise have needed a
        model turn, unless the model had batched it with others."""
        calls = "COALESCE(CAST(json_extract(details_json,'$.calls') AS INTEGER),0)"
        with _get_conn(self._db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS scripts, "
                "SUM(CASE WHEN COALESCE(json_extract(details_json,'$.ok'),"
                " json_extract(details_json,'$.outcome') = 'ok') THEN 1 ELSE 0 END) AS ok, "
                f"COALESCE(SUM({calls}),0) AS calls, "
                f"COALESCE(SUM(MAX({calls} - 1, 0)),0) AS turns_avoided, "
                "COALESCE(SUM(CAST(json_extract(details_json,'$.duration_ms') AS INTEGER)),0)"
                " AS duration_ms, "
                "SUM(CASE WHEN event_type = 'workflow_tool_call' THEN 1 ELSE 0 END) AS in_workflows "
                f"FROM audit_log {where}",
                params,
            ).fetchone()
        return {k: int(row[k] or 0) for k in _zero_scripts()}

    def _python_job_summary(self, where: str, params: list[Any]) -> dict[str, float | int]:
        """Totals over the ``python_job`` rows a window holds, plus the model
        usage of the turns that ran one (the whole turn: writing the job's
        code, reading its result and anything else that turn did)."""

        def num(field: str) -> str:
            return f"COALESCE(CAST(json_extract(details_json,'$.{field}') AS INTEGER),0)"

        jobs_where = where.replace("event_type = 'cache_event'", "event_type = 'python_job'")
        with _get_conn(self._db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS jobs, "
                "SUM(CASE WHEN json_extract(details_json,'$.ok') THEN 1 ELSE 0 END) AS ok, "
                "SUM(CASE WHEN json_extract(details_json,'$.saved_tool') IS NOT NULL"
                " THEN 1 ELSE 0 END) AS saved_runs, "
                f"SUM(CASE WHEN {num('attachments')} > 0 THEN 1 ELSE 0 END) AS attachments, "
                f"COALESCE(SUM({num('duration_ms')}),0) AS duration_ms, "
                f"COALESCE(SUM({num('cpu_ms')}),0) AS cpu_ms, "
                f"COALESCE(MAX({num('peak_mb')}),0) AS peak_mb_max, "
                f"COALESCE(SUM({num('bytes_in')}),0) AS bytes_in, "
                f"COALESCE(SUM({num('bytes_out')}),0) AS bytes_out "
                f"FROM audit_log {jobs_where}",
                params,
            ).fetchone()
            turns = conn.execute(
                f"SELECT COUNT(DISTINCT turn_id) AS turns, {_USAGE_SUM_COLS} FROM audit_log "
                f"{where} AND turn_id IN (SELECT turn_id FROM audit_log {jobs_where}"
                " AND turn_id IS NOT NULL)",
                params + params,
            ).fetchone()
        totals: dict[str, float | int] = {k: int(row[k] or 0) for k in _PYTHON_JOB_FIELDS[:9]}
        model = _row_to_usage(turns)
        totals["turns"] = int(turns["turns"] or 0)
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
            totals[k] = int(model[k])
        totals["cost_usd"] = float(model["cost_usd"])
        return totals

    def usage_summary(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        include_private: bool = True,
    ) -> dict[str, Any]:
        """Aggregate token usage + cost from `cache_event` rows over an optional
        time window. Grouping is done in SQL (`json_extract`) so this scales past
        the 1000-row cap on `query()` — a busy DB can have far more cache_event
        rows than that. Returns totals plus by-day, by-model and by-source
        breakdowns (source = the row's `actor`: executive, specialist_research,
        research_synthesis, triage, …; rows without one group as `unknown`).

        Each `cache_event` row carries the per-call token breakdown and (for
        OpenRouter calls) the actual `cost_usd`, or for Anthropic calls their
        tokens at the dated model price file (`audit.pricing`); missing/garbled fields coalesce
        to 0, and rows that predate cost capture contribute 0 cost. `since`/
        `until` bound the ISO `ts` column with the same string comparison used by
        `query()`/`count()`, and ``include_private`` False leaves out the rows
        private to the principal, as there.
        """
        empty: dict[str, Any] = {
            "totals": _zero_usage(), "by_day": [], "by_model": [], "by_source": [],
            "scripts": _zero_scripts(), "python_jobs": _zero_python_jobs(),
        }
        if not self._db_path.exists():
            return empty

        clauses = ["event_type = 'cache_event'"]
        params: list[Any] = []
        if not include_private:
            clauses.append("private_to_principal = 0")
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        where = "WHERE " + " AND ".join(clauses)

        with _get_conn(self._db_path) as conn:
            totals_row = conn.execute(
                f"SELECT {_USAGE_SUM_COLS} FROM audit_log {where}", params
            ).fetchone()
            # substr(ts,1,10) = the YYYY-MM-DD (UTC) prefix of the ISO timestamp.
            by_day_rows = conn.execute(
                f"SELECT substr(ts,1,10) AS day, {_USAGE_SUM_COLS} "
                f"FROM audit_log {where} GROUP BY day ORDER BY day ASC",
                params,
            ).fetchall()
            by_model_rows = conn.execute(
                "SELECT COALESCE(json_extract(details_json,'$.model'),'unknown') AS model, "
                f"{_USAGE_SUM_COLS} FROM audit_log {where} "
                "GROUP BY model ORDER BY input_tokens DESC",
                params,
            ).fetchall()
            by_source_rows = conn.execute(
                f"SELECT COALESCE(actor,'unknown') AS source, {_USAGE_SUM_COLS} "
                f"FROM audit_log {where} GROUP BY source ORDER BY input_tokens DESC",
                params,
            ).fetchall()

        return {
            "scripts": self._script_summary(where.replace(
                "event_type = 'cache_event'", _SCRIPT_ROWS
            ), params),
            "python_jobs": self._python_job_summary(where, params),
            "totals": _row_to_usage(totals_row),
            "by_day": [{"day": r["day"], **_row_to_usage(r)} for r in by_day_rows],
            "by_model": [{"model": r["model"], **_row_to_usage(r)} for r in by_model_rows],
            "by_source": [
                {"source": r["source"], **_row_to_usage(r)} for r in by_source_rows
            ],
        }

    def erpnext_usage(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        include_private: bool = True,
    ) -> dict[str, Any]:
        """What ERPNext work cost, per unit of work, over an optional window.

        Each turn counts once, under the first of ``ERPNEXT_USAGE_KINDS`` it
        belongs to, with every model call it made (the Executive's
        iterations, the specialists it consulted, its post-turn passes).
        Per kind: how many, their total tokens and cost, and the median and
        90th percentile of one unit's tokens and cost. Figures only: no
        question, answer or ERPNext data. The window bounds the rows that
        mark a turn as ERPNext's; ``include_private`` False leaves out the
        model calls private to the principal, as ``usage_summary`` does.
        """
        from openexecutive.audit.pricing import MAX_TOKENS, price_table

        table = price_table()

        empty = {
            "prices_as_of": table.prices_as_of,
            "kinds": [_kind_summary(kind, []) for kind in ERPNEXT_USAGE_KINDS],
        }
        if not self._db_path.exists():
            return empty

        bounds = ""
        bound_params: list[Any] = []
        if since:
            bounds += " AND ts >= ?"
            bound_params.append(since)
        if until:
            bounds += " AND ts <= ?"
            bound_params.append(until)
        private = "" if include_private else " AND private_to_principal = 0"
        tagged = _ERPNEXT_TURNS.format(scope=bounds + private)
        tag_params = bound_params * 5

        with _get_conn(self._db_path) as conn:
            tag_rows = conn.execute(tagged, tag_params).fetchall()
            call_rows = conn.execute(
                f"WITH tagged AS ({tagged}) "
                "SELECT turn_id, details_json FROM audit_log "
                "WHERE event_type = 'cache_event' "
                f"AND turn_id IN (SELECT turn_id FROM tagged){private}",
                tag_params,
            ).fetchall()

        kind_of: dict[str, int] = {}
        for row in tag_rows:
            tagged_turn, rank = str(row["turn_id"]), int(row["rank"])
            kind_of[tagged_turn] = min(rank, kind_of.get(tagged_turn, rank))

        turns: dict[str, dict[str, Any]] = {}
        for row in call_rows:
            turn_id = str(row["turn_id"])
            try:
                details = json.loads(row["details_json"] or "{}")
            except (TypeError, ValueError):
                details = {}
            if not isinstance(details, dict):
                details = {}
            turn = turns.setdefault(turn_id, {
                "calls": 0, **dict.fromkeys(_TOKEN_FIELDS, 0), "web_search_requests": 0,
                "tokens": 0, "cost_usd": 0.0, "estimated_calls": 0, "unpriced_calls": 0,
            })
            turn["calls"] += 1
            for key in (*_TOKEN_FIELDS, "web_search_requests"):
                try:
                    value = min(max(int(details.get(key) or 0), 0), MAX_TOKENS)
                except (TypeError, ValueError, OverflowError):
                    value = 0
                turn[key] += value
                if key in _TOKEN_FIELDS:
                    turn["tokens"] += value
            cost, estimated = _row_cost(details, table)
            if cost is None:
                turn["unpriced_calls"] += 1
            else:
                turn["cost_usd"] += cost
                turn["estimated_calls"] += int(estimated)

        by_kind: dict[str, list[dict[str, Any]]] = {kind: [] for kind in ERPNEXT_USAGE_KINDS}
        for turn_id, turn in turns.items():
            if turn_id in kind_of:
                by_kind[ERPNEXT_USAGE_KINDS[kind_of[turn_id]]].append(turn)
        return {
            "prices_as_of": table.prices_as_of,
            "kinds": [_kind_summary(kind, by_kind[kind]) for kind in ERPNEXT_USAGE_KINDS],
        }


# --------------------------------------------------------------------------- #
# Module-level singleton so non-FastAPI call sites (Executive, schedule tools,
# integration handlers) can log without threading an instance around.
# --------------------------------------------------------------------------- #

_default_logger: AuditLogger | None = None


def set_audit_logger(instance: AuditLogger | None) -> None:
    global _default_logger
    _default_logger = instance


def get_audit_logger() -> AuditLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = AuditLogger()
    return _default_logger


def log_event(
    event_type: str,
    summary: str,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    actor: str | None = None,
    details: dict[str, Any] | None = None,
    full: dict[str, Any] | None = None,
    department: str | None = None,
    private: bool = False,
    private_to_person: int | None = None,
    principal_only: bool = False,
) -> None:
    """Fire-and-forget convenience wrapper around the default logger.

    When ``session_id`` / ``turn_id`` aren't passed explicitly, fall back to
    the active audit ContextVars (set via ``set_turn`` / ``bind_turn`` at
    the start of every chat turn). Without this fallback, ad-hoc emitters
    that don't thread the ids themselves — e.g. ``honcho_client``'s
    ``_emit_peer_memory`` — produce rows with ``session_id=NULL``, which
    are then invisible in the session-grouped audit view.
    """
    if session_id is None or turn_id is None:
        from openexecutive.audit.context import get_active_ids
        ctx_session, ctx_turn = get_active_ids()
        if session_id is None:
            session_id = ctx_session
        if turn_id is None:
            turn_id = ctx_turn
    try:
        get_audit_logger().log(
            event_type,
            summary,
            session_id=session_id,
            turn_id=turn_id,
            actor=actor,
            details=details,
            full=full,
            department=department,
            private=private,
            private_to_person=private_to_person,
            principal_only=principal_only,
        )
    except Exception:
        logger.warning("audit.log_event_failed event_type=%s", event_type, exc_info=True)
