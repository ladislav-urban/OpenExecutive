"""Scheduled ERP metrics: one figure from ERPNext, sent to the principal.

The phase 3 follow-up "saved queries, such as DSO every Monday" (design:
oe_connector's ``docs/erpnext-integration.md``, "Briefing and watch list").
Decided with the owner: a fixed catalog of metrics computed here, never a
report or filters chosen in a conversation; sent to the principal's own
channel; the principal's alone.

**What runs unattended.** Only what this code does with ``METRICS``: the
same reports the morning brief's ERP NUMBERS run (``erp_numbers.REPORTS``,
the four oe_connector's operator lists in ``oe_connector_unattended_reports``),
inside ``erpnext_caller.brief_run()``, which the model can never set. With
per-person access the gateway signs them as the ``service`` caller, held to
those reports. Only totals come back, so no customer, supplier or item names
reach the message. A metric that fails is skipped for that run, with a log
line; the schedule goes on.

**Subscribing** (``schedule_erp_metric``, ``cancel_erp_metric``): offered on
the principal's own chat turns (``offered``) while ``ERPNEXT_BRIEF_COMPANY``
is set and ERPNext is connected. The model picks a metric by name and a
schedule (``daily@HH:MM`` or ``weekly@DOW@HH:MM``, in the user's zone); a
metric has one schedule at a time. Each is a ``scheduled_actions`` row of
kind ``KIND``, its metric and schedule in ``scope_key``; the scheduler runs it
(``run``), delivers it as it delivers the brief, and chains the next one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from openexecutive.briefing import erp_numbers

logger = logging.getLogger(__name__)

KIND = "erpnext_metric"
SCHEDULE = "schedule_erp_metric"
CANCEL = "cancel_erp_metric"
_SCOPE_PREFIX = "erp_metric:"
# How many metrics the principal may have on a schedule at once.
MAX_SCHEDULED = 5
# A recurring metric's runs are at least this far apart (a zone change while
# one ran can't send it twice the same day).
_MIN_GAP = timedelta(hours=12)
_SPEC_RE = re.compile(r"(daily@\d{2}:\d{2}|weekly@(mon|tue|wed|thu|fri|sat|sun)@\d{2}:\d{2})")
# The days of sales DSO is measured over.
DSO_DAYS = 90

Report = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class Metric:
    label: str
    description: str
    compute: Callable[[Report, str, date], Awaitable[str | None]]


def _total(result: dict[str, Any] | None, key: str) -> tuple[float, str] | None:
    """A total row's ``key`` and its currency; 0 for a report with no rows."""
    if result is None:
        return None
    total = result.get("total_row")
    if not isinstance(total, dict):
        return (0.0, erp_numbers._currency(result)) if result.get("row_count") == 0 else None
    value = total.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value), erp_numbers._currency(result, total)


async def _dso(report: Report, company: str, today: date) -> str | None:
    since = today - timedelta(days=DSO_DAYS)
    receivable = _total(
        await report(
            "Accounts Receivable Summary", {"company": company, "report_date": today.isoformat()}
        ),
        "outstanding",
    )
    sales = _total(
        await report(
            "Sales Register",
            {
                "company": company,
                "from_date": since.isoformat(),
                "to_date": (today - timedelta(days=1)).isoformat(),
            },
        ),
        "grand_total",
    )
    if receivable is None or sales is None or sales[0] <= 0:
        return None
    (owed, currency), (sold, sales_currency) = receivable, sales
    if currency and sales_currency and currency != sales_currency:
        return None
    days = owed / sold * DSO_DAYS
    return (
        f"Days sales outstanding: {days:.0f} days ({erp_numbers._money(owed, currency)} receivable "
        f"against {erp_numbers._money(sold, currency)} invoiced in the last {DSO_DAYS} days, "
        f"{since.isoformat()} to {(today - timedelta(days=1)).isoformat()})."
    )


def _from_brief(
    report_name: str,
    filters: Callable[[str, date], dict[str, Any]],
    render: Callable[[dict[str, Any], date], str | None],
) -> Callable[[Report, str, date], Awaitable[str | None]]:
    async def compute(report: Report, company: str, today: date) -> str | None:
        result = await report(report_name, filters(company, today))
        return render(result, today) if result is not None else None

    return compute


