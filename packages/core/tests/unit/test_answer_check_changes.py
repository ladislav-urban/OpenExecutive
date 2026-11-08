"""The principal's record of what the answer check changed (the owner, Nov 8,
2026): only the sentences it changed, before and after, with each figure and
why; private to the principal; their text kept 30 days. Made-up figures."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from openexecutive.audit.logger import AuditLogger
from openexecutive.orchestrator import answer_check, answer_correct, answer_review
from openexecutive.orchestrator.answer_review import Review
from openexecutive.orchestrator.figure_check import ErpnextResult

RECEIVABLES = ErpnextResult(
    "erpnext__run_report",
    {"report_name": "Accounts Receivable Summary"},
    {
        "report_name": "Accounts Receivable Summary", "currency": "USD",
        "columns": [{"fieldname": "party", "fieldtype": "Link"}, {"fieldname": "outstanding", "fieldtype": "Currency"}],
        "rows": [{"party": "Northwind Supplies", "outstanding": 12500.0}, {"party": "Southgate Partners", "outstanding": 35700.0}],
        "total_row": {"outstanding": 48200.0},
        "truncated": False,
    },
)


@pytest.fixture
def audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuditLogger:
    logger = AuditLogger(db_path=tmp_path / "audit.db")
    logger.initialize_db()
    monkeypatch.setattr("openexecutive.audit.logger._default_logger", logger)
    return logger


def _check(text: str, monkeypatch: pytest.MonkeyPatch) -> str:
    async def review(**kw: Any) -> list[Review]:
        return [Review("wrong", 12500.0) for _ in kw["flagged"]]

    monkeypatch.setattr(answer_review, "review", review)
    turn = answer_check.AnswerCheckTurn()
    turn.results.append(RECEIVABLES)
    turn._context = {"number_format": "#,###.##"}
    return asyncio.run(answer_check.check_reply(text, turn, question="Who owes us?", model="m"))


def _rows(audit: AuditLogger, event_type: str) -> list[sqlite3.Row]:
    with sqlite3.connect(audit._db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM audit_log WHERE event_type = ? ORDER BY id", (event_type,)).fetchall()


def test_the_changed_sentence_alone_is_recorded_with_its_figure_and_why(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = "Receivables are $48,200. Northwind Supplies owes $12,900 of it. Southgate owes the rest."
    out = _check(reply, monkeypatch)
    assert out == "Receivables are $48,200. Northwind Supplies owes $12,500 of it. Southgate owes the rest."
    (row,) = _rows(audit, "answer_check_changes")
    assert row["private_to_principal"] == 1
    assert "12,900" not in row["summary"] and "12,900" not in (row["details_json"] or "")
    (change,) = json.loads(row["full_json"])["changes"]
    assert change["before"] == "Northwind Supplies owes $12,900 of it."
    assert change["after"] == "Northwind Supplies owes $12,500 of it."
    assert change["figures"] == [{
        "as_written": "$12,900",
        "why": "no value in the turn's ERPNext results matches it; written as $12,500",
    }]
    # The rest of the reply is not in the record.
    assert "Southgate" not in row["full_json"] and "48,200" not in row["full_json"]
    # The counts row stays as it was: counts only.
    (counts,) = _rows(audit, "answer_check")
    assert counts["full_json"] is None


def test_a_reply_the_check_left_alone_records_nothing(audit: AuditLogger, monkeypatch: pytest.MonkeyPatch) -> None:
    _check("Receivables are $48,200. Northwind Supplies owes $12,500 of it.", monkeypatch)
    assert _rows(audit, "answer_check_changes") == []


def test_the_sentences_go_after_thirty_days(audit: AuditLogger, monkeypatch: pytest.MonkeyPatch) -> None:
    old = audit.log("answer_check_changes", "old", full={"changes": [{"before": "x"}]}, private=True)
    with sqlite3.connect(audit._db_path) as conn:
        stamp = (datetime.now(UTC) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute("UPDATE audit_log SET ts = ? WHERE id = ?", (stamp, old))
    _check("Receivables are $48,200. Northwind Supplies owes $12,900 of it.", monkeypatch)
    rows = {r["id"]: r for r in _rows(audit, "answer_check_changes")}
    assert rows[old]["full_json"] is None and rows[old]["summary"] == "old"  # the row stays, its text goes
    assert all(r["full_json"] for i, r in rows.items() if i != old)


def test_a_whole_reply_replaced_records_the_sentences_it_held() -> None:
    reasons = [("$9,999", "no value in the turn's ERPNext results matches it; left out")]
    (change,) = answer_check.changed_sentences(
        "North owes $9,999. South owes $1.", answer_correct.WHOLE_REPLY, reasons,
    )
    assert change["after"] == answer_correct.WHOLE_REPLY
    assert change["figures"] == [{"as_written": "$9,999", "why": reasons[0][1]}]
