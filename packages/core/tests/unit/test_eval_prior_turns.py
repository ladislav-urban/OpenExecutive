"""An eval scenario's ``prior_turns``: the conversation a follow-up question
follows, seeded into the session and shown to the judge
(openexecutive.evals.scenarios)."""
from __future__ import annotations

from typing import Any

import pytest

from openexecutive.evals import scenarios
from openexecutive.evals.scenarios import (
    load_scenarios,
    prior_turns_section,
    scenario_prior_turns,
    seed_prior_turns,
    validate_scenario_yaml,
)
from openexecutive.orchestrator.session import Session

FOLLOW_UP: dict[str, Any] = {
    "id": "x",
    "query": "Which of these receipts should that invoice have matched?",
    "prior_turns": [
        {"user": "Any invoices that don't match?", "assistant": "One: ACC-PINV-1 has no receipt."},
        {"page": {"doctype": "Purchase Invoice", "name": "ACC-PINV-1"}, "user": "What's wrong?", "assistant": "No PO."},
    ],
}


def test_prior_turns_seed_the_history_in_order_with_their_pages() -> None:
    session = Session()
    seed_prior_turns(session, FOLLOW_UP)
    history = session.conversation_history
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "Any invoices that don't match?"
    assert history[1]["content"] == "One: ACC-PINV-1 has no receipt."
    # A turn's page goes before its message, as the panel sends it.
    assert history[2]["content"].endswith("What's wrong?") and "ACC-PINV-1" in history[2]["content"]
    assert history[2]["content"] != "What's wrong?"


def test_the_judge_sees_the_turns_and_every_other_prompt_is_unchanged() -> None:
    section = prior_turns_section(FOLLOW_UP)
    assert "EARLIER IN THIS CONVERSATION" in section
    assert "ASSISTANT: One: ACC-PINV-1 has no receipt." in section
    assert prior_turns_section({"id": "y", "query": "q"}) == ""
    assert scenario_prior_turns({"id": "y", "query": "q"}) == []


@pytest.mark.parametrize(
    "bad",
    [
        "prior_turns: []",
        "prior_turns: {user: a, assistant: b}",
        "prior_turns:\n  - {user: a}",
        "prior_turns:\n  - {user: a, assistant: ''}",
        "prior_turns:\n  - {user: a, assistant: b, speaker: c}",
    ],
)
def test_a_malformed_prior_turns_fails_validation(bad: str) -> None:
    with pytest.raises(ValueError, match="prior_turns"):
        validate_scenario_yaml(f"id: x\nquery: q\n{bad}\n")


def test_the_follow_up_scenarios_load_and_validate() -> None:
    import yaml

    shipped = {s["id"]: s for s in load_scenarios()}
    for sid in ("erpnext_063", "erpnext_064", "erpnext_065"):
        s = shipped[sid]
        assert len(scenario_prior_turns(s)) >= 1, sid
        validate_scenario_yaml(yaml.safe_dump(s))
    assert scenarios.scenario_surface(shipped["erpnext_063"]) == "erpnext_panel"
