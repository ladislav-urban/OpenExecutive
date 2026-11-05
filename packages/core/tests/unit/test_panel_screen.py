"""A panel reply's sentences about the person's screen, held to the page or
what the answer opened (integrations.panel_screen; the pilot's Nov 5, 2026
second retake, learning-2)."""
from __future__ import annotations

from openexecutive.integrations.panel_screen import drop_stale_screen

LIST_PAGE = {"doctype": "Sales Invoice"}
AR_VIEW = {"kind": "report", "report": "Accounts Receivable"}


def test_the_page_the_answer_replaced_is_never_pointed_at() -> None:
    reply = (
        "Open the Accounts Receivable report, now open beside the panel, and age it by due date. "
        "The Sales Invoice list you have open also works: filter it by customer.\n\n"
        "Filter by customer to see one account."
    )
    out, n = drop_stale_screen(reply, LIST_PAGE, [AR_VIEW])
    assert n == 1
    assert "Sales Invoice list you have open" not in out
    assert out.startswith("Open the Accounts Receivable report, now open beside the panel")
    assert out.endswith("Filter by customer to see one account.")


def test_what_is_on_screen_stands() -> None:
    # The page itself, when nothing else was opened.
    reply = "The Sales Invoice list you have open shows 12 unpaid invoices."
    assert drop_stale_screen(reply, LIST_PAGE, []) == (reply, 0)
    # The view the answer opened.
    reply = "The Accounts Receivable report beside the panel ages them by due date."
    assert drop_stale_screen(reply, LIST_PAGE, [AR_VIEW]) == (reply, 0)
    # A sentence that names nothing.
    reply = "The report beside the panel ages them by due date."
    assert drop_stale_screen(reply, LIST_PAGE, [AR_VIEW]) == (reply, 0)
    # No screen words at all.
    reply = "The Purchase Order list holds three open orders."
    assert drop_stale_screen(reply, LIST_PAGE, []) == (reply, 0)


def test_a_screen_never_open_is_not_described() -> None:
    reply = "Three orders are late. The Purchase Order list you have open shows them by supplier."
    assert drop_stale_screen(reply, LIST_PAGE, []) == ("Three orders are late.", 1)


def test_a_reply_that_would_be_left_empty_is_kept() -> None:
    reply = "The Purchase Order list you have open shows them."
    assert drop_stale_screen(reply, LIST_PAGE, []) == (reply, 0)
