"""A ``type: watch_reason`` scenario runs a watch job's model "reason" line
through the job's own reason step (watch/runner.py ``reason_lines``) on
findings the job's own code builds, with the model and the judge stubbed.

No shipped job has one today (project status writes its sentence in code),
so these tests list a test job with a reason step and its finding builder."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.evals import runner as runner_module
from openexecutive.evals import scenarios as scenarios_module
from openexecutive.evals.runner import run_watch_reason_scenario
from openexecutive.evals.scenarios import (
    WATCH_REASON,
    load_scenarios,
    scenario_watch_reason,
    validate_scenario_yaml,
)
from openexecutive.watch import registry
from openexecutive.watch import runner as watch_runner
from openexecutive.watch.jobs import _example

JOB_ID = "reason-test"
INSTRUCTION = "Say in one sentence how this item stands, in words only."
JOB = dataclasses.replace(
    _example.JOB, id=JOB_ID,
    reason_schema=registry.ReasonSchema(INSTRUCTION, max_findings=10, no_digits=True, per_finding=True),
)

LATE = {"project": "Riverside fit-out", "update": "The flooring came late; another crew is on site."}
ON_PLAN = {"project": "Harbour office refresh", "update": "Moving as planned."}


def _finding(raw: dict[str, Any], _today: Any) -> registry.CardSpec:
    """The test job's finding: its name and the words it was given."""
    name, update = raw.get("project"), raw.get("update")
    if not isinstance(name, str) or not isinstance(update, str) or not update:
        raise ValueError("`project` and `update`: text")
    return registry.CardSpec(key=f"item:{name}", summary=name, values=(("Item", name),),
                             facts={"the item's words": update})


@pytest.fixture(autouse=True)
def listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test job, listed with a reason step and its finding builder."""
    monkeypatch.setitem(registry._JOBS, JOB_ID, JOB)
    monkeypatch.setitem(scenarios_module._WATCH_FINDINGS, JOB_ID, _finding)


def _scenario(*findings: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"id": "w", "type": WATCH_REASON, WATCH_REASON: {"job": JOB_ID, "findings": list(findings)},
            **extra}


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The fast model the reason step calls: each call's line is the first
    of ``say`` whose key its turn holds. Records each call."""
    state = SimpleNamespace(say={}, calls=[])

    async def create(**kwargs: Any) -> Any:
        state.calls.append(kwargs)
        turn = kwargs["messages"][0]["content"]
        line = next((text for key, text in state.say.items() if key in turn), "")
        reply = json.dumps({"reasons": {"f1": line}} if line else {"reasons": {}})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=50, output_tokens=10))

    monkeypatch.setattr("openexecutive.agents.utility_fast.get_fast_model", lambda: "claude-test")
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: SimpleNamespace(messages_create=create))
    monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *_a, **_k: None)
    return state


