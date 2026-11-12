"""Whether a conversation read ERPNext, from the audit log
(``audit.erpnext_reads``): every owner's rows count, and a conversation that
took in another one's ERPNext figures counts too, on every later turn."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.erpnext_reads import (
    conversation_read_erpnext,
    mark_carried,
    session_read_erpnext,
)
from openexecutive.audit.logger import AuditLogger


@pytest.fixture(autouse=True)
def audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AuditLogger]:
    log = AuditLogger(db_path=tmp_path / "audit.db")
    monkeypatch.setattr(audit_logger, "_default_logger", log)
    yield log


def test_a_teammates_own_erpnext_read_counts(audit: AuditLogger) -> None:
    # A teammate's turn writes its rows as theirs; a channel adapter's next
    # message is a new Session that only the audit log can tell.
    audit.log("tool_invocation", "mcp:erpnext__get_report Accounts Receivable",
              session_id="telegram:42", private_to_person=5)
    assert session_read_erpnext("telegram:42") is True
    fresh = SimpleNamespace(session_id="telegram:42", read_erpnext=False)
    assert conversation_read_erpnext(fresh) is True and fresh.read_erpnext is True
    assert session_read_erpnext("telegram:43") is False


def test_a_carried_mark_lasts_past_the_session_that_made_it(audit: AuditLogger) -> None:
    assert session_read_erpnext("slack:D1") is False
    assert mark_carried("slack:D1", {"web-erp"}) is True
    assert session_read_erpnext("slack:D1") is True
    [row] = audit.query(event_type="erpnext_carried", all_owners=True)
    assert row.session_id == "slack:D1"


def test_no_id_no_mark(audit: AuditLogger) -> None:
    assert mark_carried("", {"web-erp"}) is False
    assert mark_carried(None, {"web-erp"}) is False
    assert session_read_erpnext(None) is False
