"""The morning brief's ERP NUMBERS block: a few totals from ERPNext.

ERPNext phase 3b (design: oe_connector's ``docs/erpnext-integration.md``,
"Briefing and watch list"). A fixed set of ERPNext's own reports, run by this
code (never by the model), reduced to totals:

- cash this month: Cash Flow's summary (net change, and its parts);
- receivables today: Accounts Receivable Summary's total row, overdue by age;
- payables today: Accounts Payable Summary's total row;
- yesterday's sales: Sales Register's total row for that day.

Only totals reach the brief: no customer, supplier or item names, so no text
an outsider wrote. The only words ERPNext supplies, its currency code and its
ageing bucket labels, are an ERPNext administrator's to edit, so each must be a
plain code or a short label or it is dropped. Each report gets
``CALL_TIMEOUT_S``; one that stalls is left out. The morning brief adds the block only on a run for the
principal (``_private_ok``), and its text then stays out of the shared run
history (``private_to_principal``).

**How it may ask ERPNext.** Every unattended run is refused ERPNext
(``erpnext_caller.caller_for``): a scheduled follow-up's audience is whoever
scheduled it. This run's audience is fixed, the principal, so it marks its
own calls with ``erpnext_caller.brief_run()``, a context the model can never
set: with per-person access the gateway signs them as the ``service`` caller,
which oe_connector holds to the reports its operator lists
(``oe_connector_unattended_reports``: the four above); without it they run as
the service user, as every ERPNext call does in phase 1, for the principal.

``ERPNEXT_BRIEF_COMPANY`` names the company and turns the block on; unset, the
brief is unchanged. A report that fails or is refused is left out, with a log
line; the block never fails the brief.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Each report gets this long; one that stalls is left out, so ERPNext can
# never hold up the morning brief.
CALL_TIMEOUT_S = 30.0
# The only words ERPNext supplies here, its currency code and its ageing
# bucket labels ("0-30", "121-Above"), are an ERPNext administrator's to
# edit: anything but a plain code or a short label is dropped, so they can
# carry no sentence into the brief.
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_LABEL_RE = re.compile(r"[0-9A-Za-z<>+ -]{1,12}")

# What the four runs are called, for oe_connector's list.
REPORTS: tuple[str, ...] = (
    "Cash Flow",
    "Accounts Receivable Summary",
    "Accounts Payable Summary",
    "Sales Register",
)


@dataclass(frozen=True)
class _Run:
    report: str
    filters: Callable[[date], dict[str, Any]]
    render: Callable[[dict[str, Any], date], str | None]


def _currency(*sources: Any) -> str:
    """The first plain ISO currency code among ``sources``' ``currency``."""
    for source in sources:
        value = source.get("currency") if isinstance(source, dict) else None
        if isinstance(value, str) and _CURRENCY_RE.fullmatch(value):
            return value
    return ""


def _money(value: Any, currency: str) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{value:,.2f} {currency}".strip()


def _cash(result: dict[str, Any], today: date) -> str | None:
    figures = {
        str(item.get("label")): item.get("value")
        for item in result.get("summary") or []
        if isinstance(item, dict)
    }
    currency = _currency(result)
    net = _money(figures.get("Net Change in Cash"), currency)
    if net is None:
        return None
    parts = [
        f"{label.removeprefix('Net Cash from ').lower()} {amount}"
        for label in ("Net Cash from Operations", "Net Cash from Investing", "Net Cash from Financing")
        if (amount := _money(figures.get(label), currency)) is not None
    ]
    return f"Cash this month (since {today.replace(day=1).isoformat()}): net change {net}" + (
        f" ({', '.join(parts)})" if parts else ""
    ) + "."


def _ageing(result: dict[str, Any], total: dict[str, Any], currency: str) -> str:
    """An ageing total row by its buckets, in the report's own labels. With
    ageing on the due date, range0 is not yet due and range1-5 are past it
    ("0-30" is 0 to 30 days late, not current): said in full, since a reader
    took "0-30 days" for current money in a live run."""
    labels = {
        str(c.get("fieldname")): label
        for c in result.get("columns") or []
        if isinstance(c, dict) and isinstance(label := c.get("label"), str) and _LABEL_RE.fullmatch(label)
    }

    def amount(key: str) -> float | None:
        value = total.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value else None

    late = [
        f"{_money(value, currency)} {labels.get(key) or key} days past due"
        for key in ("range1", "range2", "range3", "range4", "range5")
        if (value := amount(key)) is not None
    ]
    text = f"; past due date: {', '.join(late)}" if late else "; nothing past due date"
    if (current := amount("range0")) is not None:
        text += f"; not yet due: {_money(current, currency)}"
    return text


