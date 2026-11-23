"""What moved (integrations.erpnext_evidence): the records behind an ERPNext
alert, kept apart from triage and shown on the alert's card."""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import PRIVATE_ALERT_TAG
from openexecutive.integrations import erpnext_evidence as evidence

from . import test_delegation_tools, test_erpnext_events
from .test_erpnext_events import _post

db = test_delegation_tools.db
site = test_erpnext_events.site

# A byte copy of oe_connector's tests/fixtures/openexecutive/alert_evidence_vector.json.
VECTOR_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "erpnext" / "alert_evidence_vector.json"
VECTOR = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))["event"]
TABLES = VECTOR["evidence"]


@pytest.fixture
def alerts_db(db: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(alert_store, "DB_PATH", db)
    alert_store.initialize_db(db)
    return db


MONTH = time.strftime("%Y-%m")


def _keep(kind: str, key: str, raw: Any, readers: list[int] | None = None) -> bool:
    return evidence.remember(kind, key, raw, readers=readers or [])


def _alert(kind: str, key: str, **row: Any) -> Any:
    # As the pipeline stores an ERPNext event's alert (a private one), this month.
    fields: dict[str, Any] = {
        "source": "erpnext", "external_id": f"erpnext:{kind}:{key}:{MONTH}",
        "dedup_key": f"private:erpnext:{kind}:{key}", "topic_tags": [PRIVATE_ALERT_TAG], **row,
    }
    alert_id = alert_store.insert_alert(severity="medium", headline=f"{key} needs a look", body="", **fields)
    assert alert_id is not None
    return alert_store.get_alert(alert_id)


def test_the_shared_vector_is_taken_as_sent() -> None:
    assert evidence.parse_evidence(TABLES) == TABLES


def _with(change: Any) -> list[dict[str, Any]]:
    tables = copy.deepcopy(TABLES)
    change(tables)
    return tables


def _set(path: tuple[Any, ...], value: Any) -> Any:
    def change(tables: Any) -> None:
        target = tables
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
    return change


@pytest.mark.parametrize("raw", [
    None, "What moved", {}, [],
    # Over the caps.
    [TABLES[0]] * 4,
    _with(lambda t: t[0]["rows"].extend([t[0]["rows"][0]] * 9)),
    _with(lambda t: t[0]["columns"].extend({"key": f"c{i}", "label": "x", "type": "text"} for i in range(4))),
    _with(_set((0, "heading"), "h" * 121)),
    _with(_set((0, "columns", 0, "label"), "l" * 41)),
    _with(_set((0, "rows", 0, "document", "name"), "n" * 141)),
    # Not the shape.
    _with(_set((0, "currency"), "dollars")),
    _with(_set((0, "heading"), " ")),
    _with(_set((0, "columns", 0, "type"), "html")),
    _with(_set((0, "columns", 0, "key"), "Document")),
    _with(_set((0, "columns", 1, "key"), "document")),
    _with(_set((0, "extra"), 1)),
    _with(_set((0, "rows", 0, "unknown"), "x")),
    _with(_set((0, "rows", 0, "amount"), "4,200.00")),
    _with(_set((0, "rows", 0, "amount"), True)),
    _with(_set((0, "rows", 0, "amount"), float("inf"))),
    _with(_set((0, "rows", 0, "date"), "2026-02-30")),
    _with(_set((0, "rows", 0, "date"), "yesterday")),
    _with(_set((0, "rows", 0, "document"), "ACC-PINV-2026-00041")),
    _with(_set((0, "rows", 0, "document", "url"), "https://example.com")),
    _with(_set((1, "rows", 0, "name"), {"doctype": "Item Group", "name": "Cable & Wire"})),
])
def test_evidence_not_in_the_shape_or_over_a_cap_is_dropped_whole(raw: Any) -> None:
    assert evidence.parse_evidence(raw) is None


def test_a_missing_cell_is_empty_and_any_cell_may_be_empty() -> None:
    tables = _with(lambda t: t[0]["rows"][0].pop("date"))
    tables[1]["rows"][0]["margin"] = None
    parsed = evidence.parse_evidence(tables)
    assert parsed is not None
    assert parsed[0]["rows"][0]["date"] is None and parsed[1]["rows"][0]["margin"] is None


def test_a_signed_event_keeps_its_evidence_out_of_triage(alerts_db: Path, site: SimpleNamespace) -> None:
    assert _post(site, {"events": [VECTOR]}).json() == {"accepted": 1}
    (event,) = site.scheduled
    # The event's own text reaches triage; the records behind it don't.
    assert "ACC-PINV-2026-00041" not in event.body and "Cable & Wire" not in event.body
    assert evidence.evidence_for(_alert(VECTOR["kind"], VECTOR["key"])) == TABLES


def test_an_event_with_bad_evidence_is_kept_without_it(alerts_db: Path, site: SimpleNamespace) -> None:
    bad = {**VECTOR, "evidence": _with(_set((0, "currency"), "dollars"))}
    assert _post(site, {"events": [bad]}).json() == {"accepted": 1}
    assert len(site.scheduled) == 1
    assert evidence.evidence_for(_alert(VECTOR["kind"], VECTOR["key"])) is None


def test_an_event_without_evidence_clears_what_its_alert_had(alerts_db: Path, site: SimpleNamespace) -> None:
    assert _post(site, {"events": [VECTOR]}).json() == {"accepted": 1}
    plain = {k: v for k, v in VECTOR.items() if k != "evidence"}
    assert _post(site, {"events": [plain]}).json() == {"accepted": 1}
    assert evidence.evidence_for(_alert(VECTOR["kind"], VECTOR["key"])) is None


def test_evidence_that_breaks_the_parser_never_loses_the_batch(alerts_db: Path, site: SimpleNamespace) -> None:
    hostile = [
        _with(_set((0, "columns", 0, "type"), [])),
        _with(_set((0, "columns", 0, "type"), {})),
        _with(_set((0, "rows", 0, "amount"), 10**400)),
    ]
    events = [{**VECTOR, "key": f"K{i}", "evidence": raw} for i, raw in enumerate(hostile)]
    assert _post(site, {"events": [*events, VECTOR]}).json() == {"accepted": 4}
    assert len(site.scheduled) == 4
    assert evidence.evidence_for(_alert(VECTOR["kind"], VECTOR["key"])) == TABLES


def test_a_failure_keeping_evidence_never_loses_the_event(
    alerts_db: Path, site: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("disk full")

    monkeypatch.setattr(evidence, "remember", boom)
    assert _post(site, {"events": [VECTOR]}).json() == {"accepted": 1}
    assert len(site.scheduled) == 1


@pytest.mark.parametrize("text", ["\ud800", "a\u0000b", "bell\u0007", "\u202eevil", "\u200b"])
def test_text_that_isnt_plain_is_dropped(text: str) -> None:
    assert evidence.parse_evidence(_with(_set((0, "heading"), text))) is None
    if text != "​":  # a text cell may be empty; a heading may not
        assert evidence.parse_evidence(_with(_set((1, "rows", 0, "name"), text))) is None


@pytest.mark.parametrize("key", ["constructor", "__proto__", "_x", "9a"])
def test_a_column_key_must_be_a_plain_name(key: str) -> None:
    assert evidence.parse_evidence(_with(_set((0, "columns", 0, "key"), key))) is None


def test_whitespace_in_text_is_folded_to_one_space() -> None:
    parsed = evidence.parse_evidence(_with(_set((1, "rows", 0, "name"), "Cable\t&\n Wire")))
    assert parsed is not None and parsed[1]["rows"][0]["name"] == "Cable & Wire"


def test_the_largest_well_formed_plain_shape_is_kept() -> None:
    columns = [{"key": f"c{i}", "label": "l" * 40, "type": "text"} for i in range(6)]
    table = {"heading": "h" * 120, "currency": None, "columns": columns,
             "rows": [{f"c{i}": "x" * 140 for i in range(6)} for _ in range(10)]}
    assert evidence.parse_evidence([table] * 3) is not None


def test_evidence_too_big_to_store_is_dropped() -> None:
    columns = [{"key": f"c{i}", "label": "x", "type": "text"} for i in range(6)]
    big = {"heading": "h", "currency": None, "columns": columns,
           "rows": [{f"c{i}": "\U0001f600" * 140 for i in range(6)} for _ in range(10)]}
    assert evidence.parse_evidence([big] * 3) is None
    assert evidence.parse_evidence([{**big, "rows": big["rows"][:1]}]) is not None


def _seen(kind: str, key: str, *, named: str = "", month: str = MONTH) -> Any:
    # An alert as the pipeline keys it, without the store's one-a-month dedup.
    ref = f"erpnext:{kind}:{key}"
    return SimpleNamespace(
        source="erpnext", external_id=f"{ref}:{month}", dedup_key=f"private:{ref}{named}",
        topic_tags=[PRIVATE_ALERT_TAG],
    )


def test_evidence_shows_only_for_its_own_readers_and_month(alerts_db: Path) -> None:
    key = "SAL-ORD-2026-00007"
    assert _keep("credit_hold", key, TABLES, readers=[9, 4])
    # Sent again for other readers: the first readers' alert keeps its own.
    other = _with(_set((0, "heading"), "For someone else"))
    assert _keep("credit_hold", key, other, readers=[5])
    assert evidence.evidence_for(_seen("credit_hold", key, named=":named:4,9")) == TABLES
    assert evidence.evidence_for(_seen("credit_hold", key, named=":named:5")) == other
    # The other readers' next send, without evidence, clears only theirs.
    assert not _keep("credit_hold", key, None, readers=[5])
    assert evidence.evidence_for(_seen("credit_hold", key, named=":named:5")) is None
    assert evidence.evidence_for(_seen("credit_hold", key, named=":named:4,9")) == TABLES
    # Next month's send for the same readers reaches their alert, which the
    # pipeline may have kept open from this month; an unnamed alert, or a
    # malformed suffix, shows nothing.
    newer = _with(_set((0, "heading"), "Next month"))
    assert _keep("credit_hold", key, newer, readers=[4, 9])
    assert evidence.evidence_for(_seen("credit_hold", key, named=":named:4,9", month="2020-01")) == newer
    assert evidence.evidence_for(_seen("credit_hold", key)) is None
    assert evidence.evidence_for(_seen("credit_hold", key, named=":named:4,x")) is None


def test_old_evidence_is_not_shown(alerts_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    assert _keep(VECTOR["kind"], VECTOR["key"], TABLES)
    monkeypatch.setattr(evidence, "KEEP_FOR", timedelta(seconds=-1))
    assert evidence.evidence_for(_alert(VECTOR["kind"], VECTOR["key"])) is None


@pytest.mark.parametrize("row", [
    # Not private: a demo bundle's alert, or the model's own.
    {"topic_tags": []},
    # Another key than the event's.
    {"dedup_key": f"erpnext:{VECTOR['kind']}:{VECTOR['key']}"},
    {"dedup_key": f"private:erpnext:{VECTOR['kind']}:Another"},
    # Not the external id erpnext_events gives it, or another source.
    {"external_id": "msg-123"},
    {"source": "executive_erpnext"},
])
def test_only_the_events_own_alert_shows_its_evidence(alerts_db: Path, row: dict[str, Any]) -> None:
    assert _keep(VECTOR["kind"], VECTOR["key"], TABLES)
    assert evidence.evidence_for(_alert(VECTOR["kind"], VECTOR["key"], **row)) is None


def test_a_named_alert_shows_its_evidence(alerts_db: Path) -> None:
    key = "SAL-ORD-2026-00007"
    assert _keep("credit_hold", key, TABLES, readers=[4, 9])
    alert = _alert("credit_hold", key, dedup_key=f"private:erpnext:credit_hold:{key}:named:4,9")
    assert evidence.evidence_for(alert) == TABLES


def test_the_home_card_carries_the_alerts_evidence(alerts_db: Path) -> None:
    from openexecutive.api.routes import today

    assert _keep(VECTOR["kind"], VECTOR["key"], TABLES)
    shown = today._erp_evidence(_alert(VECTOR["kind"], VECTOR["key"]))
    assert shown is not None and [t.model_dump() for t in shown] == TABLES
    other = alert_store.get_alert(
        alert_store.insert_alert(severity="low", headline="h", body="", source="email", external_id="m-1")
    )
    assert today._erp_evidence(other) is None


def test_a_database_made_again_gets_its_table_back(alerts_db: Path) -> None:
    assert _keep(VECTOR["kind"], VECTOR["key"], TABLES)
    import sqlite3

    with sqlite3.connect(alerts_db) as conn:
        conn.execute("DROP TABLE erpnext_alert_evidence")
    # The table is made again on the next call.
    assert evidence.evidence_for(_seen(VECTOR["kind"], VECTOR["key"])) is None
    assert _keep(VECTOR["kind"], VECTOR["key"], TABLES)
    assert evidence.evidence_for(_seen(VECTOR["kind"], VECTOR["key"])) == TABLES


# The overnight_postings event as oe_connector's overnight.py builds it: what
# failed or looks wrong (a document cell may be empty), then what ran.
OVERNIGHT = {
    "kind": "overnight_postings",
    "key": "overnight:2026-11-23",
    "title": "Overnight: 1 failed, 1 to check",
    "text": "ERPNext's scheduled jobs ran overnight; one failed and one result looks wrong.",
    "evidence": [
        {
            "heading": "Failed or to check",
            "currency": None,
            "columns": [
                {"key": "what", "label": "What", "type": "text"},
                {"key": "document", "label": "Document", "type": "doc"},
                {"key": "company", "label": "Company", "type": "text"},
                {"key": "detail", "label": "Detail", "type": "text"},
            ],
            "rows": [
                {"what": "Depreciation failed", "document": {"doctype": "Asset", "name": "ACC-ASS-2026-00003"},
                 "company": "Aurora Components (Demo)", "detail": "Its depreciation account is disabled."},
                {"what": "A scheduled job failed", "document": None, "company": "Aurora Components (Demo)",
                 "detail": "Auto reorder"},
            ],
        },
        {
            "heading": "What ran",
            "currency": None,
            "columns": [{"key": "what", "label": "What", "type": "text"},
                        {"key": "count", "label": "How many", "type": "number"}],
            "rows": [{"what": "Depreciation entries", "count": 4}, {"what": "Subscription invoices", "count": 2}],
        },
    ],
}


def test_the_overnight_event_keeps_its_evidence_out_of_triage(alerts_db: Path, site: SimpleNamespace) -> None:
    assert _post(site, {"events": [OVERNIGHT]}).json() == {"accepted": 1}
    (event,) = site.scheduled
    assert event.private is True and event.dedup_hint == "erpnext:overnight_postings:overnight:2026-11-23"
    assert "ACC-ASS-2026-00003" not in event.body and "Depreciation entries" not in event.body
    # Its own text is dropped; the title of counts stands for it.
    assert "ran overnight" not in event.body and "Overnight: 1 failed, 1 to check" in event.body
    shown = evidence.evidence_for(_alert(OVERNIGHT["kind"], OVERNIGHT["key"]))
    assert shown == OVERNIGHT["evidence"]


# purchase_price_rise and valuation_jump as oe_connector's movements.py builds them.
PRICE_RISE = {
    "kind": "purchase_price_rise",
    "key": "ACC-PINV-2026-00041",
    "title": "ACC-PINV-2026-00041: DEMO-CBL-100 18% above its usual price",
    "text": "Purchase invoice ACC-PINV-2026-00041 from Bolt Supply prices 1 item(s) at least 10% above their average.",
    "evidence": [{
        "heading": "Above their usual price (the 90 days before)",
        "currency": "USD",
        "columns": [
            {"key": "item", "label": "Item", "type": "text"},
            {"key": "rate", "label": "Rate now", "type": "money"},
            {"key": "usual", "label": "Usual rate", "type": "money"},
            {"key": "rise", "label": "Above usual", "type": "percent"},
            {"key": "lines", "label": "Lines before", "type": "number"},
            {"key": "same_supplier", "label": "Same supplier", "type": "text"},
        ],
        "rows": [{"item": "DEMO-CBL-100", "rate": 41.3, "usual": 35.0, "rise": 18.0, "lines": 6,
                  "same_supplier": "no, last from Orion Switchgear"}],
    }],
}
VALUATION_JUMP = {
    "kind": "valuation_jump",
    "key": "valuation_jump:0123456789abcdef01234567",
    "title": "DEMO-PNL-12 valuation rose 35% in Stores - ACD",
    "text": "The valuation rate of DEMO-PNL-12 in Stores - ACD moved from $112.00 to $151.20 on 2026-11-22.",
    "evidence": [{
        "heading": "The entries that moved it",
        "currency": "USD",
        "columns": [
            {"key": "document", "label": "Document", "type": "doc"},
            {"key": "qty", "label": "Quantity", "type": "number"},
            {"key": "incoming_rate", "label": "Incoming rate", "type": "money"},
            {"key": "valuation_rate", "label": "Valuation after", "type": "money"},
        ],
        "rows": [{"document": {"doctype": "Purchase Receipt", "name": "MAT-PRE-2026-00019"}, "qty": 20.0,
                  "incoming_rate": 1120.0, "valuation_rate": 151.2}],
    }],
}


@pytest.mark.parametrize("event", [PRICE_RISE, VALUATION_JUMP])
def test_a_movement_events_evidence_is_kept_out_of_triage_and_shown(
    alerts_db: Path, site: SimpleNamespace, event: dict[str, Any],
) -> None:
    assert _post(site, {"events": [event]}).json() == {"accepted": 1}
    (scheduled,) = site.scheduled
    assert scheduled.private is True
    assert "Usual rate" not in scheduled.body and "Valuation after" not in scheduled.body
    assert evidence.evidence_for(_alert(event["kind"], event["key"])) == event["evidence"]


def test_a_price_rise_opens_its_invoice_and_a_valuation_jump_opens_nothing(alerts_db: Path) -> None:
    from openexecutive.integrations.erpnext_events import record_for

    rise = _alert(PRICE_RISE["kind"], PRICE_RISE["key"])
    assert record_for(rise) == {"doctype": "Purchase Invoice", "name": "ACC-PINV-2026-00041"}
    assert record_for(_alert(VALUATION_JUMP["kind"], VALUATION_JUMP["key"])) is None
