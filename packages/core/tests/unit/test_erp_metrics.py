"""Scheduled ERP metrics (briefing/erp_metrics.py): fixed figures from
ERPNext, sent to the principal on a schedule, and the scheduler's run of them."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.briefing import erp_metrics
from openexecutive.delegation.settings import pin_turn_delegation
from openexecutive.memory.episodic import get_scheduled_action
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

from . import test_delegation_tools, test_erp_numbers
from .test_delegation_tools import OWNER, TEAM
from .test_erp_numbers import RESULTS, TODAY, _Gateway

# Their fixtures: a temp DB and a fresh turn pin.
db = test_delegation_tools.db
fresh_turn_state = test_delegation_tools.fresh_turn_state
assert test_erp_numbers  # the fakes above come from it

SALES_90_DAYS = {"currency": "USD", "total_row": {"net_total": 500000.0, "grand_total": 592000.0}}


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> _Gateway:
    fake = _Gateway({**RESULTS, "Sales Register": SALES_90_DAYS})
    monkeypatch.setenv("ERPNEXT_BRIEF_COMPANY", "Raveno Electric")
    monkeypatch.setattr("openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: fake)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *_a, **_k: None)
    return fake


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    teammate = people_store.upsert_person(full_name="Ben Teammate", role="Ops", email=TEAM)
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate)


def _turn(person_id: int) -> Session:
    session = Session(from_web_chat=True, caller_person_id=person_id)
    pin_turn_delegation(session, "send me DSO every Monday at 8")
    return session


def _call(session: Session, handler: Any, tool_input: dict[str, Any]) -> dict[str, Any]:
    async def go() -> str:
        with set_session(session):
            return await handler(tool_input)

    return json.loads(asyncio.run(go()))


# --------------------------------------------------------------------------- #
# The figures
# --------------------------------------------------------------------------- #


def test_dso_is_receivables_against_ninety_days_of_sales(gateway: _Gateway) -> None:
    text = asyncio.run(erp_metrics.compute("dso", TODAY))
    assert text == (
        "Days sales outstanding (Raveno Electric, from ERPNext's own reports): Days sales outstanding: "
        "45 days (296,000.00 USD receivable against 592,000.00 USD invoiced in the last 90 days, "
        "2026-07-15 to 2026-10-12)."
    )
    # Two of the brief's own reports, marked as the brief's run, the sales
    # over exactly the 90 days before today.
    assert [c["report_name"] for c in gateway.calls] == ["Accounts Receivable Summary", "Sales Register"]
    assert all(c["marked"] for c in gateway.calls)
    assert (gateway.calls[1]["filters"]["from_date"], gateway.calls[1]["filters"]["to_date"]) == (
        "2026-07-15", "2026-10-12",
    )


def test_no_sales_or_a_refused_report_gives_no_figure(gateway: _Gateway) -> None:
    gateway.results["Sales Register"] = {"currency": "USD", "rows": [], "row_count": 0}
    assert asyncio.run(erp_metrics.compute("dso", TODAY)) is None
    gateway.results["Accounts Receivable Summary"] = "Tool error: not permitted"
    assert asyncio.run(erp_metrics.compute("receivables", TODAY)) is None


def test_a_metric_only_runs_the_briefs_reports(gateway: _Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    async def ledger(report: Any, company: str, today: date) -> str | None:
        result = await report("General Ledger", {"company": company})
        return "never" if result else None

    monkeypatch.setitem(erp_metrics.METRICS, "dso", erp_metrics.Metric("x", "x", ledger))
    assert asyncio.run(erp_metrics.compute("dso", TODAY)) is None
    assert gateway.calls == []


def test_off_without_a_company(gateway: _Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ERPNEXT_BRIEF_COMPANY")
    assert asyncio.run(erp_metrics.compute("cash", TODAY)) is None


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #


def test_the_principal_schedules_replaces_and_cancels(gateway: _Gateway, roster: SimpleNamespace) -> None:
    owner = _turn(roster.principal)
    out = _call(owner, erp_metrics.handle_schedule, {"metric": "dso", "schedule": "weekly@mon@08:00"})
    assert out["status"] == "scheduled" and [s["schedule"] for s in out["scheduled"]] == ["weekly@mon@08:00"]
    # A metric has one schedule: setting it again replaces it.
    out = _call(owner, erp_metrics.handle_schedule, {"metric": "dso", "schedule": "daily@07:30"})
    assert [(s["metric"], s["schedule"]) for s in out["scheduled"]] == [("dso", "daily@07:30")]
    assert len(erp_metrics.pending()) == 1
    out = _call(owner, erp_metrics.handle_cancel, {"metric": "dso"})
    assert out["status"] == "cancelled" and erp_metrics.pending() == []


@pytest.mark.parametrize("schedule", ["every monday", "weekly@mon@25:00", "quarterly@01-08:00", "daily@8:00"])
def test_only_a_daily_or_weekly_time_is_a_schedule(
    gateway: _Gateway, roster: SimpleNamespace, schedule: str,
) -> None:
    out = _call(_turn(roster.principal), erp_metrics.handle_schedule, {"metric": "cash", "schedule": schedule})
    assert "schedule" in out["error"] and erp_metrics.pending() == []


def test_only_the_principal_on_their_own_turn_is_offered_it(gateway: _Gateway, roster: SimpleNamespace) -> None:
    assert erp_metrics.offered(_turn(roster.principal))
    assert not erp_metrics.offered(_turn(roster.teammate))
    for session in (
        Session(unattended=True),
        Session(erpnext_panel_email=OWNER, origin_channel="erpnext"),
        Session(origin_channel="email", caller_person_id=roster.principal),
        Session(private_to_principal=True, from_web_chat=True, caller_person_id=roster.principal),
    ):
        assert not erp_metrics.offered(session)
    out = _call(_turn(roster.teammate), erp_metrics.handle_schedule, {"metric": "dso", "schedule": "daily@07:30"})
    assert "not available" in out["error"]


# --------------------------------------------------------------------------- #
# The scheduler's run
# --------------------------------------------------------------------------- #


def test_a_run_delivers_the_figure_and_chains_the_next(gateway: _Gateway, roster: SimpleNamespace) -> None:
    now = datetime.now(UTC)
    action_id = erp_metrics._enqueue("dso", "daily@07:30", now - timedelta(days=2))
    action = get_scheduled_action(action_id)
    assert action is not None
    sent: list[tuple[str, str]] = []

    async def deliver(text: str, *, label: str) -> Any:
        sent.append((label, text))
        return SimpleNamespace(ok=True)

    asyncio.run(erp_metrics.run(action, now, deliver))
    ((label, text),) = sent
    assert label == "Days sales outstanding" and "Days sales outstanding:" in text
    done = get_scheduled_action(action_id)
    assert done is not None and done.status == "done"
    ((next_id, slug, spec, run_at),) = erp_metrics.pending()
    assert (slug, spec) == ("dso", "daily@07:30") and next_id != action_id
    assert datetime.fromisoformat(run_at) > now


def test_a_run_with_the_metrics_off_is_retired_unchained(
    gateway: _Gateway, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = get_scheduled_action(erp_metrics._enqueue("cash", "daily@07:30", datetime.now(UTC)))
    monkeypatch.delenv("ERPNEXT_BRIEF_COMPANY")

    async def deliver(text: str, *, label: str) -> Any:
        raise AssertionError("nothing is sent")

    asyncio.run(erp_metrics.run(action, datetime.now(UTC), deliver))
    assert erp_metrics.pending() == []
    retired = get_scheduled_action(action.id)
    assert retired is not None and retired.status == "cancelled"


def test_the_prompt_names_scheduled_metrics_only_with_a_company(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.config import get_settings
    from openexecutive.prompts.connected_systems import render_connected_systems

    off = render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())
    monkeypatch.setenv("ERPNEXT_BRIEF_COMPANY", "Raveno Electric")
    on = render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())
    assert "schedule_erp_metric" not in off
    assert "schedule_erp_metric" in on and "days sales outstanding" in on
    # The same text however often it renders: block 0 stays cacheable.
    assert on == render_connected_systems(mcp_servers=("erpnext",), settings=get_settings())


def test_a_cancel_while_it_runs_stands(gateway: _Gateway, roster: SimpleNamespace) -> None:
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path

    action_id = erp_metrics._enqueue("dso", "daily@07:30", datetime.now(UTC) - timedelta(days=2))
    with _get_conn(_resolve_db_path(None)) as conn:
        conn.execute("UPDATE scheduled_actions SET status = 'running' WHERE id = ?", (action_id,))
    action = get_scheduled_action(action_id)
    # The principal stops it while the scheduler is computing it.
    assert erp_metrics._cancel("dso") == 1

    async def deliver(text: str, *, label: str) -> Any:
        return SimpleNamespace(ok=True)

    asyncio.run(erp_metrics.run(action, datetime.now(UTC), deliver))
    stopped = get_scheduled_action(action_id)
    assert stopped is not None and stopped.status == "cancelled"
    assert erp_metrics.pending() == []
