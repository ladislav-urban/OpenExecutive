# ruff: noqa: F811 — pytest fixtures imported from test_erpnext_drafts share their names with the test parameters.
"""A number inside a record's name is part of the name, never a figure the
answer check holds to the results (``figure_check._in_a_name``), and an
ERPNext refusal of what the person asking can't see is put in words about
them (``erpnext_caller.in_askers_words``).

Found Nov 17, 2026 (where-is-my-order step 4, erpnext_098): the check read
"2.5" in an item named "... 2.5 mm2 (per metre)" as a figure no result held
and left that item's row out of the reply's table, which still said "covers
every line"; and every reply told the person "this ERPNext user has no
permission", oe_connector's own words. Fixtures use made-up items."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.orchestrator import answer_check, answer_review, erpnext_caller, mcp_gateway
from openexecutive.orchestrator.answer_review import CANNOT_TELL
from openexecutive.orchestrator.erpnext_caller import ErpnextCaller
from openexecutive.orchestrator.figure_check import ErpnextResult, check_figures, sources_for
from openexecutive.orchestrator.name_check import name_sources_for

from .test_delegation_tools import OWNER, db, fresh_turn_state  # noqa: F401
from .test_erpnext_drafts import _owner_turn, erp, extension, roster  # noqa: F401

CABLE = "Copper cable 1.5 mm2 (per metre)"
DELIVERY = ErpnextResult(
    "erpnext__get_record",
    {"doctype": "Delivery Note", "name": "DN-0001", "child_rows": True},
    {
        "doctype": "Delivery Note",
        "name": "DN-0001",
        "record": {
            "name": "DN-0001",
            "per_installed": 100.0,
            "items": [
                {"item_code": "SW-01", "item_name": "Wall switch", "qty": 24.0, "installed_qty": 24.0, "uom": "Nos"},
                {"item_code": "CB-15", "item_name": CABLE, "qty": 250.0, "installed_qty": 250.0, "uom": "Meter"},
                {"item_code": "DB-1", "item_name": "Distribution board", "qty": 1.0, "installed_qty": 1.0, "uom": "Nos"},
            ],
        },
    },
)
TABLE = (
    "Yes. The Installation Note covers every line:\n\n"
    "| Item | Delivered | Installed |\n|---|---|---|\n"
    "| Wall switch | 24 | 24 |\n"
    f"| {CABLE} | 250 | 250 |\n"
    "| Distribution board | 1 | 1 |"
)


def _figures(text: str) -> list[str]:
    names = name_sources_for([DELIVERY])
    sources = sources_for([DELIVERY], names=(n for written in names.by_norm.values() for n in written))
    return [f.text for f in check_figures(text, sources) if f.flagged]


@pytest.mark.parametrize(
    "text",
    [
        TABLE,
        "- Copper cable 1.5 mm2: 250 m delivered, 250 m installed",
        "All 250 m of copper cable 1.5 mm2 was installed.",
    ],
)
def test_a_number_inside_an_items_name_is_no_figure(text: str) -> None:
    assert _figures(text) == []


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        # A wrong quantity beside the name is still a figure.
        ("| Copper cable 1.5 mm2 (per metre) | 260 | 250 |", ["260"]),
        # A size the name doesn't hold is still held to the results.
        ("| Copper cable 2.5 mm2 (per metre) | 250 | 250 |", ["2.5"]),
        # A name's number as an amount of money is a figure.
        ("Copper cable cost $1.5 per metre.", ["$1.5"]),
    ],
)
def test_a_figure_beside_a_name_is_still_checked(text: str, flagged: list[str]) -> None:
    assert _figures(text) == flagged


def test_without_the_names_the_number_is_read_as_a_figure() -> None:
    # As before the fix: no names given, "1.5" is a figure no result holds.
    assert [f.text for f in check_figures(TABLE, sources_for([DELIVERY])) if f.flagged] == ["1.5"]


def test_the_check_keeps_the_row_that_names_the_item(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)

    async def cannot_tell(**kw: Any) -> list[Any]:
        return [CANNOT_TELL] * len(kw["flagged"])

    async def no_rewrite(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(answer_review, "review", cannot_tell)
    monkeypatch.setattr(answer_check, "_rewrite", no_rewrite)
    turn = answer_check.AnswerCheckTurn()
    turn.results.append(DELIVERY)
    turn._context = {"number_format": "#,###.##"}
    checked = asyncio.run(answer_check.check_reply(TABLE, turn, question="Was it all installed?", model="m"))
    assert checked == TABLE


# ── refusals in the asker's words ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        (
            "Tool error: Permission denied: this ERPNext user cannot read Project 'PRJ-0001'",
            "Tool error: Permission denied: the asker's own access doesn't include Project 'PRJ-0001'",
        ),
        (
            '{"not_shown": ["Project (this ERPNext user cannot read it)"]}',
            '{"not_shown": ["Project (the asker\'s own access doesn\'t include it)"]}',
        ),
        (
            "Permission denied: this ERPNext user cannot run 'Stock Ledger'",
            "Permission denied: the asker's own access doesn't let them run 'Stock Ledger'",
        ),
        ("customers that don't exist or this ERPNext user can't read.",
         "customers that don't exist or the asker's own access doesn't include."),
        ('{"rows": []}', '{"rows": []}'),
    ],
)
def test_a_refusal_is_about_the_person_asking(said: str, shown: str) -> None:
    assert erpnext_caller.in_askers_words(said, ErpnextCaller("user", "a@co.example")) == shown


@pytest.mark.parametrize(
    "caller", [None, ErpnextCaller("service"), ErpnextCaller("watch", job="j"), ErpnextCaller("supplier", supplier="S")]
)
def test_any_other_caller_keeps_the_refusal(caller: ErpnextCaller | None) -> None:
    # There the refused user is the install's own, not the person's.
    said = "Permission denied: this ERPNext user cannot read Project 'PRJ-0001'"
    assert erpnext_caller.in_askers_words(said, caller) == said


def test_the_gateway_gives_the_model_the_askers_words(erp: Any, roster: Any) -> None:
    from openexecutive.orchestrator.schedule_tools import set_session
    from openexecutive.orchestrator.session import Session

    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None

    async def call(_name: str, body: dict[str, Any]) -> Any:
        erp.calls.append((body["tool_name"], dict(body["arguments"])))
        return SimpleNamespace(content=[SimpleNamespace(text=erp.replies.pop(0))], isError=True)

    erp.session.call_tool.side_effect = call
    refusal = "Tool error: Permission denied: this ERPNext user cannot read Project 'PRJ-0001'"
    get = {"name": "erpnext__get_record", "arguments": {"doctype": "Project", "name": "PRJ-0001"}}

    async def ask(session: Any) -> str:
        erp.replies.append(refusal)
        with set_session(session):
            return str(await gateway.call_tool(dict(get)))

    # A panel turn runs as the person in it.
    panel = Session(erpnext_panel_email=OWNER, origin_channel="erpnext", read_erpnext=True)
    assert asyncio.run(ask(panel)) == (
        "Tool error: Permission denied: the asker's own access doesn't include Project 'PRJ-0001'"
    )
    assert asyncio.run(ask(_owner_turn(roster))).endswith("the asker's own access doesn't include Project 'PRJ-0001'")
