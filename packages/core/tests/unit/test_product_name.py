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
