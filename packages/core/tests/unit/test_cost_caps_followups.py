"""The cost caps' follow-ups (audit.cost_caps, after ladislav-urban/OpenExecutive#197):
the onboarding research's own budget, no exception text on a run's record
or stream, a resumed rerun spent for whoever tapped it, and a reply whose
check a cap refused saying its figures weren't checked."""
from __future__ import annotations

import ast
import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from openexecutive.audit import cost_caps
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session

PACKAGE = Path(cost_caps.__file__).resolve().parents[1]


def _cap(scope: str = "total", key: str = "all") -> cost_caps.CostCapReached:
    return cost_caps.CostCapReached(scope, key, 1.0, 1.0, datetime.now(UTC))  # type: ignore[arg-type]


# 1. Onboarding research ------------------------------------------------------


def test_the_onboarding_research_runs_under_its_own_job_and_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes import onboarding

    seen: list[Any] = []

    async def research(session_id: str) -> None:
        seen.append((cost_caps.current_job(), cost_caps.current_turn()))

    monkeypatch.setattr(onboarding, "_post_onboarding_research", research)
    asyncio.run(onboarding._fire_post_onboarding_research("s-1"))
    (job, turn), = seen
    assert job == "post-onboarding-research" and turn is not None and turn.closed


# 2. A run's record ------------------------------------------------------------


def test_a_failed_runs_record_never_holds_the_exceptions_text() -> None:
    from openexecutive.workflows.persistence import RUN_FAILED_TEXT, run_failure_text

    assert run_failure_text(RuntimeError("/home/sara/secret.db is locked")) == RUN_FAILED_TEXT
    cap = _cap()
    assert run_failure_text(RuntimeError("wrapped")) == RUN_FAILED_TEXT
    try:
        raise RuntimeError("outer") from cap
    except RuntimeError as wrapped:
        assert run_failure_text(wrapped) == cap.reply()


def _fail_run_messages_tainted_by_an_exception(tree: ast.Module) -> list[int]:
    """Lines of every fail_run call in ``tree`` whose message can carry an
    exception's text: a name bound by ``except ... as`` reaching it other
    than through ``run_failure_text``, directly, through a local assigned
    from one, or (one hop) through a parameter a caller in the module fills
    from one."""
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]

    def enclosing(node: ast.AST) -> Any:
        best = None
        for fn in functions:
            within = fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno)
            if within and (best is None or fn.lineno >= best.lineno):
                best = fn
        return best

    def exc_names(fn: Any) -> set[str]:
        return {h.name for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler) and h.name}

    def tainted(expr: ast.AST, names: set[str]) -> bool:
        if isinstance(expr, ast.Call) and getattr(expr.func, "id", getattr(expr.func, "attr", "")) == "run_failure_text":
            return False
        if isinstance(expr, ast.Name) and expr.id in names:
            return True
        return any(tainted(child, names) for child in ast.iter_child_nodes(expr))

    def assigned(fn: Any, name: str) -> list[ast.AST]:
        values: list[ast.AST] = []
        for node in ast.walk(fn):
            plain = isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets)
            other = (
                isinstance(node, ast.AnnAssign | ast.AugAssign)
                and getattr(node.target, "id", None) == name
                and node.value is not None
            )
            if plain or other:
                values.append(node.value)  # type: ignore[union-attr]
        return values

    def bad(expr: ast.AST, fn: Any, depth: int = 0) -> bool:
        if fn is None:
            return False
        names = exc_names(fn)
        if tainted(expr, names):
            return True
        if isinstance(expr, ast.Name):
            if any(bad(v, fn, depth) for v in assigned(fn, expr.id) if v is not expr):
                return True
            params = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]]
            if expr.id in params and depth < 2:
                index = params.index(expr.id)
                for call in ast.walk(tree):
                    if isinstance(call, ast.Call) and getattr(call.func, "id", getattr(call.func, "attr", "")) == fn.name:
                        args = [a for a in call.args]
                        arg = args[index] if index < len(args) else next(
                            (k.value for k in call.keywords if k.arg == expr.id), None,
                        )
                        if arg is not None and bad(arg, enclosing(call), depth + 1):
                            return True
        return False

    lines = []
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "id", getattr(call.func, "attr", "")) == "fail_run":
            message = call.args[1] if len(call.args) > 1 else next(
                (k.value for k in call.keywords if k.arg in ("error", "reason")), None,
            )
            if message is not None and bad(message, enclosing(call)):
                lines.append(call.lineno)
    return lines


