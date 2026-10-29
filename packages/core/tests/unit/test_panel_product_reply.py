"""ERPNext's panel names the product, not its parts, when the install has a
name (erpnext_panel._in_product_words, in_product_reply). Pilot, Oct 29, 2026:
"The installed quantities are as recorded in ERPNext."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from openexecutive.integrations import erpnext_panel


def _named(monkeypatch: pytest.MonkeyPatch, name: str | None) -> None:
    from openexecutive.utils import product_name as product

    monkeypatch.setattr(product, "configured_product_name", lambda: name)


@pytest.mark.parametrize(
    ("reply", "shown"),
    [
        ("The installed quantities are as recorded in ERPNext.",
         "The installed quantities are as recorded in the ERP."),
        ("ERPNext only records what was entered.", "The ERP only records what was entered."),
        ("Rate: left to the ERPNext price list.", "Rate: left to the ERP price list."),
        ("Done. ERPNext's records show two.", "Done. The ERP's records show two."),
        ("Ask in Open Executive or OpenExecutive itself.", "Ask in Dynocortex or Dynocortex itself."),
        ("Nothing to change here.", "Nothing to change here."),
        ("An ERPNextual word stays.", "An ERPNextual word stays."),
        # A URL, an email, an ID or a quoted name is never touched.
        ("See https://docs.erpnext.com/user/manual for it.", "See https://docs.erpnext.com/user/manual for it."),
        ("Write to support@erpnext.com.", "Write to support@erpnext.com."),
        ("Item ERPNEXT-01 is short.", "Item ERPNEXT-01 is short."),
        ("Customer 'ERPNext Services Ltd' owes 400.", "Customer 'ERPNext Services Ltd' owes 400."),
        ("Customer `ERPNext Hosting` paid.", "Customer `ERPNext Hosting` paid."),
    ],
)
def test_the_reply_names_the_product(monkeypatch: pytest.MonkeyPatch, reply: str, shown: str) -> None:
    _named(monkeypatch, "Dynocortex")
    assert erpnext_panel.in_product_reply(reply) == shown


def test_a_name_with_a_backslash_is_used_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    _named(monkeypatch, "ACME\\Desk")
    assert erpnext_panel.in_product_reply("Ask in Open Executive itself.") == "Ask in ACME\\Desk itself."


def test_a_long_reply_is_rewritten_quickly(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    _named(monkeypatch, "Dynocortex")
    reply = "ERPNext. " * 3000
    t0 = time.monotonic()
    out = erpnext_panel.in_product_reply(reply)
    assert time.monotonic() - t0 < 0.5
    assert out.count("The ERP.") == 3000


def test_without_a_name_the_reply_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    _named(monkeypatch, None)
    reply = "The installed quantities are as recorded in ERPNext."
    assert erpnext_panel.in_product_reply(reply) == reply


def test_the_panel_block_says_what_to_call_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.integrations.erpnext_drafts.offered", lambda _s: False)
    _named(monkeypatch, "Dynocortex")
    block = erpnext_panel.context_block(SimpleNamespace())
    assert "call it Dynocortex" in block and "never by the names of the parts" in block
    assert "OpenExecutive" not in block
    _named(monkeypatch, None)
    assert erpnext_panel.context_block(SimpleNamespace()) == erpnext_panel.PANEL_CONTEXT_BLOCK
