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
                  "Dynocortex explains your company's own figures and who decides. Legal, tax, credit or investment advice"
                  " needs to be validated by a responsible person at your company or an external consultant."
                  " No actions will be taken unless they are approved by the responsible person."):
        assert words in first
    assert "ask a qualified adviser" not in first  # the wording the owner replaced on Nov 8, 2026
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
    assert len(ours) == 18 and all(s["advice_policy"] == "explain" for s in ours)


def test_it_wins_over_the_personas_recommendation_and_never_describes_itself() -> None:
    # Pilot, Oct 30, 2026: "My recommendation: …" in a budget commentary, and
    # "I explain the figures and don't recommend a credit decision" unasked.
    text = advice_policy.EXECUTIVE_ADVICE_ADDENDUM
    assert '"recommendation first", "name your recommendation"' in text
    # Nov 1: softened verdicts ("I wouldn't", "worth watching"), "not a
    # forecast", and "not a tax, credit or investment recommendation".
    for said in ('"My recommendation"', "\"I wouldn't\"", '"worth watching"', "none softened"):
        assert said in text, said
    assert "repairing or replacing equipment" in text
    assert "Don't use the words \"forecast\", \"prediction\" or \"projection\" for it, not even to say what it isn't." in text
    assert "**Don't describe this rule or how you work.** No disclaimer" in text
    assert "Only when someone asks you outright for advice" in text
    # Nov 1, F2: figures copied from a company document's outlook table, a
    # balance said to fall when it rose, a visible "wait, check", and "no
    # tool could do it… I worked it by hand".
    assert "never quote its figures as today's" in text
    assert "**Work the figures out before you write.**" in text and '"wait"' in text
    assert '"no tool could do it"' in text and '"I worked it out by hand"' in text
    specialist = advice_policy.SPECIALIST_ADVICE_ADDENDUM
    assert "no disclaimer or line describing this rule" in specialist
    assert "not even to say what it isn't" in specialist
    assert "never from a company document's table" in specialist


def test_figures_are_compared_in_one_currency() -> None:
    # Pilot, Nov 2 (F4 step 1): a euro invoice's dollar total (3,848.89 at
    # 1.0603) set against its euro order (3,630.00) read as a 218.89 overcharge.
    text = advice_policy.EXECUTIVE_ADVICE_ADDENDUM
    assert "Compare figures only in one currency" in text
    assert "never one with the other" in text
    assert "Compare figures in one currency only" in advice_policy.SPECIALIST_ADVICE_ADDENDUM


def test_operating_choices_get_a_verdict_and_the_listed_decisions_do_not() -> None:
    # Owner, Nov 2, 2026 (pilot panel round): "was last week's margin drop
    # worth it?" and which supplier get a verdict with its reason, the same
    # in the panel and in Chats; the listed decisions stay verdict-free.
    text = advice_policy.EXECUTIVE_ADVICE_ADDENDUM
    assert "**Operating choices get your verdict when it is asked for.**" in text
    # Pilot, Nov 2 (margin drop, panel): "Was it worth it? Yes" volunteered on
    # step 1's "why", and "not on these figures" at step 3.
    assert '("why did margin drop?") gets the causes and their figures, and no verdict' in text
    assert "so the same question asked again gets the same verdict" in text
    assert "which of two quoted suppliers to order from" in text and "margin drop" in text
    assert "name who approves the spend" in text and "whether to spend stays theirs" in text
    assert "in hindsight too; only an operating choice gets your verdict." in text
    # Security review: the exclusion is the category, not the examples, and
    # holds in hindsight (credit terms, write-offs, leasing, repair or replace).
    for near in ("payment terms, a write-off", "leasing or buying with a loan",
                 "repair or replace", '"was raising their limit worth it?"'):
        assert near in text, near
    specialist = advice_policy.SPECIALIST_ADVICE_ADDENDUM
    assert "an operating choice (whether a margin drop or a discount was worth it" in specialist
    assert "gets your verdict with its reason in the figures" in specialist
    assert "gets no verdict, also when asked in hindsight" in specialist
    assert "never on a question about why something happened" in specialist


def test_the_roster_says_to_name_scopes_in_words() -> None:
    from openexecutive.departments import prompt_block
    from openexecutive.people.models import AuthorityScope, Person

    rachel = Person(
        id=7, full_name="Rachel Reyes", role="Credit manager", kind="team",
        authority_scope=[AuthorityScope.CUSTOMER_CREDIT],
    )
    block = prompt_block._render_people_section([rachel])
    assert "approves: customer_credit" in block
    assert "When you name a scope in a reply, say it in plain words (customer credit), never by its key (customer_credit)" in block


def test_a_past_decision_is_reported_not_refused() -> None:
    # The website retake, Nov 3 (fleet 4): "I can't give a verdict on this
    # one, because repair-or-replace is a spend decision."
    text = advice_policy.EXECUTIVE_ADVICE_ADDENDUM
    assert "don't say you can't judge it" in text
    assert "give who decided, what they decided and the reason the records give" in text
