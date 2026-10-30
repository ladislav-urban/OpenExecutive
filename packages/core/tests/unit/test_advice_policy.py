"""Explain, don't advise (prompts.advice_policy, ADVICE_POLICY=explain): on
money, tax, law and credit the Executive and its core specialists show the
figures, the arithmetic and who decides, and never recommend a decision. A
constant in each cached block, only when the install turns it on."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.agents import overrides as ov_mod  # noqa: E402
from openexecutive.prompts import advice_policy  # noqa: E402
from openexecutive.prompts.cache_manager import build_system_blocks  # noqa: E402

HEADING = "## Money, tax, law and credit: explain, don't advise"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ov_mod, "DB_PATH", tmp_path / "ov.db")
    monkeypatch.delenv("ADVICE_POLICY", raising=False)
    ov_mod.invalidate_cache()
    yield
    ov_mod.invalidate_cache()


def test_off_by_default_block_0_is_as_before() -> None:
    assert not advice_policy.advice_policy_on()
    text = build_system_blocks()[0]["text"]
    assert HEADING not in text
    assert "give the executive framing and the right questions" in text


@pytest.mark.parametrize("value", ["explain", " Explain "])
def test_the_setting_turns_it_on(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ADVICE_POLICY", value)
    assert advice_policy.advice_policy_on()
    first = build_system_blocks()[0]["text"]
    assert HEADING in first
    for words in ("outlook from today's records", "Never make or recommend a tax, legal, credit or investment decision",
                  "I don't give tax, legal, credit or investment advice; for those, ask a qualified adviser."):
        assert words in first
    # A constant: the block is the same on every build.
    assert build_system_blocks()[0]["text"] == first


def test_any_other_value_leaves_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADVICE_POLICY", "strict")
    assert HEADING not in build_system_blocks()[0]["text"]


def test_a_custom_persona_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADVICE_POLICY", "explain")
    assert HEADING in build_system_blocks(persona_override="CUSTOM EXEC PERSONA")[0]["text"]


def test_who_decides_is_named_only_inside_the_company(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADVICE_POLICY", "explain")
    text = build_system_blocks()[0]["text"]
    assert "Only to someone inside the company" in text
    assert "never name who decides or anyone's approval scope" in text


def test_an_override_that_is_not_text_leaves_it_off() -> None:
    token = advice_policy.advice_policy_override.set(True)  # type: ignore[arg-type]
    try:
        assert not advice_policy.advice_policy_on()
    finally:
        advice_policy.advice_policy_override.reset(token)


def test_an_evals_override_is_its_own() -> None:
    token = advice_policy.advice_policy_override.set("explain")
    try:
        assert HEADING in build_system_blocks()[0]["text"]
    finally:
        advice_policy.advice_policy_override.reset(token)
    assert HEADING not in build_system_blocks()[0]["text"]


def test_core_specialists_get_it_and_internal_helpers_do_not(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.agents.finance import FinanceAgent
    from openexecutive.agents.legal import LegalAgent
    from openexecutive.agents.triage import TriageAgent

    assert advice_policy.SPECIALIST_ADVICE_ADDENDUM not in FinanceAgent().effective_system_prompt()
    monkeypatch.setenv("ADVICE_POLICY", "explain")
    for agent in (FinanceAgent(), LegalAgent()):
        assert agent.effective_system_prompt().endswith(advice_policy.SPECIALIST_ADVICE_ADDENDUM)
    assert advice_policy.SPECIALIST_ADVICE_ADDENDUM not in TriageAgent().effective_system_prompt()


def test_a_scenario_names_only_explain() -> None:
    from openexecutive.evals.scenarios import load_scenarios, validate_scenario_yaml

    base = "id: x\ndomain: finance\nquery: q\n"
    assert validate_scenario_yaml(base + "advice_policy: explain\n")["advice_policy"] == "explain"
    with pytest.raises(ValueError, match="advice_policy"):
        validate_scenario_yaml(base + "advice_policy: advise\n")
    ours = [s for s in load_scenarios() if s["id"].startswith("finance_advice_")]
    assert len(ours) == 5 and all(s["advice_policy"] == "explain" for s in ours)