def test_no_run_record_is_written_from_an_exceptions_text() -> None:
    # Every fail_run in the package passes fixed text (or run_failure_text),
    # however the message reaches it.
    offenders = [
        f"{path.relative_to(PACKAGE)}:{line}"
        for path in PACKAGE.rglob("*.py")
        for line in _fail_run_messages_tainted_by_an_exception(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []


def test_the_guard_finds_a_message_that_reaches_fail_run_through_a_variable_or_a_parameter() -> None:
    source = (
        "def a(run_id):\n"
        "    try:\n        x()\n"
        "    except Exception as exc:\n        msg = str(exc)\n        fail_run(run_id, msg)\n"
        "def _abandon(run_id, reason):\n    fail_run(run_id, reason)\n"
        "def b(run_id):\n"
        "    try:\n        x()\n"
        "    except Exception as exc:\n        _abandon(run_id, f'bad: {exc}')\n"
        "def c(run_id):\n"
        "    try:\n        x()\n"
        "    except Exception as exc:\n        fail_run(run_id, run_failure_text(exc))\n"
    )
    assert sorted(_fail_run_messages_tainted_by_an_exception(ast.parse(source))) == [6, 8]


def test_the_run_route_stores_the_fixed_sentence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import create_model

    from openexecutive.api.routes import workflows as workflows_route
    from openexecutive.workflows import persistence
    from openexecutive.workflows.base import WorkflowStepDef

    class _Failing:
        name = "failing"
        title = "Failing"

        def input_model(self) -> Any:
            return create_model("_In", topic=(str, ""))

        def steps(self) -> list[Any]:
            return [WorkflowStepDef(id="s", title="Step", description="Fails.")]

        async def run(self, inputs: Any, store: Any) -> Any:
            raise RuntimeError("Sara's secret path /etc/x")
            yield  # pragma: no cover

    db = tmp_path / "runs.db"
    persistence.initialize_runs_db(db)
    stored: list[str] = []
    monkeypatch.setattr(workflows_route, "get_workflow", lambda name: _Failing())
    monkeypatch.setattr(workflows_route, "_caller_person_id", lambda request: None)
    monkeypatch.setattr(workflows_route, "refuse_principal_only_run", lambda *a, **k: None)
    monkeypatch.setattr(workflows_route, "create_run", lambda **kw: persistence.create_run(db_path=db, **kw))
    monkeypatch.setattr(workflows_route, "fail_run", lambda **kw: stored.append(kw["error"]))
    app = FastAPI()
    app.include_router(workflows_route.router)
    app.state.store = object()
    with TestClient(app) as client:
        body = client.post("/workflows/failing/runs", json={"topic": "x"}).text
    assert stored == [persistence.RUN_FAILED_TEXT]
    assert "secret" not in body


# 3. Workflow error events -------------------------------------------------------


@pytest.mark.parametrize("module", [
    "workflows/end_of_day_digest.py",
    "workflows/morning_brief.py",
    "workflows/executive_reflection.py",
])
def test_these_workflows_never_put_an_exceptions_text_in_an_error_event(module: str) -> None:
    source = (PACKAGE / module).read_text(encoding="utf-8")
    assert not re.search(r'type="error",\s*message=(f"[^"]*\{exc\}|str\(exc\))', source)


def test_the_engagement_reports_context_errors_are_its_own_words() -> None:
    # Its error event passes a _ContextError's text, which is now always
    # written by the report itself, never a slot's own exception.
    source = (PACKAGE / "workflows/engagement_value_report.py").read_text(encoding="utf-8")
    assert "_ContextError(str(exc))" not in source


def test_a_failed_synthesis_says_fixed_text_or_the_caps_own() -> None:
    from openexecutive.workflows import end_of_day_digest, morning_brief

    for module in (morning_brief, end_of_day_digest):
        assert "secret" not in module._synthesis_failed(RuntimeError("secret"))
        assert module._synthesis_failed(_cap()) == _cap().reply()


# 4. A rerun resumed after held writes --------------------------------------------


def test_a_resumed_rerun_is_spent_for_whoever_tapped_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from openexecutive.workflows import persistence, resumer

    db = tmp_path / "runs.db"
    persistence.initialize_runs_db(db)
    # A team workflow's scheduled run: no owner.
    persistence.create_run("r1", "team_wf", "t", {}, db_path=db)
    seen: list[Any] = []

    async def drive(row: dict, claim: str, db_path: Any = None) -> bool:
        seen.append((cost_caps._spender.get(), cost_caps.current_turn()))
        return True

    monkeypatch.setattr(resumer, "_drive_resume", drive)
    payload = json.dumps({"spent_for_person_id": 7})
    asyncio.run(resumer._execute_resume({"run_id": "r1", "resume_state_json": payload}, "c", db_path=db))
    assert seen[0][0] == "7" and seen[0][1] is not None
    # None on the payload: the creator, as a cadence's own run.
    seen.clear()
    asyncio.run(resumer._execute_resume({"run_id": "r1", "resume_state_json": "{}"}, "c", db_path=db))
    assert seen[0][0] is None


def test_a_rerun_paused_on_held_writes_keeps_its_starter_on_the_payload() -> None:
    from openexecutive.workflows.wait_for_human import WorkflowResumeState

    state = WorkflowResumeState(workflow_name="w", gate_step_id="g", gate_step_index=0, next_step_index=1)
    assert state.spent_for_person_id is None
    runner = (PACKAGE / "scheduler/runner.py").read_text(encoding="utf-8")
    assert 'event.resume_state.spent_for_person_id = getattr(action, "created_by_person_id", None)' in runner


# 7. A check a cap refused after the reply streamed -----------------------------


def test_a_shown_reply_whose_check_was_capped_says_its_figures_werent_checked() -> None:
    cap = _cap("person", "4")
    with cost_caps.bind_turn(fresh=True):
        reply, chunk = Executive._capped_check("North owes $12,900.", cap, "s")
        assert cost_caps.turn_stopped()
    assert chunk == f"\n\n{cost_caps.UNCHECKED_FIGURES} {cap.reply()}"
    assert reply.startswith("North owes $12,900.")
    # After a stop sentence too: it says nothing of the figures.
    with cost_caps.bind_turn(fresh=True):
        cost_caps.mark_stopped(cost_caps.TURN_STOP_REPLY)
        _, after_stop = Executive._capped_check(f"North owes $12,900.\n\n{cost_caps.TURN_STOP_REPLY}", cap, "s")
    assert cost_caps.UNCHECKED_FIGURES in str(after_stop)


def test_someone_off_people_hears_only_that_the_figures_werent_checked() -> None:
    stranger = Session(origin_channel="email")
    stranger.sender_off_team = True  # type: ignore[attr-defined]
    with set_session(stranger):
        cap = _cap("total", "all")
        assert cap.unchecked_reply() == cost_caps.UNCHECKED_FIGURES
    assert "usage limit" not in cost_caps.UNCHECKED_FIGURES


# F2/F1. The resumer -------------------------------------------------------------


def test_a_second_held_writes_pause_keeps_the_starter() -> None:
    source = (PACKAGE / "workflows/resumer.py").read_text(encoding="utf-8")
    assert "event.resume_state.spent_for_person_id = state.spent_for_person_id" in source


def test_a_resumed_run_through_two_pauses_stays_spent_for_its_starter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from openexecutive.workflows import persistence, resumer
    from openexecutive.workflows.wait_for_human import WaitForHumanEvent, WorkflowResumeState

    db = tmp_path / "runs.db"
    persistence.initialize_runs_db(db)
    persistence.create_run("r1", "team_wf", "t", {}, db_path=db)
    first = WorkflowResumeState(
        workflow_name="team_wf", gate_step_id="g", gate_step_index=0, next_step_index=1,
        kind="held_writes", spent_for_person_id=7,
    )
    checkpoints: list[Any] = []

    async def checkpoint(*, event: Any, **_k: Any) -> None:
        checkpoints.append(event.resume_state)

    class _Workflow:
        title = "Team"

        def input_model(self) -> Any:
            from pydantic import create_model

            return create_model("_In")

        async def resume(self, **_k: Any) -> Any:
            # The second segment pauses on held writes again.
            yield WaitForHumanEvent(
                person_id=1, question="Approve?",
                resume_state=WorkflowResumeState(
                    workflow_name="team_wf", gate_step_id="g2", gate_step_index=1, next_step_index=2,
                    kind="held_writes",
                ),
            )

    monkeypatch.setattr("openexecutive.workflows.gate.checkpoint_gate", checkpoint)
    monkeypatch.setattr("openexecutive.workflows.get_workflow", lambda name: _Workflow())
    monkeypatch.setattr("openexecutive.workflows.dynamic.DynamicWorkflow", _Workflow)
    monkeypatch.setattr("openexecutive.knowledge.store.ChromaDBStore", lambda **_k: object())
    row = {"run_id": "r1", "workflow_name": "team_wf", "title": "t", "inputs": {},
           "resume_state_json": first.model_dump_json(), "resolution_json": None}
    asyncio.run(resumer._drive_resume(row, "claim", db_path=db))
    (second,) = checkpoints
    # The third segment resumes from this payload: still spent for 7.
    assert second.spent_for_person_id == 7
    assert resumer._spent_for({"resume_state_json": second.model_dump_json()}) == 7


def test_inputs_that_no_longer_validate_record_no_exception_text() -> None:
    source = (PACKAGE / "workflows/resumer.py").read_text(encoding="utf-8")
    assert "no longer validate: {exc}" not in source


def test_run_workflows_crash_reaches_neither_record_nor_model(monkeypatch: pytest.MonkeyPatch) -> None:
    source = (PACKAGE / "orchestrator/workflow_run_tools.py").read_text(encoding="utf-8")
    assert "last_error = run_failure_text(exc)" in source and "str(exc)[:" not in source
