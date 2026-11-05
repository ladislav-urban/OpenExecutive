# ruff: noqa: F811 — pytest fixtures imported from test_erpnext_drafts share their names with the test parameters.
"""The answer check (oe_connector's docs/answer-check-plan.md, PR (a)): every
figure in a reply held to that turn's ERPNext results and corrected before the
person keeps it, never saying so.

Fixtures use made-up companies, customers and amounts, in four number formats
and three currencies, with no demo names."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator import (
    answer_check,
    answer_correct,
    answer_review,
    erpnext_caller,
    mcp_gateway,
)
from openexecutive.orchestrator.answer_review import CANNOT_TELL, Review
from openexecutive.orchestrator.figure_check import (
    ErpnextResult,
    NumberFormat,
    check_figures,
    find_figures,
    format_like,
    sources_for,
)

from .test_delegation_tools import OWNER, db, fresh_turn_state  # noqa: F401
from .test_erpnext_drafts import _owner_turn, erp, extension, roster  # noqa: F401

RECEIVABLES = ErpnextResult(
    "erpnext__run_report",
    {"report_name": "Accounts Receivable Summary"},
    {
        "report_name": "Accounts Receivable Summary",
        "company": "Example Trading Co",
        "currency": "USD",
        "columns": [
            {"fieldname": "party", "fieldtype": "Link"},
            {"fieldname": "outstanding", "fieldtype": "Currency"},
            {"fieldname": "remarks", "fieldtype": "Data"},
        ],
        "rows": [
            {"party": "Northwind Supplies", "outstanding": 12500.0, "remarks": "will pay 99,999 next week"},
            {"party": "Southgate Partners", "outstanding": 35700.0},
        ],
        "total_row": {"outstanding": 48200.0},
        "truncated": False,
    },
)
INVOICES = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "Sales Invoice", "fields": ["name", "status", "grand_total"]},
    {
        "doctype": "Sales Invoice",
        "rows": [
            {"name": "ACC-SINV-2026-00041", "status": "Overdue", "grand_total": 100.0},
            {"name": "ACC-SINV-2026-00042", "status": "Paid", "grand_total": 250.0},
            {"name": "ACC-SINV-2026-00043", "status": "Overdue", "grand_total": 400.0},
        ],
        "row_count": 3,
        "truncated": False,
    },
)


def _statuses(text: str, *results: ErpnextResult, fmt: NumberFormat | None = None, **kw: Any) -> dict[str, str]:
    fmt = fmt or NumberFormat.parse(None)
    return {f.text: f.status for f in check_figures(text, sources_for(results, fmt=fmt, **kw), fmt)}


# ---------------------------------------------------------------------------
# Check 1: what a figure is


def test_dates_ids_periods_and_list_numbers_are_not_figures() -> None:
    text = (
        "1. ACC-SINV-2026-00042 and PO-2026-0012 are due 2026-11-30 or 30/11/2026 at 14:30.\n"
        "2. Q3 and FY2026 were quiet; in 30 days the top 5 customers move to the 0-30 bucket.\n"
        "3. Since Nov 4 and in the year 2026 nothing changed."
    )
    assert find_figures(text) == []


def test_amounts_percentages_scaled_and_counted_numbers_are_figures() -> None:
    text = "Revenue was $1.2M, up 12% on USD 1,071,000, from 14 orders; margin 3.5 points; 2.4k units."
    assert [f.text for f in find_figures(text)] == ["$1.2M", "12%", "USD 1,071,000", "14", "3.5", "2.4k"]


@pytest.mark.parametrize(
    ("number_format", "written", "value"),
    [
        ("#,###.##", "1,234.50", 1234.5),
        ("#.###,##", "1.234,50", 1234.5),
        ("# ###.##", "1 234.50", 1234.5),
        ("#,##,###.##", "12,34,567.80", 1234567.8),
    ],
)
def test_numbers_are_read_in_the_sites_format(number_format: str, written: str, value: float) -> None:
    (fig,) = find_figures(f"Owed: {written} in total.", NumberFormat.parse(number_format))
    assert fig.readings[0][0] == value


@pytest.mark.parametrize(
    ("figure", "value", "matches"),
    [
        ("$1.2M", 1_184_330, True),
        ("$1.25M", 1_184_330, False),
        ("43%", 42.71, True),
        ("43%", 0.4271, True),
        ("1,200", 1_184, True),
        ("1,000", 1_400, False),
        ("$48,200", -48_200, True),
    ],
)
def test_a_figure_matches_its_value_rounded_as_shown(figure: str, value: float, matches: bool) -> None:
    (fig,) = find_figures(f"It is {figure} now.")
    assert fig.matches(value) is matches


# ---------------------------------------------------------------------------
# Check 1: where a figure comes from


def test_values_totals_and_counts_are_confirmed() -> None:
    got = _statuses("North owes $12,500 of $48,200 across 2 customers.", RECEIVABLES)
    assert set(got.values()) == {"confirmed"}


def test_a_number_in_a_text_field_never_confirms() -> None:
    # A customer's own remark is not ERPNext's figure.
    assert _statuses("North will pay $99,999.", RECEIVABLES) == {"$99,999": "unconfirmed"}


def test_a_numeric_string_counts_only_in_a_numeric_column() -> None:
    result = ErpnextResult("erpnext__run_report", {}, {
        "columns": [{"fieldname": "amount", "fieldtype": "Currency"}, {"fieldname": "note", "fieldtype": "Data"}],
        "rows": [{"amount": "7,250.00", "note": "8,800"}],
    })
    assert _statuses("Booked $7,250 and $8,800.", result) == {"$7,250": "confirmed", "$8,800": "unconfirmed"}


def test_sums_groups_and_changes_are_confirmed() -> None:
    got = _statuses("Invoices total $750, of which $500 is overdue, in 2 overdue invoices.", INVOICES)
    assert set(got.values()) == {"confirmed"}
    periods = ErpnextResult("erpnext__run_report", {}, {"summary": [{"label": "This", "value": 1150.0},
                                                                    {"label": "Last", "value": 1000.0}]})
    assert _statuses("Sales rose 15% to $1,150.", periods) == {"15%": "confirmed", "$1,150": "confirmed"}
    assert _statuses("Sales rose 12% to $1,150.", periods)["12%"] == "unconfirmed"


def test_a_rows_share_of_its_column_is_confirmed() -> None:
    # 12,500 of 48,200 is 25.9%; the largest one, 35,700, is 74%.
    got = _statuses("Northwind holds 26% of receivables, and the largest debtor 74%.", RECEIVABLES)
    assert got == {"26%": "confirmed", "74%": "confirmed"}


def test_a_phone_number_before_a_name_is_not_a_count() -> None:
    assert find_figures("Call +1 555 123 4567 Northwind about it.") == []


def test_a_total_over_a_cut_off_list_is_from_truncated() -> None:
    cut = ErpnextResult("erpnext__list_records", {"doctype": "Sales Invoice"}, {
        "rows": [{"grand_total": 100.0}, {"grand_total": 200.0}], "truncated": True,
    })
    assert _statuses("The largest is $200; all of them come to $300.", cut) == {
        "$200": "confirmed", "$300": "from_truncated",
    }


def test_what_the_person_gave_and_other_text_pass() -> None:
    got = _statuses(
        "At $7,777 a month, and the 12,345 units in the contract, receivables are $48,200.",
        RECEIVABLES, given=["What if we pay 7,777 a month?"], texts=["The contract says 12,345 units."],
    )
    assert got == {"$7,777": "given", "12,345": "from_text", "$48,200": "confirmed"}


def test_a_figure_worked_out_from_the_replys_own_figures_is_confirmed() -> None:
    got = _statuses("North owes $12,500 and South $35,700; together $48,200.", RECEIVABLES)
    assert set(got.values()) == {"confirmed"}


@pytest.mark.parametrize(
    ("number_format", "written", "value", "expected"),
    [
        ("#,###.##", "$1.2M", 1_310_000, "$1.3M"),
        ("#.###,##", "1.234,50 €", 98_765.4, "98.765,40 €"),
        ("#,##,###.##", "₹12,34,567.80", 2_345_678.9, "₹23,45,678.90"),
        ("#,###.##", "43%", 41.2, "41%"),
        ("#,###.##", "USD 48,200", 51_234, "USD 51,234"),
    ],
)
def test_a_value_is_written_as_the_figure_was(number_format: str, written: str, value: float, expected: str) -> None:
    (fig,) = find_figures(f"It is {written} today.", NumberFormat.parse(number_format))
    assert format_like(fig, value) == expected


# ---------------------------------------------------------------------------
# Check 2: its answers are checked in code


def _flagged(text: str) -> Any:
    (fig,) = [f for f in check_figures(text, sources_for([RECEIVABLES, INVOICES])) if f.flagged]
    return fig


def test_a_verdict_counts_only_with_erpnexts_own_numbers() -> None:
    src = sources_for([RECEIVABLES, INVOICES])
    fig = _flagged("North owes $12,900.")
    assert answer_review.read_verdict({"verdict": "wrong", "op": "value", "operands": [12500]}, fig, src) == Review(
        "wrong", 12500.0
    )
    # A number that isn't in the results, or a sum it can't work out, tells nothing.
    assert answer_review.read_verdict({"verdict": "wrong", "op": "value", "operands": [12345]}, fig, src) == CANNOT_TELL
    assert answer_review.read_verdict({"verdict": "supported", "op": "value", "operands": [12500]}, fig, src) == CANNOT_TELL
    assert answer_review.read_verdict({"verdict": "wrong", "op": "sum", "operands": "x"}, fig, src) == CANNOT_TELL
    assert answer_review.read_verdict("garbage", fig, src) == CANNOT_TELL
    # A right value of the reviewer's own arithmetic over real numbers is not
    # written in: only one check 1 would itself confirm.
    planted = {"verdict": "wrong", "op": "ratio", "operands": [35700, 250]}
    assert answer_review.read_verdict(planted, fig, src) == CANNOT_TELL
    cherry = {"verdict": "wrong", "op": "sum", "operands": [12500, 250, 400]}
    assert answer_review.read_verdict(cherry, fig, src) == CANNOT_TELL
    # A sum code works out and the figure matches clears it.
    got = answer_review.read_verdict({"verdict": "supported", "op": "sum", "operands": [12500, 400]}, fig, src)
    assert got == Review("supported", 12900.0)


def test_rows_of_a_cut_off_result_never_add_up_to_its_total() -> None:
    cut = ErpnextResult("erpnext__list_records", {}, {"rows": [{"a": 100.0}, {"a": 211.0}], "truncated": True})
    src = sources_for([cut])
    (fig,) = find_figures("They come to $311.")
    assert answer_review.read_verdict({"verdict": "supported", "op": "sum", "operands": [100, 211]}, fig, src) == CANNOT_TELL


# ---------------------------------------------------------------------------
# The correction


REPLY = (
    "Receivables stand at $48,200 for Example Trading Co. North owes $9,999 of it.\n\n"
    "- Northwind Supplies: $12,500\n- Southgate Partners: $35,700\n\n"
    "| Customer | Owed |\n|---|---|\n| Northwind Supplies | 12,500 |\n| Total | 48,200 |\n"
)


def _figure(text: str, figure: str) -> Any:
    return next(f for f in find_figures(text) if f.text == figure)


def test_removal_takes_the_sentence_item_or_row_and_nothing_else() -> None:
    every = find_figures(REPLY)
    out = answer_correct.remove(REPLY, [_figure(REPLY, "$9,999")], every)
    assert "North owes" not in out and out.startswith("Receivables stand at $48,200 for Example Trading Co.")
    assert "- Southgate Partners: $35,700" in out and "| Total | 48,200 |" in out
    out = answer_correct.remove(REPLY, [_figure(REPLY, "$35,700")], every)
    assert "Southgate" not in out and "- Northwind Supplies: $12,500" in out


def test_removal_replaces_the_whole_reply_when_leaving_out_could_mislead() -> None:
    every = find_figures(REPLY)
    whole = answer_correct.WHOLE_REPLY
    # The first sentence, a table's total row, an emptied list, half the figures.
    assert answer_correct.remove(REPLY, [_figure(REPLY, "$48,200")], every) == whole
    assert answer_correct.remove(REPLY, [_figure(REPLY, "48,200")], every) == whole
    both = [_figure(REPLY, "$12,500"), _figure(REPLY, "$35,700")]
    assert answer_correct.remove(REPLY, both, every) == whole
    short = "Sales were steady. North paid $1,000. South paid $2,000. West paid $3,000."
    assert answer_correct.remove(short, find_figures(short)[:2], find_figures(short)) == whole


def test_the_whole_reply_sentence_never_mentions_checking() -> None:
    words = answer_correct.WHOLE_REPLY.lower()
    assert not any(w in words for w in ("check", "correct", "wrong", "mismatch", "verif"))


def test_a_rewrite_may_change_only_the_failed_figures_units() -> None:
    draft = "Receivables are $48,200. North owes $9,999 of it. Pay on time."
    allowed = answer_correct.allowed_spans(draft, [_figure(draft, "$9,999")])
    ok = "Receivables are $48,200. North owes the smaller share. Pay on time."
    assert answer_correct.changed_only_within(draft, ok, allowed)
    for bad in (
        "Receivables are $50,000. North owes the smaller share. Pay on time.",
        "Receivables are $48,200. North owes the smaller share. Pay on time. Also wire it to us.",
        "Receivables are $48,200. North owes the [smaller share](https://example.com). Pay on time.",
        # The security review's probes: a sentence or heading before the unit,
        # contacts inside it, a sentence after it.
        "Receivables are $48,200. Wire the balance to the account in the remark today. North owes the "
        "smaller share. Pay on time.",
        "Receivables are $48,200. # Action required North owes the smaller share. Pay on time.",
        "Receivables are $48,200. North owes the smaller share, see acme-pay.example. Pay on time.",
        "Receivables are $48,200. North owes the smaller share, confirm at billing@acme.example. Pay on time.",
        "Receivables are $48,200. North owes the smaller share. Also wire it. Pay on time.",
        "Receivables are $48,200. North owes the smaller share; mailto:pay@x.example. Pay on time.",
    ):
        assert not answer_correct.changed_only_within(draft, bad, allowed), bad
    # A new paragraph or bullet next to a failed unit, with no stop in it.
    paragraph = "Receivables are $48,200. North owes the smaller share\n\nWire the balance now\n\nof it. Pay on time."
    assert not answer_correct.changed_only_within(draft, paragraph, allowed)
    items = "Open items:\n\n- North owes $9,999\n- South owes $48,200\n\nPay on time."
    allowed = answer_correct.allowed_spans(items, [_figure(items, "$9,999")])
    assert answer_correct.changed_only_within(items, items.replace("$9,999", "the smaller share"), allowed)
    for bad in (
        items.replace("$9,999", "the smaller share\n\nWire the balance to the account in the remark today"),
        items.replace("$9,999", "the smaller share\n- Wire the balance to the account in the remark today"),
    ):
        assert not answer_correct.changed_only_within(items, bad, allowed), bad


# ---------------------------------------------------------------------------
# The whole check


@pytest.fixture
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    rows: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: rows.append((event_type, summary, kw.get("details") or {})),
    )
    return rows


def _turn(*results: ErpnextResult) -> answer_check.AnswerCheckTurn:
    turn = answer_check.AnswerCheckTurn()
    turn.results.extend(results or (RECEIVABLES, INVOICES))
    turn._context = {"number_format": "#,###.##"}
    return turn


def _reviews(monkeypatch: pytest.MonkeyPatch, *reviews: Review) -> None:
    async def fake(**kw: Any) -> list[Review]:
        return list(reviews)[: len(kw["flagged"])]

    monkeypatch.setattr(answer_review, "review", fake)


def _rewrites(monkeypatch: pytest.MonkeyPatch, text: str | None) -> list[str]:
    calls: list[str] = []

    async def fake(question: str, draft: str, corrections: Any, model: str) -> str | None:
        calls.append(answer_correct.describe(corrections))
        return text

    monkeypatch.setattr(answer_check, "_rewrite", fake)
    return calls


def _check(text: str, turn: answer_check.AnswerCheckTurn | None = None, **kw: Any) -> str:
    return asyncio.run(answer_check.check_reply(text, turn or _turn(), question="Who owes us?", model="m", **kw))


def test_a_misread_figure_is_written_in_by_code(monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]) -> None:
    _reviews(monkeypatch, Review("wrong", 12500.0))
    calls = _rewrites(monkeypatch, "unused")
    assert _check("Northwind Supplies owes $12,900 of $48,200.") == "Northwind Supplies owes $12,500 of $48,200."
    assert calls == []
    ((kind, summary, details),) = audit_rows
    assert kind == "answer_check" and details["substituted"] == 1 and details["changed"] is True
    # Counts only: no figure or text in the row.
    assert "12,900" not in summary + json.dumps(details) and "Northwind" not in summary + json.dumps(details)


def test_a_figure_with_no_known_value_is_left_out_by_the_correction_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _reviews(monkeypatch, CANNOT_TELL)
    fixed = "Receivables are $48,200. North owes the smaller share."
    calls = _rewrites(monkeypatch, fixed)
    assert _check("Receivables are $48,200. North owes $9,999 of it.") == fixed
    assert calls == ['1. "$9,999": no known value. Leave it out.']


def test_words_that_depend_on_a_wrong_figure_go_to_the_correction_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Substituting 12,500 for 52,500 would leave "more than South" standing.
    _reviews(monkeypatch, Review("wrong", 12500.0))
    fixed = "Receivables are $48,200. Northwind owes $12,500, less than South."
    calls = _rewrites(monkeypatch, fixed)
    assert _check("Receivables are $48,200. Northwind owes $52,500, more than South.") == fixed
    assert calls == ['1. "$52,500" should be "$12,500".']


def test_a_rewrite_that_changes_anything_else_is_thrown_away(monkeypatch: pytest.MonkeyPatch) -> None:
    _reviews(monkeypatch, CANNOT_TELL)
    _rewrites(monkeypatch, "Receivables are $48,200, all due now. North owes the smaller share.")
    assert _check("Receivables are $48,200. North owes $9,999 of it.") == "Receivables are $48,200."


def test_a_rewrite_with_a_new_unconfirmed_figure_is_thrown_away(monkeypatch: pytest.MonkeyPatch) -> None:
    _reviews(monkeypatch, CANNOT_TELL)
    _rewrites(monkeypatch, "Receivables are $48,200. North owes $8,888 of it.")
    assert _check("Receivables are $48,200. North owes $9,999 of it.") == "Receivables are $48,200."


def test_a_wrong_headline_replaces_the_whole_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    _reviews(monkeypatch, CANNOT_TELL)
    _rewrites(monkeypatch, None)
    turn = _turn()
    assert _check("Receivables are $77,777. Most of it is South's.", turn) == answer_correct.WHOLE_REPLY
    assert turn.whole_replaced


def test_a_supported_figure_stays(monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]) -> None:
    _reviews(monkeypatch, Review("supported", 12900.0))
    text = "Receivables are $48,200, and North with the largest invoice comes to $12,900."
    assert _check(text) == text
    assert audit_rows[0][2]["supported"] == 1 and audit_rows[0][2]["changed"] is False


def test_no_erpnext_read_or_the_check_off_leaves_the_reply(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any],
) -> None:
    _reviews(monkeypatch, CANNOT_TELL)
    _rewrites(monkeypatch, None)
    text = "North owes $9,999."
    assert _check(text, answer_check.AnswerCheckTurn()) == text
    assert asyncio.run(answer_check.check_reply(text, None)) == text
    monkeypatch.setenv("ERPNEXT_ANSWER_CHECK", "off")
    text = "Receivables are $48,200. North owes $9,999 of it."
    assert _check(text) == text
    assert audit_rows == []


def test_out_of_time_it_falls_back_to_code_alone(monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]) -> None:
    async def slow(**_kw: Any) -> list[Review]:
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(answer_review, "review", slow)
    _rewrites(monkeypatch, None)
    assert _check("Receivables are $48,200. North owes $9,999 of it.", budget_s=0.05) == "Receivables are $48,200."
    assert audit_rows[0][2]["code_only"] is True


def test_an_error_inside_leaves_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(**_kw: Any) -> list[Review]:
        raise RuntimeError("boom")

    monkeypatch.setattr(answer_review, "review", broken)
    assert _check("North owes $9,999.") == "North owes $9,999."


def test_a_turn_keeps_texts_without_erpnexts_own_results() -> None:
    from openexecutive.orchestrator.content_trust import wrap_untrusted

    turn = answer_check.AnswerCheckTurn()
    turn.note_texts([[
        {"type": "tool_result", "content": wrap_untrusted('{"total": 31337}', source="erpnext", author="x")},
        {"type": "tool_result", "content": "The contract runs to 12,345 units."},
    ]])
    assert turn.texts == ["The contract runs to 12,345 units."]
    # A result cut to the reading budget lost its closing tag: still left out.
    cut = wrap_untrusted('{"remarks": "settle 48,200 by Friday"}', source="erpnext", author="x")
    turn.note_texts([[{"type": "tool_result", "content": cut[: cut.index("</")] + "…[cut]"}]])
    assert turn.texts == ["The contract runs to 12,345 units."]


def test_a_long_conversation_is_read_quickly() -> None:
    import time

    table = "| Item | Qty | Amount |\n|---|---|---|\n" + "".join(
        f"| ITEM-{i:05d} | {i % 97} | {i * 13.5:,.2f} |\n" for i in range(30_000)
    )
    started = time.monotonic()
    sources_for([RECEIVABLES], texts=[table, table], given=[table])
    assert time.monotonic() - started < 30  # was minutes before the cap


# ---------------------------------------------------------------------------
# The gateway: the context tool runs only inside the check's marker, as the
# turn's own caller, and the model never sees it


def test_the_context_tool_runs_only_inside_the_checks_marker(erp: Any, roster: Any) -> None:
    from openexecutive.orchestrator.schedule_tools import set_session

    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    context_call = {"name": "erpnext__answer_check_context", "arguments": {"doctypes": ["Sales Invoice"]}}

    async def go(call: dict[str, Any], marked: bool) -> str:
        with set_session(_owner_turn(roster)):
            if marked:
                with erpnext_caller.answer_check_run():
                    return await gateway.call_tool(call)
            return await gateway.call_tool(call)

    assert "answer check" in json.loads(asyncio.run(go(context_call, False)))["error"]
    other = {"name": "erpnext__get_record", "arguments": {"doctype": "ToDo", "name": "x"}}
    assert "answer check" in json.loads(asyncio.run(go(other, True)))["error"]
    assert erp.calls == []
    erp.replies.append(json.dumps({"number_format": "#.###,##", "doctypes": {}}))
    assert json.loads(asyncio.run(go(context_call, True)))["number_format"] == "#.###,##"
    assert [name for name, _ in erp.calls] == ["erpnext__answer_check_context"]
    assert erp.claims()["sub"] == OWNER


def test_the_model_is_never_offered_the_context_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit._erpnext_pins import pin_listing

    listing = "".join(
        f"## {name}\n**Description:** {name}\n\n" for name in ("erpnext__answer_check_context", "erpnext__run_report")
    )
    pin_listing(monkeypatch, listing)
    kept = mcp_gateway._drop_blocked_tools(listing)
    assert "erpnext__run_report" in kept and "erpnext__answer_check_context" not in kept


def test_a_turn_being_checked_keeps_its_reads_and_the_context_from_erpnext(erp: Any, roster: Any) -> None:
    from openexecutive.orchestrator.schedule_tools import set_session

    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    turn = answer_check.AnswerCheckTurn()

    async def call(_name: str, body: dict[str, Any]) -> Any:
        erp.calls.append((body["tool_name"], dict(body["arguments"])))
        return SimpleNamespace(content=[SimpleNamespace(text=erp.replies.pop(0))], isError=False)

    erp.session.call_tool.side_effect = call
    erp.replies.append(json.dumps(INVOICES.payload))
    erp.replies.append(json.dumps({"number_format": "#.###,##", "doctypes": {"Sales Invoice": {"numeric": []}}}))

    async def go() -> dict[str, Any]:
        with set_session(_owner_turn(roster)), answer_check.collect(turn):
            await gateway.call_tool({"name": "erpnext__list_records", "arguments": dict(INVOICES.arguments)})
            return await answer_check._context(turn)

    context = asyncio.run(go())
    assert context["number_format"] == "#.###,##"
    assert [r.tool for r in turn.results] == ["erpnext__list_records"]  # the context isn't a figure source
    assert erp.calls[-1][1]["doctypes"] == ["Sales Invoice"]


def test_the_pin_holds_oe_connectors_definition() -> None:
    from openexecutive.orchestrator import erpnext_tool_pin

    served = {
        "name": "answer_check_context",
        "description": "For OpenExecutive's answer check only, never to answer with: this site's number "
        "format and currencies, which readable fields of the given DocTypes (and their child tables) "
        "hold numbers and which hold names, and the headline figures that have a standard report.",
        "inputSchema": {
            "type": "object",
            "properties": {"doctypes": {"type": "array", "items": {"type": "string"}, "maxItems": 30}},
            "required": ["doctypes"],
            "additionalProperties": False,
        },
    }
    assert erpnext_tool_pin.definition_digest(served) == erpnext_tool_pin.TOOL_DIGESTS["answer_check_context"]


# ---------------------------------------------------------------------------
# The Executive: the reply streams, then the checked text replaces it


@pytest.fixture
def quiet_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    from openexecutive.audit import AuditLogger, set_audit_logger

    set_audit_logger(AuditLogger(tmp_path / "audit.db"))
    yield
    set_audit_logger(None)


def test_stream_chat_replaces_the_reply_and_keeps_the_checked_text(
    monkeypatch: pytest.MonkeyPatch, quiet_audit: None,
) -> None:
    from openexecutive.memory.company_profile import CompanyProfile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    monkeypatch.setattr(answer_check, "_context", _no_context)
    _reviews(monkeypatch, Review("wrong", 12500.0))

    async def loop(*_a: Any, **_kw: Any) -> AsyncIterator[Any]:
        answer_check.note_erpnext_result(
            "erpnext__run_report", RECEIVABLES.arguments, json.dumps(RECEIVABLES.payload), failed=False,
        )
        yield "Northwind Supplies owes "
        yield "$12,900 of $48,200."

    session = Session(session_id="sess-check", company_profile=CompanyProfile())

    async def go() -> list[Any]:
        with answer_check.collect(answer_check.AnswerCheckTurn()):
            return [item async for item in Executive().stream_chat(user_message="Who owes us?", session=session)]

    with patch.object(Executive, "_stream_agent_loop", new=loop):
        items = asyncio.run(go())
    replaced = [i for i in items if isinstance(i, dict) and i.get("type") == "reply_replaced"]
    assert replaced == [{
        "type": "reply_replaced", "content": "Northwind Supplies owes $12,500 of $48,200.", "session_id": "sess-check",
    }]
    assert session.get_recent_history()[-1]["content"] == "Northwind Supplies owes $12,500 of $48,200."


async def _no_context(_turn: Any) -> dict[str, Any]:
    return {}


def test_chat_returns_the_checked_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    seen: list[Any] = []

    async def stream(self: Any, **_kw: Any) -> AsyncIterator[Any]:
        seen.append(answer_check.current())
        yield "draft"
        yield {"type": "reply_replaced", "content": "checked"}

    monkeypatch.setattr(Executive, "stream_chat", stream)
    out = asyncio.run(Executive().chat(user_message="hi", session=Session(session_id="s")))
    assert out == "checked"
    assert isinstance(seen[0], answer_check.AnswerCheckTurn)  # its own turn when none was bound
