"""Unit tests for the static user-guide content loader.

These double as a content lint: every section in the registry must ship a
well-formed pre-authored file, and the guide is intentionally diagram-free.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator

import pytest

from openexecutive.guide import prebuilt
from openexecutive.guide.sections import GUIDE_SECTIONS
from openexecutive.utils.prebuilt_store import REQUIRED_KEYS
from openexecutive.utils.product_name import product_name_override

_REQUIRED_KEYS = REQUIRED_KEYS


def test_every_section_has_prebuilt_content() -> None:
    for spec in GUIDE_SECTIONS:
        data = prebuilt.get_prebuilt(spec.id)
        assert data is not None, f"missing prebuilt content for {spec.id}"
        assert set(data) >= _REQUIRED_KEYS, f"bad keys for {spec.id}"
        assert data["section_id"] == spec.id
        assert data["markdown"].strip(), f"empty markdown for {spec.id}"


def test_list_prebuilt_covers_registry_exactly() -> None:
    listed = set(prebuilt.list_prebuilt())
    registry = {s.id for s in GUIDE_SECTIONS}
    assert listed == registry


def test_markdown_has_no_top_level_heading() -> None:
    # The UI renders the section title; the body must not repeat it.
    for spec in GUIDE_SECTIONS:
        data = prebuilt.get_prebuilt(spec.id)
        assert data is not None
        assert not data["markdown"].lstrip().startswith(
            "#"
        ), f"{spec.id}: markdown must not start with a heading (UI renders the title)"


def test_guide_is_diagram_free() -> None:
    # The user guide is plain-language prose by design — no Mermaid.
    for spec in GUIDE_SECTIONS:
        data = prebuilt.get_prebuilt(spec.id)
        assert data is not None
        assert data["mermaid"] is None, f"{spec.id} should have no diagram"


def test_every_section_has_how_to_use() -> None:
    # Each section explains how to use the feature, not just what it is.
    for spec in GUIDE_SECTIONS:
        data = prebuilt.get_prebuilt(spec.id)
        assert data is not None
        assert (
            "**How to use:**" in data["markdown"]
        ), f"{spec.id}: add a **How to use:** walkthrough"


def test_get_prebuilt_unknown_returns_none() -> None:
    assert prebuilt.get_prebuilt("does-not-exist") is None


def test_get_prebuilt_rejects_path_traversal() -> None:
    assert prebuilt.get_prebuilt("../cache") is None
    assert prebuilt.get_prebuilt("..") is None
    assert prebuilt.get_prebuilt("a/b") is None


# ── the product's name (OE_PRODUCT_NAME) ────────────────────────────────────

# On-screen labels are quoted as the screen says them ("**Settings → Your
# Executive**"); everything else names the product by PRODUCT_WORD.
_QUOTED = re.compile(r"(`[^`\n]*`|\]\([^)\s]*\)|\*\*[^*\n]+\*\*|(?<![*\w])\*[^*\n]+\*(?![*\w]))")


@pytest.fixture
def dynocortex() -> Iterator[None]:
    token = product_name_override.set("Dynocortex")
    yield
    product_name_override.reset(token)


def test_named_swaps_the_product_word_outside_code_and_links() -> None:
    text = "Ask OpenExecutive in `OpenExecutive/packages` or [the docs](https://x.example/OpenExecutive)."
    assert prebuilt.named(text, "Dynocortex") == (
        "Ask Dynocortex in `OpenExecutive/packages` or [the docs](https://x.example/OpenExecutive)."
    )
    # A name is written as it is, never read as a regex template.
    assert prebuilt.named("OpenExecutive's mail", r"A\1B") == r"A\1B's mail"
    assert prebuilt.named("OpenExecutives and XOpenExecutive", "D") == "OpenExecutives and XOpenExecutive"


def test_sections_read_under_the_installs_name(dynocortex: None) -> None:
    from openexecutive.api.routes.guide import get_section_content
    from openexecutive.orchestrator.guide_tools import section_markdown

    served = asyncio.run(get_section_content("chat"))
    assert "Dynocortex" in served["markdown"] and "OpenExecutive" not in _prose(served["markdown"])
    assert "Dynocortex" in (section_markdown("chat") or "")
    # The store keeps the authored text.
    assert "OpenExecutive" in (prebuilt.get_prebuilt("chat") or {})["markdown"]


def test_sections_read_as_open_executive_without_a_name() -> None:
    token = product_name_override.set("")
    try:
        assert "Open Executive" in (prebuilt.get_named("chat") or {})["markdown"]
    finally:
        product_name_override.reset(token)


def _prose(markdown: str) -> str:
    return "".join(p for i, p in enumerate(_QUOTED.split(markdown)) if i % 2 == 0)


def test_the_guide_names_the_product_one_way() -> None:
    # Never "the Executive" or "Open Executive" in the guide's own words, so
    # every section follows OE_PRODUCT_NAME, here and in oe_connector's help
    # pages copied from these files.
    for spec in GUIDE_SECTIONS:
        data = prebuilt.get_prebuilt(spec.id)
        assert data is not None
        prose = _prose(data["markdown"])
        found = re.findall(r".{0,30}(?:\bExecutive\b|Open Executive).{0,10}", prose)
        assert not found, f"{spec.id}: {found}"
        assert "Executive" not in data["title"] and "Executive" not in spec.sub, spec.id


def test_the_sections_files_are_valid_json() -> None:
    from pathlib import Path

    for path in (Path(prebuilt.__file__).parent / "prebuilt").glob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))
