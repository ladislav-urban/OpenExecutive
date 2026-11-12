"""The test job: proves a watch job's path end to end, and is registered by
the tests alone (``watch.registry._LISTED`` never names it).

It reads Accounts Receivable for the company on the run's day and leaves one
notice: how many customers owe, the total outstanding, and the largest
debtor, with one reason line from the reason step.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, time
from typing import Any

from openexecutive.watch.registry import (
    CardSpec,
    Prior,
    ReasonSchema,
    Settings,
    WatchJob,
    WatchRead,
)

JOB_ID = "example"

# Its entry in oe_connector_watch_jobs, as oe_connector lists it.
RECEIVABLES_SHAPE: Mapping[str, Any] = {
    "report_name": "Accounts Receivable",
    "filters": {"company": "$company", "report_date": "$date", "ageing_based_on": "Due Date"},
    "limit": "$int",
}


def _receivables(settings: Settings, today: date, prior: Prior = ()) -> dict[str, Any]:
    return {
        "report_name": "Accounts Receivable",
        "filters": {"company": settings["company"], "report_date": today.isoformat(), "ageing_based_on": "Due Date"},
        "limit": settings["limit"],
    }


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def build(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> list[CardSpec]:
    """One notice when anyone owes the company anything, figures from the
    report's rows only."""
    rows = reads[0].get("rows")
    if not isinstance(rows, list):
        raise ValueError("no rows")
    owed: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        party = row.get("party") or row.get("customer")
        amount = _amount(row.get("outstanding"))
        if not isinstance(party, str) or amount is None or amount <= 0:
            continue
        owed[party] = owed.get(party, 0.0) + amount
    if not owed:
        return []
    currency = reads[0].get("currency")
    unit = f" {currency}" if isinstance(currency, str) and currency else ""
    total = sum(owed.values())
    largest = max(sorted(owed), key=lambda name: owed[name])
    return [
        CardSpec(
            key=f"receivables:{settings['company']}",
            summary="Customers owe the company money today.",
            values=(
                ("Customers owing", str(len(owed))),
                ("Outstanding", f"{total:,.2f}{unit}"),
                ("Largest debtor", largest),
                ("Their outstanding", f"{owed[largest]:,.2f}{unit}"),
            ),
            facts={"customers_owing": len(owed), "outstanding": round(total, 2), "largest_debtor": largest},
        )
    ]


JOB = WatchJob(
    id=JOB_ID,
    title="Receivables check",
    reads=(WatchRead("run_report", RECEIVABLES_SHAPE, _receivables, "the receivables report"),),
    build=build,
    schedule=time(7, 0),
    card_cap_day=1,
    default_settings={"company": "", "limit": 200},
    reason_schema=ReasonSchema("Say in one sentence why the largest debtor is worth a call today."),
)