def _ledger_filters(company: str, today: date) -> dict[str, Any]:
    return {"company": company, "report_date": today.isoformat()}


def _cash_filters(company: str, today: date) -> dict[str, Any]:
    return {
        "company": company,
        "filter_based_on": "Date Range",
        "period_start_date": today.replace(day=1).isoformat(),
        "period_end_date": today.isoformat(),
        "periodicity": "Monthly",
    }


async def _sales_week(report: Report, company: str, today: date) -> str | None:
    start, end = today - timedelta(days=7), today - timedelta(days=1)
    result = await report(
        "Sales Register",
        {"company": company, "from_date": start.isoformat(), "to_date": end.isoformat()},
    )
    net = _total(result, "net_total")
    if net is None:
        return None
    amount, currency = net
    return f"Sales in the last 7 days ({start.isoformat()} to {end.isoformat()}): {erp_numbers._money(amount, currency)} net."


METRICS: dict[str, Metric] = {
    "dso": Metric(
        "Days sales outstanding", "receivables today against the last 90 days of sales", _dso
    ),
    "cash": Metric(
        "Cash this month",
        "net change in cash since the 1st, by its parts",
        _from_brief("Cash Flow", _cash_filters, erp_numbers._cash),
    ),
    "receivables": Metric(
        "Receivables",
        "outstanding today, by days past due",
        _from_brief(
            "Accounts Receivable Summary", _ledger_filters, erp_numbers._ledger("Receivables")
        ),
    ),
    "payables": Metric(
        "Payables",
        "outstanding today, by days past due",
        _from_brief("Accounts Payable Summary", _ledger_filters, erp_numbers._ledger("Payables")),
    ),
    "sales_week": Metric("Sales last week", "net sales over the last 7 days", _sales_week),
}


# ---------------------------------------------------------------------------
# Computing one
# ---------------------------------------------------------------------------


def _connected() -> Any:
    """The running gateway when ERPNext is on it, else None."""
    from openexecutive.orchestrator.mcp_gateway import gateway_server_names, get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_SERVER

    gateway = get_active_gateway()
    return (
        gateway if gateway is not None and ERPNEXT_SERVER in gateway_server_names(gateway) else None
    )


async def compute(slug: str, today: date) -> str | None:
    """``slug``'s message for the principal, or None when it is off or
    ERPNext gave no figures. Never raises."""
    from openexecutive.orchestrator import erpnext_caller

    metric, company, gateway = METRICS.get(slug), erp_numbers.brief_company(), _connected()
    if metric is None or not company or gateway is None:
        return None

    async def report(name: str, filters: dict[str, Any]) -> dict[str, Any] | None:
        if name not in erp_numbers.REPORTS:
            # Only the reports oe_connector lets an unattended run use.
            return None
        try:
            raw = await asyncio.wait_for(
                gateway.call_tool(
                    {
                        "name": "erpnext__run_report",
                        "arguments": {"report_name": name, "filters": filters, "limit": 1},
                    }
                ),
                timeout=erp_numbers.CALL_TIMEOUT_S,
            )
            result = json.loads(raw)
        except Exception:
            logger.warning("erp_metrics: %s gave no figures", name)
            return None
        return result if isinstance(result, dict) and "error" not in result else None

    with erpnext_caller.brief_run():
        try:
            line = await metric.compute(report, company, today)
        except Exception:
            logger.warning("erp_metrics: computing %s failed", slug, exc_info=True)
            return None
    return f"{metric.label} ({company}, from ERPNext's own reports): {line}" if line else None


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def _next_run(spec: str, after: datetime) -> datetime | None:
    from openexecutive.departments.cadence import _parse_cadence_spec
    from openexecutive.memory.workspace_settings import get_user_timezone

    if not _SPEC_RE.fullmatch(spec):
        return None
    try:
        return _parse_cadence_spec(spec, after, get_user_timezone())
    except ValueError:
        return None


def _scope(slug: str, spec: str) -> str:
    return f"{_SCOPE_PREFIX}{slug}:{spec}"


def _parse_scope(scope: str | None) -> tuple[str, str] | None:
    if not scope or not scope.startswith(_SCOPE_PREFIX):
        return None
    slug, _, spec = scope[len(_SCOPE_PREFIX) :].partition(":")
    return (slug, spec) if slug in METRICS and _SPEC_RE.fullmatch(spec) else None


