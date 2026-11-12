"""``openexecutive draft-seals``: the cards' seals for oe_connector's
``oe-connector-verify-drafts --expect``, from the image's own CLI."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from openexecutive.cli import cli
from openexecutive.integrations import erpnext_drafts

SEAL = {"row_hash": "a" * 64, "chain_index": 3}


def test_prints_the_seals_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erpnext_drafts, "recorded_seals", lambda: {"card-1": SEAL})
    result = CliRunner().invoke(cli, ["draft-seals"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"card-1": SEAL}
    assert "1 recorded seals" in result.stderr


def test_an_empty_ledger_is_never_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erpnext_drafts, "recorded_seals", lambda: {})
    result = CliRunner().invoke(cli, ["draft-seals"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {}
