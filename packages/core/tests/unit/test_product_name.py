"""OE_PRODUCT_NAME on the API (utils.product_name), by the web app's rules."""
from __future__ import annotations

import pytest

from openexecutive.utils.product_name import DEFAULT_PRODUCT_NAME, product_name


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("Dynocortex", "Dynocortex"),
        ("  Acme   Desk ", "Acme Desk"),
        ("", DEFAULT_PRODUCT_NAME),
        (None, DEFAULT_PRODUCT_NAME),
        ("x" * 41, DEFAULT_PRODUCT_NAME),
        ("<b>Evil</b>", DEFAULT_PRODUCT_NAME),
        ('Say "hi"', DEFAULT_PRODUCT_NAME),
        ("Line\nbreak", "Line break"),
    ],
)
def test_product_name(raw: str | None, shown: str) -> None:
    assert product_name(raw if raw is not None else "") == shown


def test_configured_name_is_none_until_one_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.utils import product_name as product

    monkeypatch.setattr(product, "_clean", product._clean)
    token = product.product_name_override.set("Dynocortex")
    try:
        assert product.configured_product_name() == "Dynocortex"
        assert product_name() == "Dynocortex"
    finally:
        product.product_name_override.reset(token)


def test_block_0_names_the_product_only_when_one_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.prompts.cache_manager import build_system_blocks
    from openexecutive.utils import product_name as product

    monkeypatch.setattr(product, "configured_product_name", lambda: None)
    plain = build_system_blocks()[0]["text"]
    assert "What to call things" not in plain
    monkeypatch.setattr(product, "configured_product_name", lambda: "Dynocortex")
    named = build_system_blocks()[0]["text"]
    assert "## What to call things" in named and "**Dynocortex**" in named
    assert "Never name the parts it is built from, such as ERPNext or Open Executive" in named
    # The same text every turn: block 0 keeps its 1h cache.
    assert build_system_blocks()[0]["text"] == named


def test_an_eval_fails_a_reply_that_names_a_part() -> None:
    from openexecutive.evals.scenarios import forbidden_words_in

    scenario = {"forbidden_reply_words": ["ERPNext", "Open Executive", "OpenExecutive"]}
    assert forbidden_words_in(scenario, "ERPNext only records what was entered.") == ["ERPNext"]
    assert forbidden_words_in(scenario, "Ask in open executive itself") == ["Open Executive"]
    assert forbidden_words_in(scenario, "Dynocortex only records what was entered.") == []
    assert forbidden_words_in({}, "ERPNext") == []


def _named(monkeypatch: pytest.MonkeyPatch, name: str | None) -> None:
    from openexecutive.utils import product_name as product

    monkeypatch.setattr(product, "configured_product_name", lambda: name)


def test_connected_systems_words_what_people_hear_in_the_products_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.prompts import connected_systems as cs

    phrases = cs._people_words("Dynocortex")
    _named(monkeypatch, None)
    # Each phrase still exists in the plain text (so the rewording can't rot).
    plain = " ".join([cs._ERPNEXT_PRINCIPAL_ONLY, cs._ERPNEXT_PRICED_DRAFTS,
                      cs._erpnext_drafts(["Quotation", "Purchase Order"])])
    for before, _after in phrases:
        assert before in plain, before
    assert cs.in_product_words(plain) == plain
    _named(monkeypatch, "Dynocortex")
    named = cs.in_product_words(plain)
    for before, after in phrases:
        assert before not in named and after in named
    assert "OpenExecutive" not in named and "approval card on Today in Dynocortex" in named


def test_the_panel_blocks_name_the_product(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from openexecutive.integrations import erpnext_panel

    _named(monkeypatch, "Dynocortex")
    monkeypatch.setattr("openexecutive.integrations.erpnext_drafts.offered", lambda _s: False)
    block = erpnext_panel.context_block(SimpleNamespace())
    assert "OpenExecutive" not in block and "the Dynocortex app" in block
    _named(monkeypatch, None)
    assert erpnext_panel.context_block(SimpleNamespace()) == erpnext_panel.PANEL_CONTEXT_BLOCK


def test_a_chip_names_the_product_for_its_own_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator.tool_labels import labels_for

    _named(monkeypatch, None)
    assert labels_for("erpnext__list_records")[0] == "Listed records · ERPNext"
    _named(monkeypatch, "Dynocortex")
    assert labels_for("erpnext__list_records")[0] == "Listed records · Dynocortex"
    assert labels_for("erpnext__get_record")[0].endswith("· Dynocortex")
    # Someone else's server keeps its name.
    assert labels_for("google_workspace__frobnicate_things")[0].endswith("· Google")


def test_the_panels_refusals_name_the_product(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.integrations import erpnext_panel

    _named(monkeypatch, None)
    assert "OpenExecutive" in erpnext_panel._not_set_up() and "OpenExecutive" in erpnext_panel._not_on_roster()
    _named(monkeypatch, "Dynocortex")
    for text in (erpnext_panel._not_set_up(), erpnext_panel._not_on_roster()):
        assert "Dynocortex" in text and "OpenExecutive" not in text and "ERPNext" not in text