def pending() -> list[tuple[int, str, str, str]]:
    """``(action_id, slug, spec, run_at)`` of every metric on a schedule."""
    return _rows(("pending",))


def _rows(statuses: tuple[str, ...]) -> list[tuple[int, str, str, str]]:
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path

    resolved = _resolve_db_path(None)
    if not resolved.exists():
        return []
    marks = ",".join("?" * len(statuses))
    with _get_conn(resolved) as conn:
        rows = conn.execute(
            f"SELECT id, scope_key, run_at FROM scheduled_actions WHERE kind = ? "
            f"AND status IN ({marks}) ORDER BY run_at",
            (KIND, *statuses),
        ).fetchall()
    out = []
    for action_id, scope, run_at in rows:
        parsed = _parse_scope(scope)
        if parsed is not None:
            out.append((int(action_id), parsed[0], parsed[1], str(run_at)))
    return out


def _enqueue(slug: str, spec: str, after: datetime) -> int | None:
    from openexecutive.memory.episodic import insert_scheduled_action

    run_at = _next_run(spec, after)
    if run_at is None:
        return None
    return insert_scheduled_action(
        run_at=run_at.isoformat(),
        channel="__internal__",
        channel_ref="principal",
        intent_text=f"ERP metric: {METRICS[slug].label} ({spec})",
        kind=KIND,
        scope_key=_scope(slug, spec),
    )


def _cancel(slug: str) -> int:
    from openexecutive.memory.episodic import mark_action_cancelled

    cancelled = 0
    # A running one too: run() sees it cancelled and chains no next one.
    for action_id, pending_slug, _spec, _run_at in _rows(("pending", "running")):
        if pending_slug == slug and mark_action_cancelled(
            action_id, "the principal changed or cancelled it"
        ):
            cancelled += 1
    return cancelled


async def run(action: Any, now: datetime, deliver: Callable[..., Awaitable[Any]]) -> None:
    """The scheduler's run of one ``KIND`` row: compute, deliver with
    ``deliver`` (the runner's ``_deliver_to_principal``), then chain the next
    one. A metric whose company or ERPNext went away is retired unchained."""
    from openexecutive.audit import log_event as audit_log
    from openexecutive.memory.episodic import (
        get_scheduled_action,
        mark_action_cancelled,
        mark_action_done,
    )

    parsed = _parse_scope(getattr(action, "scope_key", None))
    if parsed is None or not erp_numbers.brief_company():
        mark_action_cancelled(action.id, "ERP metrics are off or this one is unknown")
        return
    slug, spec = parsed
    text = await compute(slug, now.astimezone(_zone()).date())
    if text:
        delivery = await deliver(text, label=METRICS[slug].label)
        ok = bool(getattr(delivery, "ok", False))
        audit_log(
            "scheduled_action",
            f"{KIND} {slug} {'delivered' if ok else 'NOT delivered'}",
            actor="scheduler",
            details={"kind": KIND, "metric": slug, "delivered": ok},
            private=True,
        )
    else:
        logger.warning("erp_metrics: %s gave no figures this run", slug)
    # Changed or cancelled while it ran (the principal, or DELETE
    # /scheduled/{id}): that stands, and no next one is chained.
    current = get_scheduled_action(action.id)
    if current is None or current.status == "cancelled":
        logger.info("erp_metrics: %s was cancelled while it ran; not chained", slug)
        return
    mark_action_done(action.id)
    fired = _run_at(action)
    next_id = None
    try:
        next_id = _enqueue(slug, spec, max(now, fired + _MIN_GAP) if fired else now)
    except Exception:
        logger.exception("erp_metrics: chaining %s failed", slug)
    if next_id is None:
        # The schedule ends here unless they set it again: said where the
        # owner and an operator can see it.
        logger.warning("erp_metrics: %s's next run could not be scheduled", slug)
        audit_log(
            "scheduled_action",
            f"{KIND} {slug}: the next run could not be scheduled",
            actor="scheduler",
            details={"kind": KIND, "metric": slug, "phase": "chain_failed"},
            private=True,
        )


def _zone() -> Any:
    from openexecutive.memory.workspace_settings import get_user_timezone

    return get_user_timezone()