def _ledger(name: str) -> Callable[[dict[str, Any], date], str | None]:
    def render(result: dict[str, Any], today: date) -> str | None:
        total = result.get("total_row")
        currency = _currency(result, total)
        if not isinstance(total, dict):
            # No total row is "none" only when the report found no rows; any
            # other shape is not a figure to state.
            return f"{name} today: none outstanding." if result.get("row_count") == 0 else None
        outstanding = _money(total.get("outstanding"), currency)
        if outstanding is None:
            return None
        return f"{name} today: {outstanding} outstanding{_ageing(result, total, currency)}."

    return render


def _sales(result: dict[str, Any], today: date) -> str | None:
    day = (today - timedelta(days=1)).isoformat()
    total = result.get("total_row")
    if not isinstance(total, dict):
        if result.get("row_count") != 0:
            return None
        return f"Sales yesterday ({day}): no submitted sales invoices."
    currency = _currency(result, total)
    net, gross = _money(total.get("net_total"), currency), _money(total.get("grand_total"), currency)
    if net is None:
        return None
    return f"Sales yesterday ({day}): {net} net" + (f", {gross} with taxes" if gross and gross != net else "") + "."


def _runs(company: str) -> list[_Run]:
    def yesterday(today: date) -> str:
        return (today - timedelta(days=1)).isoformat()

    return [
        _Run(
            "Cash Flow",
            lambda today: {
                "company": company,
                "filter_based_on": "Date Range",
                "period_start_date": today.replace(day=1).isoformat(),
                "period_end_date": today.isoformat(),
                "periodicity": "Monthly",
            },
            _cash,
        ),
        _Run(
            "Accounts Receivable Summary",
            lambda today: {"company": company, "report_date": today.isoformat()},
            _ledger("Receivables"),
        ),
        _Run(
            "Accounts Payable Summary",
            lambda today: {"company": company, "report_date": today.isoformat()},
            _ledger("Payables"),
        ),
        _Run(
            "Sales Register",
            lambda today: {"company": company, "from_date": yesterday(today), "to_date": yesterday(today)},
            _sales,
        ),
    ]


def brief_company() -> str:
    """``ERPNEXT_BRIEF_COMPANY``, stripped; "" when the block is off."""
    from openexecutive.config import get_settings

    return (get_settings().erpnext_brief_company or "").strip()


async def gather_erp_numbers(today: date) -> str:
    """The ERP NUMBERS block for the principal's brief, "" when it is off,
    ERPNext is not connected, or nothing came back. Never raises."""
    company = brief_company()
    if not company:
        return ""
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.orchestrator.mcp_gateway import gateway_server_names, get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_SERVER

    gateway = get_active_gateway()
    if gateway is None or ERPNEXT_SERVER not in gateway_server_names(gateway):
        return ""
    lines: list[str] = []
    currency = ""
    with erpnext_caller.brief_run():
        for run in _runs(company):
            try:
                raw = await asyncio.wait_for(
                    gateway.call_tool({
                        "name": "erpnext__run_report",
                        "arguments": {"report_name": run.report, "filters": run.filters(today), "limit": 1},
                    }),
                    timeout=CALL_TIMEOUT_S,
                )
                result = json.loads(raw)
                if not isinstance(result, dict) or "error" in result:
                    raise ValueError("refused")
                line = run.render(result, today)
            except Exception:
                # A plain-text refusal ("Tool error: …") is not JSON either.
                logger.warning("erp_numbers: %s gave no figures", run.report)
                continue
            if line:
                lines.append(f"- {line}")
                currency = currency or _currency(result)
    if not lines:
        return ""
    head = f"ERP NUMBERS (ERPNext's own reports, {company}" + (f", {currency}" if currency else "") + "):"
    return "\n".join([head, *lines])