@pytest.fixture
def judged(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_judge(_scenario: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        seen.append(outcome)
        return {"overall": 5}

    monkeypatch.setattr(runner_module, "judge_watch_reason", fake_judge)
    return seen


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_a_finding_is_built_by_the_jobs_own_builder() -> None:
    case = scenario_watch_reason(_scenario(LATE))
    assert case is not None and case.job is JOB
    [finding] = case.findings
    assert finding.label == "Riverside fit-out" and finding.expect_line is None
    assert dict(finding.spec.facts) == {"the item's words": LATE["update"]}


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ("watch_reason: x", "mapping"),
        ("watch_reason: {job: collections, findings: []}", "job must be one of"),
        ("watch_reason: {job: project-status, findings: [{project: A, update: u}]}", "job must be one of"),
        ("watch_reason: {job: reason-test, findings: []}", "1 to 10"),
        ("watch_reason: {job: reason-test, findings: [x]}", "mapping"),
        ("watch_reason: {job: reason-test, findings: [{project: A}]}", "update"),
        ("watch_reason: {job: reason-test, findings: [{project: A, update: u, expect_line: yes please}]}",
         "expect_line"),
        ("watch_reason: {job: reason-test, findings: [{project: A, update: u}, {project: A, update: v}]}",
         "its own project"),
    ],
)
def test_a_malformed_block_fails_validation(block: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_scenario_yaml(f"id: w\ntype: watch_reason\n{block}\n")


def test_a_watch_reason_scenario_needs_its_block() -> None:
    with pytest.raises(ValueError, match="`watch_reason` block"):
        validate_scenario_yaml("id: w\ntype: watch_reason\n")


def test_no_shipped_job_has_a_model_line_to_evaluate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Project status writes its sentence in code, so its scenarios are retired.
    assert load_scenarios(kind=WATCH_REASON) == []
    monkeypatch.delitem(scenarios_module._WATCH_FINDINGS, JOB_ID)
    assert scenarios_module._WATCH_FINDINGS == {}
    project_status = registry.get("project-status")
    assert project_status is not None and project_status.reason_schema is None


# --------------------------------------------------------------------------- #
# The run: the job's own reason step
# --------------------------------------------------------------------------- #


def test_a_scenario_runs_the_jobs_reason_step_one_call_per_finding(
    model: SimpleNamespace, judged: list[dict[str, Any]],
) -> None:
    model.say = {"flooring": "Behind and over its estimate after the late flooring.",
                 "Moving as planned": "Running to plan with nothing outstanding."}
    result = asyncio.run(run_watch_reason_scenario(_scenario(LATE, ON_PLAN)))
    assert result["passed"] is True
    # The runner's constant system prompt and the job's instruction, each
    # call shown its own finding alone.
    assert len(model.calls) == 2
    for call in model.calls:
        assert call["system"] == watch_runner.REASON_SYSTEM
        assert call["max_tokens"] == watch_runner.PER_FINDING_MAX_TOKENS
        assert INSTRUCTION in call["messages"][0]["content"]
    turns = [c["messages"][0]["content"] for c in model.calls]
    assert sum("flooring" in t for t in turns) == 1 and sum("Moving as planned" in t for t in turns) == 1
    assert not any("flooring" in t and "Moving as planned" in t for t in turns)
    [outcome] = judged
    assert outcome["job"] == JOB_ID
    assert [(f["finding"], f["line"], f["dropped"]) for f in outcome["findings"]] == [
        ("Riverside fit-out", "Behind and over its estimate after the late flooring.", None),
        ("Harbour office refresh", "Running to plan with nothing outstanding.", None),
    ]
    assert outcome["findings"][0]["facts"] == {"the item's words": LATE["update"]}


def test_a_line_with_a_number_is_dropped_and_shown_only_as_dropped(
    model: SimpleNamespace, judged: list[dict[str, Any]],
) -> None:
    model.say = {"flooring": "Twelve days late after the flooring."}
    result = asyncio.run(run_watch_reason_scenario(_scenario({**LATE, "expect_line": True})))
    [finding] = judged[0]["findings"]
    assert finding["line"] is None and finding["dropped"] == "Twelve days late after the flooring."
    # The judge passed it, but the scenario wanted a line on the notice.
    assert result["passed"] is False and result["scores"]["expect_line_unmet"] == ["Riverside fit-out"]


def test_no_line_passes_when_the_scenario_allows_it(model: SimpleNamespace, judged: list[dict[str, Any]]) -> None:
    result = asyncio.run(run_watch_reason_scenario(_scenario(LATE)))
    assert judged[0]["findings"][0]["line"] is None and judged[0]["findings"][0]["dropped"] is None
    assert result["passed"] is True


def test_a_forbidden_word_in_a_kept_line_fails_the_scenario(
    model: SimpleNamespace, judged: list[dict[str, Any]],
) -> None:
    model.say = {"flooring": "The project is on time and within budget."}
    result = asyncio.run(run_watch_reason_scenario(_scenario(LATE, forbidden_reply_words=["on time"])))
    assert result["passed"] is False and result["scores"]["forbidden_reply_words"] == ["on time"]


def test_a_dropped_line_never_counts_as_said(model: SimpleNamespace, judged: list[dict[str, Any]]) -> None:
    # The injected claim came with a number, so the filter dropped it: it
    # never reaches the notice.
    model.say = {"flooring": "On time: zero days late."}
    result = asyncio.run(run_watch_reason_scenario(_scenario(LATE, forbidden_reply_words=["on time"])))
    assert result["passed"] is True and "forbidden_reply_words" not in result["scores"]


def test_the_package_runner_streams_the_kind(model: SimpleNamespace, judged: list[dict[str, Any]]) -> None:
    model.say = {"flooring": "Behind after the late flooring."}

    async def go() -> list[dict[str, Any]]:
        return [e async for e in runner_module.run_scenarios(kind=WATCH_REASON, scenarios=[_scenario(LATE)])]

    events = asyncio.run(go())
    done = next(e for e in events if e["type"] == "scenario_done")
    assert done["passed"] is True
    assert done["outcome"]["findings"][0]["line"] == "Behind after the late flooring."
    assert events[-1] == {"type": "suite_done", "kind": WATCH_REASON, "passed": 1, "total": 1}


def test_reason_step_still_returns_the_kept_lines(model: SimpleNamespace) -> None:
    case = scenario_watch_reason(_scenario(LATE, ON_PLAN))
    assert case is not None
    model.say = {"flooring": "Behind after the late flooring.", "Moving as planned": "Two weeks ahead."}
    specs = [f.spec for f in case.findings]
    kept = asyncio.run(watch_runner.reason_step(JOB, specs))
    assert kept == {specs[0].key: "Behind after the late flooring."}
    assert asyncio.run(watch_runner.reason_lines(JOB, specs)) == (
        {specs[0].key: "Behind after the late flooring."}, {specs[1].key: "Two weeks ahead."},
    )


def test_a_line_dropped_for_its_length_is_recorded_as_too_long(
    model: SimpleNamespace, judged: list[dict[str, Any]],
) -> None:
    # watch_project_status_001 at a735e38c: a line past the limit was lost
    # with no record of why (dropped None, rule None).
    long = "Behind schedule and over its estimate, " + "with the snag list still being worked through " * 6
    model.say = {"flooring": long}
    result = asyncio.run(run_watch_reason_scenario(_scenario(LATE)))
    finding = result["outcome"]["findings"][0]
    assert finding["line"] is None
    assert finding["dropped_rule"] == "too_long"
    # Kept cleaned and capped in the record: never the model's raw text.
    assert finding["dropped"] == watch_runner.clean(long, 2 * watch_runner.MAX_REASON_CHARS)
    assert len(finding["dropped"]) <= 2 * watch_runner.MAX_REASON_CHARS

