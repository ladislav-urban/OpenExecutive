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


# ---------------------------------------------------------------------------
# The security review of ladislav-urban/OpenExecutive#167


def test_nested_items_never_cut_what_follows() -> None:
    reply = (
        "Two things stand out.\n\n"
        "- The Purchase Order list you have open shows 3 late orders.\n"
        "  - The Sales Order list you have open ranks them by value.\n"
        "- Supplier Acme owes EUR 12,400 on invoice PINV-0007.\n\n"
        "Pay PINV-0007 first."
    )
    out, n = drop_stale_screen(reply, LIST_PAGE, [])
    assert n == 1
    assert "- Supplier Acme owes EUR 12,400 on invoice PINV-0007." in out
    assert out.endswith("Pay PINV-0007 first.")
    assert "Purchase Order list" not in out and "Sales Order list" not in out


def test_beside_a_dynocortex_screen_the_reply_is_left_alone() -> None:
    reply = (
        "From ERPNext I can show open orders. "
        "I can't see the Watch list you have open from this panel. Open Conversations for your watches."
    )
    assert drop_stale_screen(reply, {"screen": "/watchlist"}, []) == (reply, 0)


def test_what_it_leaves_alone() -> None:
    # A short page name shows nothing; the stale sentence still goes.
    reply = "Three orders are late. The Purchase Order list you have open shows them by supplier."
    assert drop_stale_screen(reply, {"doctype": "Sales Invoice", "name": "e"}, [])[1] == 1
    # The lead, a warning or an alternative, and a sentence that also names what is on screen.
    lead = "The Purchase Order list you have open shows them. Three are late."
    assert drop_stale_screen(lead, LIST_PAGE, []) == (lead, 0)
    warning = "Three are late. Don't use the Purchase Order list you have open; use the report instead."
    assert drop_stale_screen(warning, LIST_PAGE, []) == (warning, 0)
    both = (
        "Receivables total EUR 48,200. The Accounts Receivable report is now open beside the panel; "
        "the Sales Invoice list shows each invoice."
    )
    assert drop_stale_screen(both, LIST_PAGE, [AR_VIEW]) == (both, 0)
    # Never every item of a list.
    items = (
        "You have two ways to see them:\n\n"
        "- The Purchase Order list you have open, by supplier.\n"
        "- The Purchase Receipt report you have open, by date."
    )
    assert drop_stale_screen(items, LIST_PAGE, []) == (items, 0)


def test_curly_apostrophes_and_plurals() -> None:
    warning = "Three are late. Don’t use the Purchase Order list you have open; it lacks suppliers."
    assert drop_stale_screen(warning, LIST_PAGE, []) == (warning, 0)
    plural = "Twelve are unpaid. The Sales Invoices list you have open shows them by customer."
    assert drop_stale_screen(plural, LIST_PAGE, []) == (plural, 0)
