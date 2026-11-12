"""A chat scenario may run as a turn from ERPNext's "Ask the Dynocortex" panel.

``surface: erpnext_panel`` gives the turn what ``integrations.erpnext_panel``
gives it: the session marks that make it the panel's (only ERPNext reads and
specialists are offered) and ``PANEL_CONTEXT_BLOCK`` as the channel block, so
an eval sees the panel's own prompt line (erpnext_006).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.evals import runner as runner_module
from openexecutive.evals.scenarios import (
    EVAL_PANEL_EMAIL,
    load_scenarios,
    scenario_surface,
    validate_scenario_yaml,
)
from openexecutive.integrations.erpnext_panel import PANEL_CONTEXT_BLOCK, PANEL_DRAFTS_CONTEXT_BLOCK
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.schedule_tools import erpnext_panel_turn


def _run(monkeypatch: pytest.MonkeyPatch, scenario: dict[str, Any]) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def fake_chat(self: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "a response"

    async def fake_judge(_scenario: dict[str, Any], _response: str) -> dict[str, Any]:
        return {"overall": 5}

    monkeypatch.setattr(Executive, "__init__", lambda self, mcp_gateway=None: None)
    monkeypatch.setattr(Executive, "chat", fake_chat)
    monkeypatch.setattr(runner_module, "judge_chat", fake_judge)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    run_one = runner_module._make_chat_runner(asyncio.Semaphore(1), queue, [0], 1, None)
    asyncio.run(run_one(0, scenario))
    return seen


def test_a_panel_scenario_runs_as_the_panel_route_runs_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _run(monkeypatch, {"id": "p", "query": "q", "surface": "erpnext_panel"})
    session = seen["session"]
    assert erpnext_panel_turn(session)
    assert session.erpnext_panel_email == EVAL_PANEL_EMAIL
    assert session.origin_channel == "erpnext" and session.read_erpnext is True
    assert seen["channel_context_block"] == PANEL_CONTEXT_BLOCK


def test_make_eval_runs_a_panel_scenario_the_same_way() -> None:
    # `make eval` runs evals/run_evals.py, which builds its own session.
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[4] / "evals" / "run_evals.py"
    spec = importlib.util.spec_from_file_location("run_evals", script)
    assert spec is not None and spec.loader is not None
    run_evals = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_evals)
    seen: dict[str, Any] = {}

    class FakeExecutive:
        async def chat(self, **kwargs: Any) -> str:
            seen.update(kwargs)
            return "a response"

    scenario = {"id": "p", "domain": "mcp", "query": "q", "surface": "erpnext_panel"}
    asyncio.run(run_evals.run_eval(scenario, FakeExecutive(), None))
    assert erpnext_panel_turn(seen["session"])
    assert seen["channel_context_block"] == PANEL_CONTEXT_BLOCK


def test_a_panel_scenario_on_a_screen_gets_the_routes_page_block(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.integrations.erpnext_panel import page_block

    page = {"screen": "/watchlist"}
    seen = _run(monkeypatch, {"id": "p", "query": "q", "surface": "erpnext_panel", "page": page})
    assert seen["user_message"] == f"{page_block(page)}\n\nq"
    assert _run(monkeypatch, {"id": "p", "query": "q", "surface": "erpnext_panel"})["user_message"] == "q"


def test_a_page_is_only_for_a_panel_scenario_and_must_say_something() -> None:
    with pytest.raises(ValueError, match="page"):
        validate_scenario_yaml("id: x\nquery: q\npage:\n  screen: /watchlist\n")
    with pytest.raises(ValueError, match="page"):
        validate_scenario_yaml("id: x\nquery: q\nsurface: erpnext_panel\npage:\n  screen: watchlist\n")
    validate_scenario_yaml("id: x\nquery: q\nsurface: erpnext_panel\npage:\n  screen: /watchlist\n")


def test_the_screen_scenarios_ship_with_their_page() -> None:
    found = {s["id"]: s for s in load_scenarios(kind="chat") if s["id"] in ("erpnext_020", "erpnext_021")}
    assert found["erpnext_020"]["page"] == {"screen": "/watchlist"}
    assert found["erpnext_021"]["page"] == {"screen": "/"}


def test_any_other_scenario_runs_in_the_web_app(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _run(monkeypatch, {"id": "w", "query": "q"})
    assert not erpnext_panel_turn(seen["session"])
    assert seen["channel_context_block"] == ""


def test_an_unknown_surface_fails_the_scenario() -> None:
    assert scenario_surface({}) is None
    with pytest.raises(ValueError, match="surface"):
        scenario_surface({"surface": "slack"})
    with pytest.raises(ValueError, match="surface"):
        validate_scenario_yaml("id: x\nquery: q\nsurface: slack\n")


def test_the_panel_prompt_names_it_ask_the_dynocortex() -> None:
    for block in (PANEL_CONTEXT_BLOCK, PANEL_DRAFTS_CONTEXT_BLOCK):
        assert '"Ask the Dynocortex" panel' in block
        assert "Ask the Executive" not in block


def test_the_panel_scenario_ships_and_loads_as_chat() -> None:
    found = [s for s in load_scenarios(kind="chat") if s["id"] == "erpnext_006"]
    assert len(found) == 1
    assert scenario_surface(found[0]) == "erpnext_panel"
    assert found[0]["quality_criteria"]["never_calls_the_panel_ask_the_executive"] is True


def test_an_mcp_run_gets_the_running_gateway_and_a_chat_run_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a gateway the Executive offers no MCP tools, so an mcp
    # scenario would be judged on a turn that can't reach ERPNext.
    import asyncio as _asyncio

    from openexecutive.evals import runner
    from openexecutive.orchestrator import executive as executive_module

    given: list[object] = []

    class Recording:
        def __init__(self, mcp_gateway: object = None) -> None:
            given.append(mcp_gateway)

    gateway = object()
    monkeypatch.setattr(executive_module, "Executive", Recording)
    monkeypatch.setattr("openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: gateway)
    queue: _asyncio.Queue[dict[str, object] | None] = _asyncio.Queue()
    for with_mcp in (True, False):
        runner._make_chat_runner(_asyncio.Semaphore(1), queue, [0], 1, None, with_mcp=with_mcp)
    assert given == [gateway, None]


def test_a_principal_speaker_is_the_principal_on_either_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a speaker, whatever is the principal's alone (ERPNext reads,
    # drafts, scheduled metrics) is refused, and the scenario judged on that.
    from types import SimpleNamespace

    from openexecutive.evals import scenarios

    monkeypatch.setattr(scenarios, "_principal", lambda: SimpleNamespace(id=7, email="Owner@Co.example"))
    web, block = scenarios.scenario_turn_surface({"id": "x", "query": "q", "speaker": "principal"})
    assert web == {"from_web_chat": True, "caller_person_id": 7, "web_caller_signed_in": True} and block == ""
    panel, _ = scenarios.scenario_turn_surface(
        {"id": "x", "query": "q", "speaker": "principal", "surface": "erpnext_panel"}
    )
    assert panel["erpnext_panel_email"] == "owner@co.example"
    # No speaker: as before, nobody.
    assert scenarios.scenario_turn_surface({"id": "x", "query": "q"}) == ({}, "")
    # An install with no principal yet: the single-user operator, unsigned.
    monkeypatch.setattr(scenarios, "_principal", lambda: None)
    web, _ = scenarios.scenario_turn_surface({"id": "x", "query": "q", "speaker": "principal"})
    assert web == {"from_web_chat": True, "caller_person_id": None, "web_caller_signed_in": False}


def test_both_runners_hand_chat_the_speaker(monkeypatch: pytest.MonkeyPatch) -> None:
    # chat() sets the turn's caller from person_id: without it a `speaker`
    # scenario's caller is reset to nobody and its ERPNext calls refused.
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    from openexecutive.evals import scenarios

    monkeypatch.setattr(scenarios, "_principal", lambda: SimpleNamespace(id=7, email="owner@co.example"))
    scenario = {"id": "s", "domain": "mcp", "query": "q", "speaker": "principal"}
    assert _run(monkeypatch, scenario)["person_id"] == 7
    script = Path(__file__).resolve().parents[4] / "evals" / "run_evals.py"
    spec = importlib.util.spec_from_file_location("run_evals", script)
    assert spec is not None and spec.loader is not None
    run_evals = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_evals)
    seen: dict[str, Any] = {}

    class FakeExecutive:
        async def chat(self, **kwargs: Any) -> str:
            seen.update(kwargs)
            return "a response"

    asyncio.run(run_evals.run_eval(scenario, FakeExecutive(), None))
    assert seen["person_id"] == 7
    # No speaker: nobody, as before.
    assert _run(monkeypatch, {"id": "w", "query": "q"})["person_id"] is None


# ── the views a panel turn opens (workstream D) ─────────────────────────────


def _done(monkeypatch: pytest.MonkeyPatch, scenario: dict[str, Any], views: list[dict[str, Any]]) -> dict[str, Any]:
    from openexecutive.integrations import erpnext_panel

    async def fake_chat(self: Any, **kwargs: Any) -> str:
        for view in views:
            erpnext_panel.note_erpnext_call("erpnext__show_in_erpnext", {"view": view}, failed=False)
        return "a response"

    async def fake_judge(_scenario: dict[str, Any], _response: str) -> dict[str, Any]:
        return {"overall": 5}

    monkeypatch.setattr(Executive, "__init__", lambda self, mcp_gateway=None: None)
    monkeypatch.setattr(Executive, "chat", fake_chat)
    monkeypatch.setattr(runner_module, "judge_chat", fake_judge)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    run_one = runner_module._make_chat_runner(asyncio.Semaphore(1), queue, [0], 1, None)
    asyncio.run(run_one(0, scenario))
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return next(e for e in events if e["type"] == "scenario_done")


_CHART = {
    "kind": "chart", "doctype": "Sales Invoice", "measure": "sum", "on": "outstanding_amount",
    "over": {"time": "due_date", "every": "week", "from": "2026-11-02", "to": "2027-02-01"},
}


def test_a_panel_scenario_passes_only_when_its_turn_opens_the_expected_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = {"id": "c", "query": "q", "surface": "erpnext_panel", "expected_view_kinds": ["chart"]}
    done = _done(monkeypatch, scenario, [{"kind": "report", "report": "Accounts Receivable"}, _CHART])
    assert done["passed"] is True
    assert done["scores"]["views"] == {"kinds": ["chart", "report"], "missing": [], "forbidden": [], "ok": True}

    done = _done(monkeypatch, scenario, [{"kind": "report", "report": "Accounts Receivable"}])
    assert done["passed"] is False
    assert done["scores"]["views"]["missing"] == ["chart"]


def test_a_forbidden_kind_fails_the_scenario_whatever_the_judge_says(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = {"id": "f", "query": "q", "surface": "erpnext_panel", "forbidden_view_kinds": ["chart"]}
    done = _done(monkeypatch, scenario, [_CHART])
    assert done["passed"] is False and done["scores"]["views"]["forbidden"] == ["chart"]
    assert _done(monkeypatch, scenario, [{"kind": "list", "doctype": "Sales Invoice"}])["passed"] is True
    # A turn that opens nothing opens no chart either.
    assert _done(monkeypatch, scenario, [])["passed"] is True


def test_a_scenario_without_view_kinds_is_scored_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    done = _done(monkeypatch, {"id": "p", "query": "q", "surface": "erpnext_panel"}, [_CHART])
    assert done["passed"] is True and "views" not in done["scores"]


def test_view_kinds_are_a_list_of_names_and_only_for_a_panel_scenario() -> None:
    for key in ("expected_view_kinds", "forbidden_view_kinds"):
        with pytest.raises(ValueError, match=key):
            validate_scenario_yaml(f"id: x\nquery: q\nsurface: erpnext_panel\n{key}: chart\n")
        with pytest.raises(ValueError, match=key):
            validate_scenario_yaml(f"id: x\nquery: q\n{key}:\n  - chart\n")
        validate_scenario_yaml(f"id: x\nquery: q\nsurface: erpnext_panel\n{key}:\n  - chart\n")


def test_a_report_must_open_with_its_expected_filter_names(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = {
        "id": "r", "query": "q", "surface": "erpnext_panel",
        "expected_report_filters": {"Accounts Receivable": ["party_type", "party"]},
    }
    by_party = {
        "kind": "report", "report_name": "Accounts Receivable",
        "filters": {"company": "C", "party_type": "Customer", "party": ["Metro Transit Works"]},
    }
    done = _done(monkeypatch, scenario, [_CHART, by_party])
    assert done["passed"] is True and done["scores"]["views"]["reports_unmet"] == []

    # The report without the party filters, another report with them, or
    # nothing opened (oe_connector refused a 'customer' filter): each fails.
    for views in (
        [{"kind": "report", "report_name": "Accounts Receivable", "filters": {"company": "C"}}],
        [{**by_party, "report_name": "Accounts Payable"}],
        [],
    ):
        done = _done(monkeypatch, scenario, views)
        assert done["passed"] is False
        assert done["scores"]["views"]["reports_unmet"] == ["Accounts Receivable"]


def test_expected_report_filters_name_reports_and_filters_and_only_for_a_panel_scenario() -> None:
    good = "expected_report_filters:\n  Accounts Receivable:\n  - party\n"
    validate_scenario_yaml(f"id: x\nquery: q\nsurface: erpnext_panel\n{good}")
    with pytest.raises(ValueError, match="expected_report_filters"):
        validate_scenario_yaml(f"id: x\nquery: q\n{good}")
    for bad in ("expected_report_filters: party\n", "expected_report_filters:\n  Accounts Receivable: party\n"):
        with pytest.raises(ValueError, match="expected_report_filters"):
            validate_scenario_yaml(f"id: x\nquery: q\nsurface: erpnext_panel\n{bad}")


def test_the_cash_outlook_follow_up_opens_receivables_by_party() -> None:
    found = next(s for s in load_scenarios() if s["id"] == "erpnext_094")
    assert found["expected_report_filters"] == {"Accounts Receivable": ["party_type", "party"]}
    assert scenario_surface(found) == "erpnext_panel"


def test_the_chart_scenarios_ship_with_their_view_kinds() -> None:
    found = {s["id"]: s for s in load_scenarios() if s["id"] in ("erpnext_054", "erpnext_055")}
    assert found["erpnext_054"]["expected_view_kinds"] == ["chart"]
    assert found["erpnext_055"]["forbidden_view_kinds"] == ["chart"]
    assert all(scenario_surface(s) == "erpnext_panel" for s in found.values())
