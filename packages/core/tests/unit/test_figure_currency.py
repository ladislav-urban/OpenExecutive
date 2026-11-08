"""The answer check's currency marks, multiples and counts in words (the
pilot's Nov 5, 2026 retake: a € sign on a dollar total, "8 times the last
order of 6", "six customers" over five). Made-up companies and amounts."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.orchestrator import answer_check, answer_review
from openexecutive.orchestrator.answer_review import Review
from openexecutive.orchestrator.figure_check import (
    ErpnextResult,
    check_figures,
    find_figures,
    sources_for,
)

ORDER = ErpnextResult(
    "erpnext__get_record",
    {"doctype": "Purchase Order", "name": "PUR-ORD-2026-02093"},
    {
        "doctype": "Purchase Order", "name": "PUR-ORD-2026-02093",
        "company_currency": "USD", "document_currency": "EUR",
        "record": {"grand_total": 3630.0, "base_grand_total": 3848.89, "total_qty": 50.0},
    },
)
ORDERS = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "Purchase Order", "fields": ["name", "supplier", "total_qty", "base_grand_total"]},
    {
        "doctype": "Purchase Order", "currency": "USD",
        "rows": [
            {"name": "PO-1", "supplier": "Orion Switchgear", "total_qty": 50.0, "base_grand_total": 600.0},
            {"name": "PO-2", "supplier": "Orion Switchgear", "total_qty": 600.0, "base_grand_total": 7200.0},
            {"name": "PO-3", "supplier": "Lumen Supply", "total_qty": 30.0, "base_grand_total": 900.0},
        ],
        "truncated": False,
    },
)


def _statuses(text: str, *results: ErpnextResult) -> dict[str, str]:
    src = sources_for(results, currencies={"USD": {"symbol": "$"}, "EUR": {"symbol": "€"}})
    return {f.text: f.status for f in check_figures(text, src)}


def test_an_amount_in_its_documents_currency_or_the_companys_is_right() -> None:
    assert _statuses("It cost €3,630.00, which is $3,848.89.", ORDER) == {
        "€3,630.00": "confirmed", "$3,848.89": "confirmed",
    }


def test_a_company_currency_total_with_the_documents_sign_is_wrong_currency() -> None:
    src = sources_for([ORDER], currencies={"USD": {"symbol": "$"}, "EUR": {"symbol": "€"}})
    (fig,) = check_figures("The order came to €3,848.89.", src)
    assert (fig.status, fig.currency_fix) == ("wrong_currency", "$")


def test_a_multiple_is_held_to_the_two_figures_it_compares() -> None:
    assert _statuses("That order is 12 times the size of the last one.", ORDERS) == {"12": "confirmed"}
    assert _statuses("That order is 11 times the size of the last one.", ORDERS) == {"11": "unconfirmed"}
    # How often something happened isn't a multiple, nor a figure.
    assert find_figures("We chased them 3 times this month.") == []


def test_a_count_in_words_is_checked_like_one_in_digits() -> None:
    assert _statuses("Three orders are open.", ORDERS) == {"Three": "confirmed"}
    assert _statuses("Seven orders are open.", ORDERS) == {"Seven": "unconfirmed"}
    # Words that aren't counts of anything stay words.
    assert find_figures("There are two ways to read this, and three steps.") == []


def test_the_check_rewrites_the_mark_alone_without_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    called: list[Any] = []

    async def review(**kw: Any) -> list[Review]:
        called.append(kw)
        return [answer_review.CANNOT_TELL] * len(kw["flagged"])

    monkeypatch.setattr(answer_review, "review", review)
    turn = answer_check.AnswerCheckTurn()
    turn.results.append(ORDER)
    turn._context = {"number_format": "#,###.##", "currencies": {"USD": {"symbol": "$"}, "EUR": {"symbol": "€"}}}
    out = asyncio.run(answer_check.check_reply("The order came to €3,848.89.", turn, question="q", model="m"))
    assert out == "The order came to $3,848.89."
    assert called == []


# The pilot's Nov 6, 2026 retake (margin drop, step 4): "The 162 LED panels"
# was read as 162 in a currency "LED", not the report's USD, and written back
# as "The 162 $ panels". Only a currency's ISO 4217 code is a currency.
BY_ITEM = ErpnextResult(
    "erpnext__run_report",
    {"report_name": "Gross Profit"},
    {
        "report_name": "Gross Profit", "currency": "USD",
        "columns": [
            {"fieldname": "item_code", "fieldtype": "Link"},
            {"fieldname": "qty", "fieldtype": "Float"},
            {"fieldname": "selling_amount", "fieldtype": "Currency"},
        ],
        "rows": [
            {"item_code": "DEMO-LED-40", "qty": 162.0, "selling_amount": 3402.0},
            {"item_code": "DEMO-PCB-12", "qty": 40.0, "selling_amount": 600.0},
        ],
        "truncated": False,
    },
)


def test_capitals_after_a_number_are_a_currency_only_as_an_iso_code() -> None:
    for text in ("The 162 LED panels sold for $3,402.", "Stock: 40 LED, 12 PCB and 7 USB boards."):
        quantities = [f for f in find_figures(text) if "$" not in f.text]
        assert quantities and all(f.currency_text == "" and f.text.isdigit() for f in quantities), text
    for text, mark in (("It came to EUR 1,200.", "EUR"), ("It came to 1,200 USD.", "USD"), ("It was $1,200.", "$")):
        (fig,) = find_figures(text)
        assert fig.currency_text == mark, text
    # A code that is also a word in capitals is never a currency in a reply.
    for text in ("We kept the 40 TOP sellers in stock.", "Order 12 CUP holders."):
        assert all(f.currency_text == "" for f in find_figures(text)), text


def test_a_quantity_before_an_items_code_stays_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)

    async def review(**kw: Any) -> list[Review]:
        return [answer_review.CANNOT_TELL] * len(kw["flagged"])

    monkeypatch.setattr(answer_review, "review", review)
    turn = answer_check.AnswerCheckTurn()
    turn.results.append(BY_ITEM)
    turn._context = {"number_format": "#,###.##", "currencies": {"USD": {"symbol": "$"}}}
    text = "Yes, it was worth it. The 162 LED panels sold at cost brought in $3,402 of cash with no loss."
    assert asyncio.run(answer_check.check_reply(text, turn, question="q", model="m")) == text
    # A real code on the wrong amount is still corrected.
    wrong = "The panels brought in 3,402 EUR."
    assert asyncio.run(answer_check.check_reply(wrong, turn, question="q", model="m")) == "The panels brought in 3,402 $."
