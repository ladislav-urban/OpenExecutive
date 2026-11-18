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

    monkeypatch.setattr(scenarios, "_principal_names_and_addresses", lambda: ([], []))  # the roster
    (scenario,) = [s for s in load_scenarios(kind="mcp") if s["id"] == "erpnext_098"]
    assert scenario["surface"] == "erpnext_panel" and scenario["demo_viewer"] is True
    assert scenario["forbidden_reply_words"] == ["ERPNext"]
    assert reply_must_name_missing(scenario, THREE_ROWS) == ["Panel wiring|DEMO-WIR-25"]
    assert reply_check_failures(scenario, FOUR_ROWS) == {}


# ── the principal's name, apart from the scenario's own words ─────────────

PRINCIPAL = "Rhoda Prentiss"


@pytest.fixture
def principal(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals import scenarios

    monkeypatch.setattr(scenarios, "_principal_names_and_addresses", lambda: ([PRINCIPAL], []))


# The ways a reply may name the principal (scenarios.name_variants): not
# only the full or first name.
def _olivia(monkeypatch: pytest.MonkeyPatch, *names: str, addresses: tuple[str, ...] = ()) -> None:
    from openexecutive.evals import scenarios

    monkeypatch.setattr(scenarios, "_principal_names_and_addresses", lambda: (list(names), list(addresses)))


FORBID = {"forbid_principal_name": True}


def test_the_name_variants_say_which_match_in_any_case() -> None:
    from openexecutive.evals.scenarios import name_variants

    # (text, any_case): the guard's words, the pairs and the addresses in any
    # case; another name word capitalised only.
    assert name_variants("Dr. Olivia  Owner") == [
        ("Dr. Olivia Owner", True), ("Owner, Olivia", True), ("Olivia Owner", True), ("Olivia", True),
        ("OWNER", False), ("Owner", False),
    ]
    assert name_variants("Olivia Owner", "olivia.owner@example.com") == [
        ("olivia.owner@example.com", True), ("Owner, Olivia", True), ("Olivia Owner", True),
        ("olivia.owner", True), ("Olivia", True), ("OWNER", False), ("Owner", False),
    ]
    # The guard's words have no length minimum; another short word is left out.
    assert name_variants("Jo Owner") == [
        ("Owner, Jo", True), ("Jo Owner", True), ("OWNER", False), ("Owner", False), ("Jo", True),
    ]
    assert ("Al", True) not in name_variants("Olivia Al Owner")
    assert name_variants(None) == [] and name_variants("  ") == []
    # An email's local part only when it joins parts; the whole address always.
    assert ("o.owner", True) in name_variants("Olivia Owner", "o.owner@x")
    assert ("o_owner", True) in name_variants("Olivia Owner", "o_owner@x")
    assert [v.text for v in name_variants("Olivia Owner", "principal@eval.example")] == [
        "principal@eval.example", "Owner, Olivia", "Olivia Owner", "Olivia", "OWNER", "Owner",
    ]


@pytest.mark.parametrize(
    ("full_name", "reply"),
    [
        ("Olivia Owner, CPA", "Ms. Owner approves."),
        ("Olivia Owner Jr", "Ms. Owner approves."),
        ("Olivia Owner III, PhD", "Ms. Owner approves."),
        ("Olivia Owner (CEO)", "Ms. Owner approves."),
        ("Owner, Olivia", "Ms. Owner approves."),
        ("Olivia Owner-Smith", "Ms. Owner approves."),
        ("Olivia Owner-Smith", "Ms. Smith approves."),
        ("Olivia Owner PhD", "Owner approves."),
        ("Olivia Owner Esq.", "Owner approves."),
        ("Olivia Owner - CEO", "Owner approves."),
        ("De La Cruz, Maria", "Ask Maria."),
        ("De La Cruz, Maria", "Maria De La Cruz approves."),
        ("van der Berg, Anna", "Ask Anna."),
        ("St. John, Olivia", "Ask Olivia."),
        ("St. John, Olivia", "Olivia St. John approves."),
        ("Owner Jr., Olivia", "Ask Olivia."),
        ("Owner, JoAnn", "Ask JoAnn."),
        ("Owner, McKay", "Ask McKay."),
        ("OWNER, ANNA", "Ask Anna."),
        ("Jo Owner", "Ask Jo."),
    ],
)
def test_each_shape_of_a_stored_name_is_caught_and_scrubbed(
    monkeypatch: pytest.MonkeyPatch, full_name: str, reply: str,
) -> None:
    from openexecutive.evals.scenarios import principal_name_in, scrub_principal_name

    _olivia(monkeypatch, full_name)
    assert principal_name_in(FORBID, reply)
    assert "[the principal]" in scrub_principal_name(reply)


@pytest.mark.parametrize(
    "full_name",
    ["Olivia Owner, CPA", "Olivia Owner Jr", "Olivia Owner PhD", "Olivia Owner Esq.", "Olivia Owner - CEO", "Olivia Owner (CEO)"],
)
def test_a_suffix_or_credential_is_never_a_variant(monkeypatch: pytest.MonkeyPatch, full_name: str) -> None:
    from openexecutive.evals.scenarios import principal_name_in, scrub_principal_name

    _olivia(monkeypatch, full_name, addresses=("principal@eval.example",))
    for reply in ("a PhD is needed", "the CEO approves.", "It sits with the principal.", "Jr staff and the CPA."):
        assert not principal_name_in(FORBID, reply), reply
        assert scrub_principal_name(reply) == reply


@pytest.mark.parametrize(
    "reply",
    [
        "Ms. Owner signs off.", "Owner approves.", "Ask Olivia.", "Ask olivia.", "OLIVIA signs.",
        "Owner, Olivia approves.", "owner, olivia approves.", "OLIVIA OWNER approves.",
        "Mail Olivia.Owner today.", "Created by olivia@acme.example.", "Cc O.Owner@Alias.Example.",
    ],
)
def test_each_variant_of_the_principals_name_is_caught(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    from openexecutive.evals.scenarios import principal_name_in

    _olivia(
        monkeypatch, "Dr. Olivia Owner",
        addresses=("olivia.owner@example.com", "olivia@acme.example", "o.owner@alias.example"),
    )
    assert principal_name_in(FORBID, reply)


@pytest.mark.parametrize(
    "reply",
    ["Dr. Smith approves.", "The Owners approve.", "Prof. Jones and Mr. Lee.", "the account owner approves."],
)
def test_an_honorific_a_longer_word_or_a_lowercase_surname_is_not_the_principals_name(
    monkeypatch: pytest.MonkeyPatch, reply: str,
) -> None:
    from openexecutive.evals.scenarios import principal_name_in

    _olivia(monkeypatch, "Dr. Olivia Owner")
    assert not principal_name_in(FORBID, reply)


def test_a_bare_email_local_part_is_an_ordinary_word(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals.scenarios import principal_name_in, scrub_principal_name

    # The eval bench's principal.
    _olivia(monkeypatch, "Olivia Owner", addresses=("principal@eval.example",))
    for reply in ("It sits with the principal.", "the account owner approves."):
        assert not principal_name_in(FORBID, reply)
        assert scrub_principal_name(reply) == reply
    for reply in ("Owner approves.", "Ask Olivia.", "Ask olivia.", "Mail principal@eval.example."):
        assert principal_name_in(FORBID, reply)
        assert "[the principal]" in scrub_principal_name(reply)


def test_an_email_local_part_with_a_separator_is_the_principals_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals.scenarios import principal_name_in, scrub_principal_name

    _olivia(monkeypatch, "Olivia Owner", addresses=("o.owner@x",))
    assert principal_name_in(FORBID, "Write to o.owner about it.")
    assert principal_name_in(FORBID, "Write to O.Owner about it.")
    assert scrub_principal_name("Write to o.owner@x today.") == "Write to [the principal] today."


def test_every_principal_row_is_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals.scenarios import principal_name_in

    # An archived principal's row still names them in old texts.
    _olivia(monkeypatch, "Olivia Owner", "Peter Parker", addresses=("pp.old@acme.example",))
    for reply in ("Ask Peter.", "Parker signs.", "Mail pp.old today."):
        assert principal_name_in(FORBID, reply)


@pytest.mark.parametrize(
    ("full_name", "reply"),
    [
        # A title and a single name: that name in any case.
        ("Dr. Olivia", "Ask olivia."),
        ("Ms OLIVIA", "Ask Olivia."),
        ("Mrs. olivia", "OLIVIA signs."),
        # A two-character name, capitalised.
        ("Olivia Li", "Ms. Li signs."),
        ("Olivia Li", "Li approves."),
        ("Wu, Olivia", "Ms. Wu signs."),
        ("Olivia Ng", "NG approves."),
        ("Olivia Owner", "OWNER approves."),
    ],
)
def test_a_title_and_one_name_a_short_name_and_all_caps_are_caught(
    monkeypatch: pytest.MonkeyPatch, full_name: str, reply: str,
) -> None:
    from openexecutive.evals.scenarios import principal_name_in, scrub_principal_name

    _olivia(monkeypatch, full_name)
    assert principal_name_in(FORBID, reply)
    assert "[the principal]" in scrub_principal_name(reply)


@pytest.mark.parametrize(
    ("full_name", "reply"),
    [
        ("Olivia Li", "the li-ion battery"),
        ("Olivia Md", "Call your MD today."),
        ("A. Owner", "A plan and a budget."),
        ("I. M. Owner", "I think I can."),
    ],
)
def test_an_initial_or_a_lowercase_short_word_is_not_the_principals_name(
    monkeypatch: pytest.MonkeyPatch, full_name: str, reply: str,
) -> None:
    from openexecutive.evals.scenarios import name_variants, principal_name_in, scrub_principal_name

    _olivia(monkeypatch, full_name)
    assert not principal_name_in(FORBID, reply)
    assert scrub_principal_name(reply) == reply
    # No variant is a single character, whatever its source.
    assert all(len(v.text) > 1 for v in name_variants(full_name, "a@b.example"))


@pytest.mark.parametrize(
    ("full_name", "reply"),
    [
        ("김 민준", "Ask 김."), ("王 伟", "Ask 王 today."), ("王 伟", "Ask 伟."), ("Olivia 王", "Ms. 王 signs."),
        ("민준 김", "Ms. 김 signs."), ("Weiß, Olivia", "WEISS signs."), ("Weiß, Olivia", "Ask weiß."),
    ],
)
def test_an_uncased_one_character_name_and_a_sharp_s_are_caught(
    monkeypatch: pytest.MonkeyPatch, full_name: str, reply: str,
) -> None:
    from openexecutive.evals.scenarios import principal_name_in, scrub_principal_name

    _olivia(monkeypatch, full_name)
    assert principal_name_in(FORBID, reply)
    assert "[the principal]" in scrub_principal_name(reply)


def test_an_initialled_name_still_matches_its_surname(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals.scenarios import principal_name_in

    _olivia(monkeypatch, "A. Owner")
    assert principal_name_in(FORBID, "Ms. Owner signs.")
    assert principal_name_in(FORBID, "A. Owner signs.")


ROSTERS = [
    (["Olivia Owner"], ["principal@eval.example"]),
    (["Jo Owner", "Owner, Olivia"], ["olivia@acme.example", "o.owner@alias.example"]),
    (["De La Cruz, Maria", "van der Berg, Anna"], ["ceo@acme.example"]),
    (["St. John, Olivia", "Owner Jr., Olivia", "OWNER, ANNA"], []),
    (["Olivia Owner PhD", "Olivia Owner - CEO", "Olivia Owner (CEO)", "Olivia Owner-Smith"], ["x@y.example"]),
    (["Dr. Olivia Owner", "Prof Al Bo"], ["al.bo@uni.example"]),
    (["Dr. Olivia", "Olivia Li", "Wu, Olivia", "A. Owner"], []),
    (["김 민준", "王 伟", "Weiß, Olivia"], []),
]


@pytest.mark.parametrize(("names", "addresses"), ROSTERS)
def test_the_eval_matches_every_word_the_demo_guard_hides(
    monkeypatch: pytest.MonkeyPatch, names: list[str], addresses: list[str],
) -> None:
    from openexecutive.evals.scenarios import _HONORIFICS, name_pattern, principal_name_variants
    from openexecutive.people.demo import principal_name_words

    _olivia(monkeypatch, *names, addresses=tuple(addresses))
    pattern = name_pattern(principal_name_variants())
    assert pattern is not None
    for word in principal_name_words(names, addresses):
        bare = word.strip(".")
        if bare.casefold() in _HONORIFICS or (len(bare) < 2 and bare.lower() != bare.upper()):
            continue  # "Dr." or an initial ("A.") alone is no name; "김" is
        for reply in (word, f"Ask {word} today.", f"Ask {word.lower()} today."):
            assert pattern.search(reply), (word, reply)


def test_the_scrub_replaces_every_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals.scenarios import scrub_principal_name

    _olivia(monkeypatch, "Dr. Olivia Owner", addresses=("olivia.owner@example.com",))
    text = (
        "Dr. Olivia Owner, Ms. Owner, Owner, Olivia, olivia.owner@example.com, olivia;"
        " Dr. Smith and the account owner."
    )
    assert scrub_principal_name(text) == (
        # The roster's own spelling ("Dr. Olivia Owner") is a variant too.
        "[the principal], Ms. [the principal], [the principal], [the principal], [the principal];"
        " Dr. Smith and the account owner."
    )


# A sweep reported "Rachel", a demo-fixture name in erpnext_064's own list,
# under the same key as a principal leak: the two now have a key each.
NAMED = {"forbidden_reply_words": ["Rachel"], "forbid_principal_name": True}


@pytest.mark.usefixtures("principal")
def test_a_fixture_name_and_the_principals_name_have_a_key_each() -> None:
    assert reply_check_failures(NAMED, "Rachel raises them.") == {"forbidden_reply_words": ["Rachel"]}
    leak = reply_check_failures(NAMED, "Rhoda raises them.")
    assert leak == {"principal_name": ["the principal's name appeared in the reply"]}
    both = reply_check_failures(NAMED, "Rachel drafts them and Rhoda Prentiss approves.")
    assert both["forbidden_reply_words"] == ["Rachel"] and both["principal_name"]
    # The name is never recorded, whatever path records it.
    assert "Rhoda" not in repr(both)
    assert "Rhoda" not in repr(reply_check_failures(NAMED, "Rhoda: on time.", repeated=False))  # a watch notice
    # Without forbid_principal_name the principal may be named.
    assert reply_check_failures({"forbidden_reply_words": ["Rachel"]}, "Rhoda raises them.") == {}


@pytest.mark.usefixtures("principal")
def test_the_package_runner_fails_a_principal_name_under_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    done = _chat(monkeypatch, {"id": "c", "query": "q", **NAMED}, "Rhoda Prentiss raises them.")
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
    leak = _evaluate(monkeypatch, scenario, _Replying("Rhoda Prentiss raises them."))
    assert leak["passed"] is False
    assert _run_evals().failed_checks(leak["scores"]) == ["principal_name"]
    assert "Rhoda" not in repr(leak["scores"])
    fixture = _evaluate(monkeypatch, scenario, _Replying("Rachel raises them."))
    assert fixture["passed"] is False
    assert _run_evals().failed_checks(fixture["scores"]) == ["forbidden_reply_words"]


# A live prior turn's reply is held to the principal's name too (runner.chat_checks).
LIVE_NAMED = {
    "id": "l",
    "domain": "mcp",
    "query": "q",
    **NAMED,
    "prior_turns": [{"user": "a", "assistant": "fixed"}, {"user": "b", "live": True}, {"user": "c", "live": True}],
}


class _InTurn:
    """Executive.chat answering each call with the next reply."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)

    async def chat(self, **kwargs: Any) -> str:
        return self.replies.pop(0)


def _chat_turns(monkeypatch: pytest.MonkeyPatch, scenario: dict[str, Any], *replies: str) -> dict[str, Any]:
    executive = _InTurn(*replies)

    async def fake_chat(self: Any, **kwargs: Any) -> str:
        return await executive.chat(**kwargs)

    async def fake_judge(_scenario: dict[str, Any], _response: str) -> dict[str, Any]:
        return {"overall": 5}

    monkeypatch.setattr(Executive, "__init__", lambda self, mcp_gateway=None: None)
    monkeypatch.setattr(Executive, "chat", fake_chat)
    monkeypatch.setattr(runner_module, "judge_chat", fake_judge)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    asyncio.run(runner_module._make_chat_runner(asyncio.Semaphore(1), queue, [0], 1, None)(0, scenario))
    (done,) = [e for e in (queue.get_nowait() for _ in range(queue.qsize())) if e["type"] == "scenario_done"]
    return done


@pytest.mark.usefixtures("principal")
def test_the_package_runner_fails_a_live_turn_that_names_the_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    done = _chat_turns(monkeypatch, LIVE_NAMED, "Ask Rhoda.", "On time.", "On time.")
    assert done["passed"] is False
    # Which turn, by its place in prior_turns; the final reply's own hit is apart.
    assert done["scores"]["principal_name"] == ["the principal's name appeared in live prior turn 2's reply"]
    assert "Rhoda" not in repr(done["scores"]) and "rhoda" not in repr(done["scores"])
    both = _chat_turns(monkeypatch, LIVE_NAMED, "On time.", "Rhoda Prentiss signs.", "Rhoda approves.")
    assert both["scores"]["principal_name"] == [
        "the principal's name appeared in the reply",
        "the principal's name appeared in live prior turn 3's reply",
    ]
    # Only the principal's name: a live turn's other words stay its own.
    clean = _chat_turns(monkeypatch, LIVE_NAMED, "Rachel raises them.", "On time.", "On time.")
    assert clean["passed"] is True and "principal_name" not in clean["scores"]
    # Without forbid_principal_name a live turn may name the principal.
    unforbidden = {k: v for k, v in LIVE_NAMED.items() if k != "forbid_principal_name"}
    assert _chat_turns(monkeypatch, unforbidden, "Ask Rhoda.", "On time.", "On time.")["passed"] is True


@pytest.mark.usefixtures("principal")
def test_run_evals_fails_a_live_turn_that_names_the_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    leak = _evaluate(monkeypatch, LIVE_NAMED, _InTurn("Rhoda Prentiss signs.", "On time.", "On time."))
    assert leak["passed"] is False
    assert leak["scores"]["principal_name"] == ["the principal's name appeared in live prior turn 2's reply"]
    assert _run_evals().failed_checks(leak["scores"]) == ["principal_name"]
    assert "Rhoda" not in repr(leak["scores"])


@pytest.mark.usefixtures("principal")
def test_a_live_reply_that_named_the_principal_is_stored_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    said = "Rhoda Prentiss signs; ask Rhoda."
    scrubbed = {"turn": 2, "response": "[the principal] signs; ask [the principal].", "response_length": len(said)}
    done = _chat_turns(monkeypatch, LIVE_NAMED, said, "On time.", "On time.")
    assert done["live_turns"][0] == scrubbed and done["scores"]["principal_name"]
    leak = _evaluate(monkeypatch, LIVE_NAMED, _InTurn(said, "On time.", "On time."))
    assert leak["live_turns"][0] == scrubbed and leak["scores"]["principal_name"]
    # The name is in neither result, stored replies included.
    assert "rhoda" not in repr(done).lower() and "rhoda" not in repr(leak).lower()
    # A live reply that didn't name them is kept as it was.
    assert leak["live_turns"][1] == {"turn": 3, "response": "On time.", "response_length": 8}
    # Without forbid_principal_name the scenario may pass, but the stored
    # reply is scrubbed all the same.
    unforbidden = {k: v for k, v in LIVE_NAMED.items() if k != "forbid_principal_name"}
    free = _chat_turns(monkeypatch, unforbidden, said, "On time.", "On time.")
    assert free["passed"] is True and free["live_turns"][0] == scrubbed


@pytest.mark.usefixtures("principal")
def test_a_name_past_the_stored_cut_still_fails_the_live_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    said = "x" * runner_module.LIVE_REPLY_STORED_CHARS + " Rhoda Prentiss signs."
    done = _chat_turns(monkeypatch, LIVE_NAMED, said, "On time.", "On time.")
    assert done["scores"]["principal_name"] == ["the principal's name appeared in live prior turn 2's reply"]
    assert done["live_turns"][0]["response"] == "x" * runner_module.LIVE_REPLY_STORED_CHARS
    assert done["live_turns"][0]["response_truncated"] is True


# The judge's notes echo what it read; a live reply reaches it scrubbed, and
# whatever it still says of the principal is scrubbed before it's stored.
ECHOING_SCORES = {"overall": 5, "notes": "Rhoda Prentiss is named.", "reasons": {"tone": ["ask Rhoda"]}}


def _echoing_judge(seen: list[dict[str, Any]]) -> Any:
    async def judge(scenario: dict[str, Any], _response: str, drafts: Any = None) -> dict[str, Any]:
        seen.append(scenario)
        return {**ECHOING_SCORES, "reasons": {"tone": list(ECHOING_SCORES["reasons"]["tone"])}}

    return judge


@pytest.mark.usefixtures("principal")
@pytest.mark.parametrize("forbid", [True, False])
def test_the_package_runner_stores_no_principal_name_anywhere(monkeypatch: pytest.MonkeyPatch, forbid: bool) -> None:
    scenario = LIVE_NAMED if forbid else {k: v for k, v in LIVE_NAMED.items() if k != "forbid_principal_name"}
    seen: list[dict[str, Any]] = []
    executive = _InTurn("Ask Rhoda.", "On time.", "Rhoda Prentiss signs.")

    async def fake_chat(self: Any, **kwargs: Any) -> str:
        return await executive.chat(**kwargs)

    monkeypatch.setattr(Executive, "__init__", lambda self, mcp_gateway=None: None)
    monkeypatch.setattr(Executive, "chat", fake_chat)
    monkeypatch.setattr(runner_module, "judge_chat", _echoing_judge(seen))
    queue: asyncio.Queue[Any] = asyncio.Queue()
    asyncio.run(runner_module._make_chat_runner(asyncio.Semaphore(1), queue, [0], 1, None)(0, scenario))
    (done,) = [e for e in (queue.get_nowait() for _ in range(queue.qsize())) if e["type"] == "scenario_done"]
    assert done["response"] == "[the principal] signs."
    assert done["scores"]["notes"] == "[the principal] is named."
    assert done["scores"]["reasons"] == {"tone": ["ask [the principal]"]}
    assert "rhoda" not in repr(done).lower()
    # The judge read the live reply scrubbed, the fixed one as written.
    (judged,) = seen
    assert [t["assistant"] for t in judged["prior_turns"]] == ["fixed", "Ask [the principal].", "On time."]
    # The check still read the whole replies.
    assert done["passed"] is not forbid
    if forbid:
        assert done["scores"]["principal_name"] == [
            "the principal's name appeared in the reply",
            "the principal's name appeared in live prior turn 2's reply",
        ]


@pytest.mark.usefixtures("principal")
@pytest.mark.parametrize("forbid", [True, False])
def test_run_evals_stores_no_principal_name_anywhere(monkeypatch: pytest.MonkeyPatch, forbid: bool) -> None:
    scenario = LIVE_NAMED if forbid else {k: v for k, v in LIVE_NAMED.items() if k != "forbid_principal_name"}
    run_evals = _run_evals()
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(run_evals, "judge_response", _echoing_judge(seen))
    said = "Rhoda Prentiss signs."
    result = asyncio.run(run_evals.evaluate_chat(scenario, _InTurn("Ask Rhoda.", "On time.", said)))
    assert result["response"] == "[the principal] signs." and result["response_length"] == len(said)
    assert result["scores"]["notes"] == "[the principal] is named."
    assert result["scores"]["reasons"] == {"tone": ["ask [the principal]"]}
    assert "rhoda" not in repr(result).lower()
    (judged,) = seen
    assert [t["assistant"] for t in judged["prior_turns"]] == ["fixed", "Ask [the principal].", "On time."]
    assert result["passed"] is not forbid


# A failed scenario's error is stored too; a live turn's carries its
# exception's text, which may name the principal.
class _Failing:
    async def chat(self, **kwargs: Any) -> str:
        raise RuntimeError("Rhoda Prentiss's mailbox timed out")


@pytest.mark.usefixtures("principal")
def test_a_failed_scenarios_error_is_stored_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    executive = _Failing()

    async def fake_chat(self: Any, **kwargs: Any) -> str:
        return await executive.chat(**kwargs)

    monkeypatch.setattr(Executive, "__init__", lambda self, mcp_gateway=None: None)
    monkeypatch.setattr(Executive, "chat", fake_chat)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    asyncio.run(runner_module._make_chat_runner(asyncio.Semaphore(1), queue, [0], 1, None)(0, LIVE_NAMED))
    (error,) = [e for e in (queue.get_nowait() for _ in range(queue.qsize())) if e["type"] == "scenario_error"]
    assert error["error"].startswith("live prior turn 2 failed")
    assert "[the principal]'s mailbox timed out" in error["error"]
    assert "rhoda" not in repr(error).lower()
    result = asyncio.run(_run_evals().evaluate_chat_or_error(LIVE_NAMED, _Failing()))
    assert result["passed"] is False and "[the principal]'s mailbox" in result["error"]
    assert "rhoda" not in repr(result).lower()


def test_an_error_that_cant_be_checked_is_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals import scenarios

    def unreadable() -> Any:
        raise RuntimeError("roster unreadable")

    monkeypatch.setattr(scenarios, "_principal_names_and_addresses", unreadable)
    assert runner_module.stored_error(RuntimeError("Rhoda Prentiss failed")) == runner_module.ERROR_UNCHECKED


# ── where evals/run_evals.py writes its results ────────────────────────────


def test_run_evals_writes_into_its_own_ignored_results_dir_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Before, the default was "results/" in the current directory, so a run
    # from packages/core or the repo root wrote full replies outside the
    # one ignored results directory.
    repo = Path(__file__).resolve().parents[4]
    monkeypatch.chdir(tmp_path)
    parser = _run_evals().build_parser()
    assert Path(parser.parse_args([]).output) == repo / "evals" / "results"
    assert "evals/results/" in (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    # --output still goes where it's given.
    assert parser.parse_args(["--output", "elsewhere/"]).output == "elsewhere/"
