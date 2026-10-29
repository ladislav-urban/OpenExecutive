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
