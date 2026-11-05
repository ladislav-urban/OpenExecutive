"""Names in a reply held to the turn's ERPNext results (oe_connector's
docs/answer-check-plan.md, section 2, "Names"; PR (b)). Made-up names only."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.orchestrator import answer_check, answer_correct, answer_review
from openexecutive.orchestrator.answer_review import Review
from openexecutive.orchestrator.figure_check import ErpnextResult
from openexecutive.orchestrator.name_check import (
    allowed_edits,
    check_names,
    name_sources_for,
    within,
)

RECEIVABLES = ErpnextResult(
    "erpnext__run_report",
    {"report_name": "Accounts Receivable Summary"},
    {
        "report_name": "Accounts Receivable Summary",
        "company": "Example Trading Co",
        "columns": [
            {"fieldname": "party", "fieldtype": "Link", "label": "Customer"},
            {"fieldname": "outstanding", "fieldtype": "Currency", "label": "Outstanding"},
            {"fieldname": "remarks", "fieldtype": "Data", "label": "Remarks"},
        ],
        "rows": [
            {"party": "Ridgeway Electrical", "outstanding": 12500.0, "remarks": "same owner as Harbor Lights Ltd"},
            {"party": "Metro Transit Works", "outstanding": 35700.0},
            {"party": "Metro Transit Authority", "outstanding": 1000.0},
            {"party": "ACME", "outstanding": 10.0},
            {"party": "ACNE", "outstanding": 20.0},
        ],
        "truncated": False,
    },
)
ORDERS = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "Purchase Order", "fields": ["name", "supplier"]},
    {"doctype": "Purchase Order", "rows": [{"name": "PO-2026-0021", "supplier": "Northwind Supplies"}]},
)
DOCTYPES = {"Purchase Order": {"numeric": [], "names": ["name", "supplier"], "title_field": "supplier"}}


def _names(text: str, **kw: Any) -> dict[str, tuple[str, str | None]]:
    src = name_sources_for([RECEIVABLES, ORDERS], doctypes=DOCTYPES, **kw)
    return {n.text: (n.status, n.right) for n in check_names(text, src)}


def test_edits_allowed_scale_with_length() -> None:
    assert [allowed_edits("a" * n) for n in (5, 6, 12, 13)] == [0, 1, 1, 2]
    assert within("ridgeway electrcal", "ridgeway electrical", 2)
    assert not within("acme", "acne", 0)


def test_a_document_name_is_exact_or_not_known() -> None:
    got = _names("Raised PO-2026-0021, then PO-2026-0012 and SINV-2026-00099.")
    assert got == {
        "PO-2026-0021": ("confirmed", None),
        "PO-2026-0012": ("unconfirmed", None),  # transposed: never corrected to the other
        "SINV-2026-00099": ("unconfirmed", None),
    }


def test_a_misspelled_customer_is_written_as_erpnext_has_it() -> None:
    table = "| Customer | Owed |\n|---|---|\n| Ridgeway Electrcal | 12,500 |\n| Metro Transit Works | 35,700 |\n"
    assert _names(table) == {
        "Ridgeway Electrcal": ("near", "Ridgeway Electrical"),
        "Metro Transit Works": ("confirmed", None),
    }
    # In prose too, past a sentence's first word.
    assert _names("Yesterday Ridgeway Electrcal paid.") == {"Ridgeway Electrcal": ("near", "Ridgeway Electrical")}


def test_a_short_name_is_never_corrected_to_another() -> None:
    table = "| Customer | Owed |\n|---|---|\n| ACMF | 10 |\n| ACME | 10 |\n"
    assert _names(table) == {"ACMF": ("unconfirmed", None), "ACME": ("confirmed", None)}


def test_a_name_only_contained_in_longer_ones_is_not_known() -> None:
    table = "| Customer | Owed |\n|---|---|\n| Metro Transit | 36,700 |\n"
    assert _names(table) == {"Metro Transit": ("unconfirmed", None)}


def test_names_the_person_gave_and_other_text_pass() -> None:
    table = "| Customer | Owed |\n|---|---|\n| Bluefin Foods | 0 |\n| Example Trading Co | 0 |\n| Cobalt Mills | 0 |\n"
    got = _names(table, given=["What does Bluefin Foods owe?"], texts=["People: Cobalt Mills (our partner)"])
    assert got == {
        "Bluefin Foods": ("given", None),
        "Example Trading Co": ("confirmed", None),  # the result's company
        "Cobalt Mills": ("from_text", None),
    }


def test_a_name_in_a_remark_is_no_source() -> None:
    table = "| Customer | Owed |\n|---|---|\n| Harbor Lights Ltd | 1,000 |\n"
    assert _names(table) == {"Harbor Lights Ltd": ("unconfirmed", None)}


def test_bold_opening_a_list_item_with_a_figure_is_a_name_unless_its_a_label() -> None:
    text = (
        "- **Ridgeway Electrcal**: $12,500\n"
        "- **Total outstanding**: $48,200\n"
        "- **Harbor Lights Ltd**: $500\n"
        "- **Ridgeway Electrical** has been a customer since spring\n"
    )
    assert _names(text) == {
        "Ridgeway Electrcal": ("near", "Ridgeway Electrical"),
        "Harbor Lights Ltd": ("unconfirmed", None),
    }  # the last line has no figure, and a right name in prose isn't listed


def test_ordinary_capitalised_words_are_left_alone() -> None:
    assert _names("Accounts Receivable Summary shows Example Trading Co is fine. Overall Things Look Good.") == {}


# ---------------------------------------------------------------------------
# The whole check


@pytest.fixture
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    rows: list[Any] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: rows.append(kw.get("details")))
    return rows


def _turn() -> answer_check.AnswerCheckTurn:
    turn = answer_check.AnswerCheckTurn()
    turn.results.extend([RECEIVABLES, ORDERS])
    turn._context = {"number_format": "#,###.##", "doctypes": DOCTYPES}
    return turn


def _check(monkeypatch: pytest.MonkeyPatch, text: str, rewrite: str | None = None) -> tuple[str, list[str]]:
    calls: list[str] = []

    async def fake_rewrite(question: str, draft: str, corrections: Any, model: str) -> str | None:
        calls.append(answer_correct.describe(corrections))
        return rewrite

    async def no_review(**kw: Any) -> list[Review]:
        return [answer_review.CANNOT_TELL] * len(kw["flagged"])

    monkeypatch.setattr(answer_check, "_rewrite", fake_rewrite)
    monkeypatch.setattr(answer_review, "review", no_review)
    return asyncio.run(answer_check.check_reply(text, _turn(), question="Who owes us?", model="m")), calls


def test_a_near_name_is_written_in_by_code(monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]) -> None:
    out, calls = _check(monkeypatch, "Ridgeway Electrcal owes $12,500.")
    assert out == "Ridgeway Electrical owes $12,500."
    assert calls == []
    assert audit_rows[0]["names_flagged"] == 1 and "Ridgeway" not in str(audit_rows[0])


def test_a_name_not_in_the_results_is_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = "Receivables total $49,230.\n\n| Customer | Owed |\n|---|---|\n| Metro Transit Works | 35,700 |\n| Harbor Lights Ltd | 1,000 |\n"
    fixed = reply.replace("| Harbor Lights Ltd | 1,000 |\n", "")
    out, calls = _check(monkeypatch, reply, rewrite=fixed)
    assert calls == ['1. "Harbor Lights Ltd": not a name in the results. Leave it out.']
    assert out == fixed
    # "Another customer" in the Customer column names no record either, so
    # that rewrite goes and the code-only removal takes the row.
    out, _ = _check(monkeypatch, reply, rewrite=reply.replace("Harbor Lights Ltd", "Another customer"))
    assert out == fixed.strip()
    out, _ = _check(monkeypatch, reply, rewrite=None)
    assert out == reply.replace("| Harbor Lights Ltd | 1,000 |\n", "").strip()


def test_a_rewrite_that_names_another_unknown_record_is_thrown_away(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = "Receivables total $49,230.\n\n| Customer | Owed |\n|---|---|\n| Metro Transit Works | 35,700 |\n| Harbor Lights Ltd | 1,000 |\n"
    bad = reply.replace("Harbor Lights Ltd", "Harbor Lights Group")
    out, _ = _check(monkeypatch, reply, rewrite=bad)
    assert "Harbor" not in out
