"""The deterministic checks every eval runner holds a reply to, whatever the
judge's score (``scenarios.reply_check_failures``): ``forbidden_reply_words``,
``forbid_repeated_text`` and ``reply_must_name``. The package runner
(``evals.runner``) and the repo's ``evals/run_evals.py`` apply the same ones
through ``runner.chat_checks``, so both fail the same replies.

Found Nov 17, 2026 (where-is-my-order step 4, erpnext_098): two of three
replies said the Installation Note "covers every line" over a table of three
of the four lines, and the judge passed them at an overall 4; every reply
named ERPNext, which ``run_evals.py --mcp`` never checked."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from openexecutive.evals import runner as runner_module
from openexecutive.evals.scenarios import (
    load_scenarios,
    reply_check_failures,
    reply_must_name_missing,
    validate_scenario_yaml,
)
from openexecutive.orchestrator.executive import Executive

ITEMS = ["Circuit breaker 32A|DEMO-BRK-32", "Motion sensor", "Panel wiring|DEMO-WIR-25", "Sub-metering panel"]
# Reps 1 and 3's tables, shortened.
THREE_ROWS = (
    "Yes. The Installation Note covers every line:\n\n| Item | Delivered | Installed |\n|---|---|---|\n"
    "| Circuit breaker 32A | 48 | 48 |\n| Motion sensor | 18 | 18 |\n| Sub-metering panel, made to order | 1 | 1 |"
)
FOUR_ROWS = (
    "Yes. Everything delivered was installed:\n\n- Circuit breaker 32A: 48 delivered, 48 installed\n"
    "- Motion sensor: 18 delivered, 18 installed\n- Panel wiring: 300 m delivered, 300 m installed\n"
    "- Sub-metering panel, made to order: 1 delivered, 1 installed"
)


# ── reply_must_name ────────────────────────────────────────────────────────


def test_an_item_left_out_is_missing_and_one_named_any_way_is_there() -> None:
    scenario = {"reply_must_name": ITEMS}
    assert reply_must_name_missing(scenario, THREE_ROWS) == ["Panel wiring|DEMO-WIR-25"]
    assert reply_must_name_missing(scenario, FOUR_ROWS) == []
    # Any case and spacing; an alternative (the item code) counts too.
    assert reply_must_name_missing(scenario, THREE_ROWS + "\n| DEMO-WIR-25 | 300 | 300 |") == []
    assert reply_must_name_missing(scenario, THREE_ROWS.upper() + " panel   WIRING") == []
    assert reply_must_name_missing({}, "anything") == []


@pytest.mark.parametrize(
    "block",
    [
        "reply_must_name: Panel wiring",
        "reply_must_name: []",
        "reply_must_name: ['']",
        "reply_must_name: ['  ']",
        "reply_must_name: [3]",
        "reply_must_name: ['Panel wiring|']",
        "reply_must_name: ['|DEMO-WIR-25']",
    ],
)
def test_a_malformed_list_fails_validation(block: str) -> None:
    with pytest.raises(ValueError, match="reply_must_name"):
        validate_scenario_yaml(f"id: x\nquery: q\n{block}\n")


def test_a_list_of_names_validates() -> None:
    assert validate_scenario_yaml("id: x\nquery: q\nreply_must_name: [Motion sensor, 'Panel wiring|DEMO-WIR-25']\n")


def test_every_check_is_keyed_as_the_runs_record_it() -> None:
    scenario = {
        "forbidden_reply_words": ["ERPNext"],
        "forbid_repeated_text": True,
        "reply_must_name": ["Panel wiring"],
    }
    reply = THREE_ROWS + "\n\nThis ERPNext user has no permission on it. This ERPNext user has no permission on it."
    failures = reply_check_failures(scenario, reply)
    assert failures["forbidden_reply_words"] == ["ERPNext"]
    assert failures["reply_must_name_missing"] == ["Panel wiring"]
    assert failures["repeated_text"]
    # Text that isn't one reply (a watch notice's lines) skips the repeat check.
    assert "repeated_text" not in reply_check_failures(scenario, reply, repeated=False)
    assert reply_check_failures(scenario, FOUR_ROWS + " Panel wiring") == {}


# ── the package runner ─────────────────────────────────────────────────────


def _chat(monkeypatch: pytest.MonkeyPatch, scenario: dict[str, Any], reply: str) -> dict[str, Any]:
    async def fake_chat(self: Any, **kwargs: Any) -> str:
        return reply

    async def fake_judge(_scenario: dict[str, Any], _response: str) -> dict[str, Any]:
        return {"overall": 5}

    monkeypatch.setattr(Executive, "__init__", lambda self, mcp_gateway=None: None)
    monkeypatch.setattr(Executive, "chat", fake_chat)
    monkeypatch.setattr(runner_module, "judge_chat", fake_judge)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    asyncio.run(runner_module._make_chat_runner(asyncio.Semaphore(1), queue, [0], 1, None)(0, scenario))
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    (done,) = [e for e in events if e["type"] == "scenario_done"]
    return done


def test_the_package_runner_fails_a_reply_that_leaves_an_item_out(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = {"id": "c", "query": "q", "reply_must_name": ITEMS}
    done = _chat(monkeypatch, scenario, THREE_ROWS)
    assert done["passed"] is False
    assert done["scores"]["reply_must_name_missing"] == ["Panel wiring|DEMO-WIR-25"]
    assert _chat(monkeypatch, scenario, FOUR_ROWS)["passed"] is True


def test_the_supplier_runner_holds_reply_must_name_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.integrations import erpnext_a2a_agent

    from .test_evals_supplier import SCENARIO

    async def fake_answer(supplier: str, message: str, call: Any, **_k: Any) -> Any:
        return erpnext_a2a_agent.SupplierAnswer("Your order is open.", [])

    async def fake_judge(_scenario: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        return {"overall": 5}

    monkeypatch.setattr(erpnext_a2a_agent, "answer", fake_answer)
    monkeypatch.setattr(runner_module, "judge_supplier", fake_judge)
    result = asyncio.run(
        runner_module.run_supplier_scenario({**SCENARIO, "reply_must_name": ["PUR-ORD-2026-00012"]})
    )
    assert result["passed"] is False
    assert result["scores"]["reply_must_name_missing"] == ["PUR-ORD-2026-00012"]


# ── evals/run_evals.py ─────────────────────────────────────────────────────


def _run_evals() -> ModuleType:
    script = Path(__file__).resolve().parents[4] / "evals" / "run_evals.py"
    spec = importlib.util.spec_from_file_location("run_evals_reply_checks", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Replying:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.product: str | None = None

    async def chat(self, **kwargs: Any) -> str:
        from openexecutive.utils.product_name import configured_product_name

        self.product = configured_product_name()
        return self.reply


def _evaluate(monkeypatch: pytest.MonkeyPatch, scenario: dict[str, Any], executive: Any) -> dict[str, Any]:
    run_evals = _run_evals()

    async def judge_passes(_scenario: dict[str, Any], _response: str, drafts: Any = None) -> dict[str, Any]:
        return {"overall": 5, "drafts_seen": drafts}

    monkeypatch.setattr(run_evals, "judge_response", judge_passes)
    result: dict[str, Any] = asyncio.run(run_evals.evaluate_chat(scenario, executive))
    return result


def test_run_evals_fails_a_reply_with_a_forbidden_word_the_judge_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = {"id": "m", "domain": "mcp", "query": "q", "forbidden_reply_words": ["ERPNext"]}
    result = _evaluate(monkeypatch, scenario, _Replying("This ERPNext user has no permission on the project."))
    assert result["passed"] is False
    assert result["scores"]["forbidden_reply_words"] == ["ERPNext"]
    assert "turn" not in result  # what's written to the results file
    passed = _evaluate(monkeypatch, scenario, _Replying("Your own access doesn't include the project."))
    assert passed["passed"] is True


def test_run_evals_fails_a_reply_that_leaves_an_item_out(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = {"id": "m", "domain": "mcp", "query": "q", "reply_must_name": ITEMS}
    result = _evaluate(monkeypatch, scenario, _Replying(THREE_ROWS))
    assert result["passed"] is False
    assert result["scores"]["reply_must_name_missing"] == ["Panel wiring|DEMO-WIR-25"]
    assert _run_evals().failed_checks(result["scores"]) == ["reply_must_name_missing"]


def test_run_evals_gives_the_turn_the_scenarios_product_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Before, it never set it: the panel's block named no product, and the
    # reply named ERPNext.
    from openexecutive.utils.product_name import configured_product_name

    executive = _Replying("ok")
    _evaluate(monkeypatch, {"id": "m", "domain": "mcp", "query": "q", "product_name": "Dynocortex"}, executive)
    assert executive.product == "Dynocortex"
    assert configured_product_name() != "Dynocortex"  # for that turn only


def test_run_evals_hands_the_judge_an_act_as_me_turns_drafts(monkeypatch: pytest.MonkeyPatch) -> None:
    drafts = [{"to": ["ana@co.example"], "subject": "Re: invoice", "body": "Paid on Friday."}]

    async def turn(_executive: Any, scenario: dict[str, Any]) -> runner_module.ChatTurn:
        return runner_module.ChatTurn(query="q", response="Saved a draft.", drafts=drafts)

    monkeypatch.setattr(runner_module, "run_chat_turn", turn)
    result = _evaluate(monkeypatch, {"id": "d", "domain": "chat", "query": "q"}, _Replying("unused"))
    assert result["scores"]["drafts_seen"] == drafts
    # And its judge's prompt shows them, as judge_chat's does.
    from openexecutive.evals.judges import chat_judge_context

    assert "Paid on Friday." in chat_judge_context({"delegation": {}}, drafts)


def test_both_judges_see_a_panel_scenarios_criteria() -> None:
    from openexecutive.evals.judges import chat_judge_context

    panel = {"id": "p", "query": "q", "surface": "erpnext_panel", "quality_criteria": {"lists_every_line": True}}
    assert "lists every line" in chat_judge_context(panel)
    # Any other scenario's prompt is unchanged.
    assert chat_judge_context({"id": "w", "query": "q", "quality_criteria": {"lists_every_line": True}}) == ""


# ── erpnext_098 ────────────────────────────────────────────────────────────


def test_the_where_is_my_order_scenario_holds_every_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals import scenarios

    monkeypatch.setattr(scenarios, "_principal", lambda: None)  # forbid_principal_name reads the roster
    (scenario,) = [s for s in load_scenarios(kind="mcp") if s["id"] == "erpnext_098"]
    assert scenario["surface"] == "erpnext_panel" and scenario["demo_viewer"] is True
    assert scenario["forbidden_reply_words"] == ["ERPNext"]
    assert reply_must_name_missing(scenario, THREE_ROWS) == ["Panel wiring|DEMO-WIR-25"]
    assert reply_check_failures(scenario, FOUR_ROWS) == {}


# ── the principal's name, apart from the scenario's own words ─────────────

PRINCIPAL = "Rhoda Principal"


@pytest.fixture
def principal(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from openexecutive.evals import scenarios

    monkeypatch.setattr(scenarios, "_principal", lambda: SimpleNamespace(full_name=PRINCIPAL))


# A sweep reported "Rachel", a demo-fixture name in erpnext_064's own list,
# under the same key as a principal leak: the two now have a key each.
NAMED = {"forbidden_reply_words": ["Rachel"], "forbid_principal_name": True}


@pytest.mark.usefixtures("principal")
def test_a_fixture_name_and_the_principals_name_have_a_key_each() -> None:
    assert reply_check_failures(NAMED, "Rachel raises them.") == {"forbidden_reply_words": ["Rachel"]}
    leak = reply_check_failures(NAMED, "Rhoda raises them.")
    assert leak == {"principal_name": ["the principal's name appeared in the reply"]}
    both = reply_check_failures(NAMED, "Rachel drafts them and Rhoda Principal approves.")
    assert both["forbidden_reply_words"] == ["Rachel"] and both["principal_name"]
    # The name is never recorded, whatever path records it.
    assert "Rhoda" not in repr(both)
    assert "Rhoda" not in repr(reply_check_failures(NAMED, "Rhoda: on time.", repeated=False))  # a watch notice
    # Without forbid_principal_name the principal may be named.
    assert reply_check_failures({"forbidden_reply_words": ["Rachel"]}, "Rhoda raises them.") == {}


@pytest.mark.usefixtures("principal")
def test_the_package_runner_fails_a_principal_name_under_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    done = _chat(monkeypatch, {"id": "c", "query": "q", **NAMED}, "Rhoda Principal raises them.")
    assert done["passed"] is False
    assert done["scores"]["principal_name"] == ["the principal's name appeared in the reply"]
    assert "forbidden_reply_words" not in done["scores"]
    assert "Rhoda" not in repr(done["scores"])


@pytest.mark.usefixtures("principal")
def test_the_supplier_runner_keys_the_principals_name_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.integrations import erpnext_a2a_agent

    from .test_evals_supplier import SCENARIO

    async def fake_answer(supplier: str, message: str, call: Any, **_k: Any) -> Any:
        return erpnext_a2a_agent.SupplierAnswer("Rhoda will confirm your order.", [])

    async def fake_judge(_scenario: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        return {"overall": 5}

    monkeypatch.setattr(erpnext_a2a_agent, "answer", fake_answer)
    monkeypatch.setattr(runner_module, "judge_supplier", fake_judge)
    result = asyncio.run(runner_module.run_supplier_scenario({**SCENARIO, **NAMED}))
    assert result["passed"] is False
    assert result["scores"]["principal_name"] and "forbidden_reply_words" not in result["scores"]
    assert "Rhoda" not in repr(result["scores"])


@pytest.mark.usefixtures("principal")
def test_run_evals_reports_the_principals_name_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = {"id": "m", "domain": "mcp", "query": "q", **NAMED}
    leak = _evaluate(monkeypatch, scenario, _Replying("Rhoda Principal raises them."))
    assert leak["passed"] is False
    assert _run_evals().failed_checks(leak["scores"]) == ["principal_name"]
    assert "Rhoda" not in repr(leak["scores"])
    fixture = _evaluate(monkeypatch, scenario, _Replying("Rachel raises them."))
    assert fixture["passed"] is False
    assert _run_evals().failed_checks(fixture["scores"]) == ["forbidden_reply_words"]