def _run_at(action: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(action.run_at))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def offered(session: Any) -> bool:
    """The principal's own chat turn, with the metrics on: never a run nobody
    is watching, ERPNext's panel, an email turn or a turn private to them."""
    from openexecutive.orchestrator.content_trust import principal_speaking
    from openexecutive.orchestrator.schedule_tools import erpnext_panel_turn

    try:
        return (
            session is not None
            and getattr(session, "unattended", False) is not True
            and getattr(session, "private_to_principal", False) is not True
            and str(getattr(session, "origin_channel", "") or "") != "email"
            and not erpnext_panel_turn(session)
            and bool(erp_numbers.brief_company())
            and _connected() is not None
            and principal_speaking(session)
        )
    except Exception:
        logger.warning("erp_metrics: couldn't tell who is asking — not offered", exc_info=True)
        return False


def _listing() -> list[dict[str, str]]:
    return [
        {"metric": slug, "schedule": spec, "next": run_at} for _id, slug, spec, run_at in pending()
    ]


_CATALOG = "; ".join(
    f"`{slug}` ({m.label.lower()}: {m.description})" for slug, m in METRICS.items()
)

METRIC_TOOLS: list[dict[str, Any]] = [
    {
        "name": CANCEL,
        "description": "Stop sending the principal a scheduled ERP metric. Returns what is still scheduled.",
        "input_schema": {
            "type": "object",
            "properties": {"metric": {"type": "string", "enum": sorted(METRICS)}},
            "required": ["metric"],
        },
    },
    {
        "name": SCHEDULE,
        "description": (
            "Send the principal one ERPNext figure on a schedule, to their own channel, as the "
            f"morning brief is sent. Metrics: {_CATALOG}. Schedule: daily@HH:MM or "
            "weekly@DOW@HH:MM (DOW is mon..sun), in the user's own time zone; e.g. "
            '"DSO every Monday at 8" is metric dso, schedule weekly@mon@08:00. A metric has one '
            "schedule: setting it again replaces it. Only these metrics exist; say so if they ask "
            "for another. Returns what is scheduled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": sorted(METRICS)},
                "schedule": {
                    "type": "string",
                    "description": "daily@HH:MM or weekly@DOW@HH:MM, local time.",
                },
            },
            "required": ["metric", "schedule"],
        },
    },
]


def _error(message: str) -> str:
    return json.dumps({"error": message})


async def handle_schedule(tool_input: dict[str, Any]) -> str:
    from openexecutive.orchestrator.schedule_tools import current_session

    if not offered(current_session.get()):
        return _error(f"{SCHEDULE} is not available here. Say so; do not retry.")
    slug, spec = tool_input.get("metric"), str(tool_input.get("schedule") or "").strip().lower()
    if slug not in METRICS:
        return _error(f"`metric` must be one of {', '.join(sorted(METRICS))}.")
    if _next_run(spec, datetime.now(UTC)) is None:
        return _error(
            "`schedule` must be daily@HH:MM or weekly@DOW@HH:MM (DOW mon..sun), a real time of day."
        )
    others = {s for _i, s, _sp, _r in pending() if s != slug}
    if len(others) >= MAX_SCHEDULED:
        return _error(f"{MAX_SCHEDULED} metrics are already scheduled; ask which to stop first.")
    _cancel(slug)
    if _enqueue(slug, spec, datetime.now(UTC)) is None:
        return _error("Couldn't schedule it just now. Try again in a moment.")
    return json.dumps(
        {"status": "scheduled", "metric": slug, "schedule": spec, "scheduled": _listing()}
    )


async def handle_cancel(tool_input: dict[str, Any]) -> str:
    from openexecutive.orchestrator.schedule_tools import current_session

    if not offered(current_session.get()):
        return _error(f"{CANCEL} is not available here. Say so; do not retry.")
    slug = tool_input.get("metric")
    if slug not in METRICS:
        return _error(f"`metric` must be one of {', '.join(sorted(METRICS))}.")
    stopped = _cancel(str(slug))
    return json.dumps(
        {
            "status": "cancelled" if stopped else "not_scheduled",
            "metric": slug,
            "scheduled": _listing(),
        }
    )


METRIC_TOOL_HANDLERS: dict[str, Any] = {SCHEDULE: handle_schedule, CANCEL: handle_cancel}
