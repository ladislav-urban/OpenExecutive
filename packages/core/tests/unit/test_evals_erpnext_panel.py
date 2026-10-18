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

    monkeypatch.setattr(Executive, "__init__", lambda self: None)
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
