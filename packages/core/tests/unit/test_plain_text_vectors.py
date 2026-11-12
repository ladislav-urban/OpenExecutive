"""plain() and a card's shown text against oe_connector's own vectors
(oe_connector/tests/plain_text_vectors.json, copied here from
ladislav-urban/oe_connector#576). oe_connector refuses a draft's text unless
plain(text) == text, by a mirror of utils/prompt_blocks.plain: a change to
plain() on either side fails here or there, so the two change together and
the vectors are copied again."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openexecutive.integrations.erpnext_changes import _as_shown
from openexecutive.utils.prompt_blocks import plain

VECTORS: dict[str, Any] = json.loads((Path(__file__).parent / "plain_text_vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", VECTORS["cases"], ids=[c["note"] for c in VECTORS["cases"]])
def test_plain_matches_oe_connectors_vectors(case: dict[str, str]) -> None:
    assert plain(case["input"]) == case["plain"]
    # What plain() makes is plain already: oe_connector accepts it unchanged.
    assert plain(case["plain"]) == case["plain"]


@pytest.mark.parametrize("case", VECTORS["as_shown"], ids=[c["note"] for c in VECTORS["as_shown"]])
def test_a_cards_shown_text_matches_oe_connectors_vectors(case: dict[str, str]) -> None:
    assert _as_shown(case["input"]) == case["shown"]
    assert plain(case["shown"]) == case["shown"]
