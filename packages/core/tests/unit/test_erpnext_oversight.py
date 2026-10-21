# ruff: noqa: F811 — pytest fixtures imported from test_erpnext_drafts share their names with the test parameters.
"""One oversight record per ERPNext draft (integrations/erpnext_oversight.py,
GET /erpnext/drafts/oversight): who asked, what the card showed, who decided
and what came of it, and, asked of ERPNext through oe_connector's service-only
audit_drafts, what became of each draft since."""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openexecutive.integrations import erpnext_drafts as drafts
from openexecutive.integrations import erpnext_oversight as oversight
from openexecutive.memory.decision_ledger import close_externally, get_decision_instance
from openexecutive.orchestrator import erpnext_caller, mcp_gateway

# The draft tests' fixtures: a temp DB, ERPNext faked behind a real gateway,
# the roster, signed approvals, the extension and the route client.
from .test_delegation_tools import OWNER, db, fresh_turn_state  # noqa: F401
from .test_erpnext_drafts import (  # noqa: F401
    CHECKED_RESTOCK,
    CREATED,
    RESTOCK,
    FakeErpnext,
    _approve,
    _owner_turn,
    _propose,
    _restock_card,
    client,
    erp,
    extension,
    roster,
    signed,
)

SEAL = "d" * 64


def _search_text() -> str:
    """extensible-mcp's ``search_tools`` answer naming the audit tool and one other."""
    names = ("erpnext__audit_drafts", "erpnext__run_report")
    return "".join(f"## {name}\n**Description:** {name}\n\n" for name in names)


def _made(erp: FakeErpnext, roster: SimpleNamespace) -> Any:
    erp.replies.append(json.dumps({**CHECKED_RESTOCK, "fingerprint": "a" * 64}))
    out = _propose(_owner_turn(roster), {"why": "Restock ITEM-1", "doctype": "Material Request", "fields": RESTOCK})
    card = get_decision_instance(out["decision_id"])
    assert card is not None
    erp.replies.append(json.dumps({**CREATED, "row_hash": SEAL, "chain_index": 7}))
    _approve(card)
    return card


def test_a_made_draft_is_one_record_with_who_what_and_its_seal(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    card = _made(erp, roster)
    (record,) = oversight.records(reader=roster.principal)
    assert (record["decision_id"], record["doctype"], record["outcome"]) == (card.id, "Material Request", "made")
    assert (record["asked_by"], record["asked_in"], record["why"]) == (OWNER, "chat", "Restock ITEM-1")
    assert record["fingerprint"] == "a" * 64
    assert record["card"]["fields"], "what the card showed is kept"
    assert record["erpnext_document"]["name"] == "MAT-MR-2026-00012"
    assert record["seal"] == {"row_hash": SEAL, "chain_index": 7}
    assert record["resolved_at"] and "Olivia Owner" in record["resolved_by"]
    assert record["private"] is False


def test_what_someone_elses_card_said_stays_theirs(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    # A card is its person's alone, the principal's included: a record of it
    # keeps who, when, what came of it and the seal, never what it showed.
    card = _made(erp, roster)
    (record,) = oversight.records(reader=roster.teammate)
    assert (record["decision_id"], record["outcome"], record["asked_by"]) == (card.id, "made", OWNER)
    assert record["seal"] == {"row_hash": SEAL, "chain_index": 7}
    assert record["private"] is True
    assert (record["why"], record["card"], record["fields"], record["conversation"]) == (None, {}, {}, None)
    assert record["fingerprint"] is None, "a digest of what the card said would let a guess be checked"
    (row,) = list(csv.DictReader(io.StringIO(oversight.to_csv([record]))))
    assert (row["why"], row["card"], row["private"]) == ("", "", "yes")


def test_a_dismissed_card_and_a_changed_one_say_so(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    first = _restock_card(erp, roster)
    close_externally(first.id, reason="expired")
    second = _restock_card(erp, roster)
    erp.replies.append(f"Tool error: {drafts.CHANGED_SINCE_PROPOSED}: ERPNext would now make it differently.")
    with pytest.raises(drafts.ApproveRefused):
        _approve(second)
    outcomes = {r["decision_id"]: r["outcome"] for r in oversight.records()}
    assert outcomes == {first.id: "expired", second.id: "not made: changed since proposed"}


def test_a_date_range_picks_the_cards_proposed_in_it(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    _restock_card(erp, roster)
    now = datetime.now(UTC)
    assert len(oversight.records(since=now - timedelta(hours=1))) == 1
    assert oversight.records(since=now + timedelta(hours=1)) == []
    assert oversight.records(until=now - timedelta(hours=1)) == []


def test_erpnext_is_asked_with_the_service_caller_inside_the_export_only(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    card = _made(erp, roster)
    erp.replies.append(json.dumps({"ok": True, "drafts": {card.idempotency_key: {
        "in_log": True, "doctype": "Material Request", "name": "MAT-MR-2026-00012",
        "state": "submitted", "modified": "2026-10-21 09:00:00", "modified_by": "olivia@co.example",
        "chain_index": 7, "row_hash": SEAL, "seal_intact": True,
    }}}))
    (record,) = asyncio.run(oversight.with_erpnext_state(oversight.records()))
    assert record["erpnext"]["state"] == "submitted" and record["seal_in_log"] is True
    tool, sent = erp.calls[-1]
    assert tool == "erpnext__audit_drafts" and sent["idempotency_keys"] == [card.idempotency_key]
    assert erp.claims()["kind"] == "service" and erp.claims()["tool"] == "audit_drafts"


def test_a_seal_the_log_no_longer_holds_is_flagged(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    # Another hash; the same hash on a row that no longer hashes to it; or an
    # intact-looking row now naming another document.
    card = _made(erp, roster)
    same = {"in_log": True, "doctype": "Material Request", "name": "MAT-MR-2026-00012", "state": "draft",
            "chain_index": 7, "row_hash": SEAL, "seal_intact": True}
    for changed in ({"row_hash": "e" * 64}, {"seal_intact": False}, {"name": "MAT-MR-2026-00001"}):
        erp.replies.append(json.dumps({"ok": True, "drafts": {card.idempotency_key: {**same, **changed}}}))
        (record,) = asyncio.run(oversight.with_erpnext_state(oversight.records()))
        assert record["seal_in_log"] is False, changed


def _told(state: str, unchanged: bool | None) -> dict[str, Any]:
    return {"erpnext": {"state": state, "unchanged": unchanged}}


def test_the_share_submitted_unchanged_counts_only_known_answers() -> None:
    # A row from before the digest, or a deleted document, has no answer and
    # stays out of the share; drafts not submitted aren't counted at all.
    rows = [
        _told("submitted", True), _told("submitted", True), _told("submitted", True),
        _told("submitted", False), _told("submitted", None),
        _told("draft", True), _told("cancelled", False), {"erpnext": None}, {},
    ]
    assert oversight.unchanged_share(rows) == {
        "submitted": 5, "unchanged": 3, "changed": 1, "unknown": 1, "share": 0.75,
    }


def test_a_range_with_no_submitted_drafts_has_no_share() -> None:
    nothing = {"submitted": 0, "unchanged": 0, "changed": 0, "unknown": 0, "share": None}
    assert oversight.unchanged_share([]) == nothing
    assert oversight.unchanged_share([_told("draft", True), {"erpnext": None}]) == nothing
    assert oversight.unchanged_share([_told("submitted", None)]) == {**nothing, "submitted": 1, "unknown": 1}


def test_unchanged_comes_from_erpnext_into_the_record_the_csv_and_the_summary(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], client: TestClient,
) -> None:
    card = _made(erp, roster)
    told = {"in_log": True, "doctype": "Material Request", "name": "MAT-MR-2026-00012", "state": "submitted",
            "chain_index": 7, "row_hash": SEAL, "seal_intact": True}
    column = oversight.CSV_COLUMNS.index("unchanged")
    for unchanged, kept, cell in ((True, True, "yes"), (False, False, "NO"), (None, None, ""), ("true", None, "")):
        erp.replies.append(json.dumps({"ok": True, "drafts": {card.idempotency_key: {**told, "unchanged": unchanged}}}))
        rows = asyncio.run(oversight.with_erpnext_state(oversight.records()))
        assert rows[0]["erpnext"]["unchanged"] is kept
        assert list(csv.reader(io.StringIO(oversight.to_csv(rows))))[1][column] == cell
    # The route's summary, only when ERPNext was asked.
    owner = {"x-caller-email": OWNER}
    assert "unchanged_share" not in client.get("/erpnext/drafts/oversight", headers=owner).json()
    erp.replies.append(json.dumps({"ok": True, "drafts": {card.idempotency_key: {**told, "unchanged": True}}}))
    got = client.get("/erpnext/drafts/oversight?erpnext=1", headers=owner).json()
    assert got["unchanged_share"] == {"submitted": 1, "unchanged": 1, "changed": 0, "unknown": 0, "share": 1.0}


def test_the_audit_tool_is_refused_outside_the_export_and_runs_nothing_else_inside(erp: FakeErpnext) -> None:
    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    outside = json.loads(asyncio.run(gateway.call_tool(
        {"name": "erpnext__audit_drafts", "arguments": {"idempotency_keys": ["oe:x:0123456789"]}}
    )))
    assert "oversight export" in outside["error"]

    async def inside() -> str:
        with erpnext_caller.oversight_run():
            return await gateway.call_tool({"name": "erpnext__get_record", "arguments": {"doctype": "ToDo", "name": "x"}})

    assert "oversight export" in json.loads(asyncio.run(inside()))["error"]
    assert erp.calls == []


def test_the_model_is_never_offered_the_audit_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit._erpnext_pins import pin_listing

    pin_listing(monkeypatch, _search_text())
    kept = mcp_gateway._drop_blocked_tools(_search_text())
    assert "erpnext__run_report" in kept and "erpnext__audit_drafts" not in kept


def test_the_hidden_tool_is_still_discovered_for_the_export(erp: FakeErpnext) -> None:
    # extensible-mcp runs only a tool a search returned; the search the model
    # sees hides this one, so discovery must read the search unfiltered.
    listed = _search_text()

    async def search(name: str, _args: dict[str, Any]) -> Any:
        assert name == "search_tools"
        return SimpleNamespace(content=[SimpleNamespace(text=listed)])

    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    gateway._session.call_tool.side_effect = search
    assert asyncio.run(gateway._discover("erpnext__audit_drafts")) is True
    assert "erpnext__audit_drafts" not in asyncio.run(gateway.search_tools({"query": "audit drafts"}))


def test_the_csv_is_safe_to_open_in_a_spreadsheet(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    _made(erp, roster)
    rows = oversight.records(reader=roster.principal)
    rows[0]["why"] = "=HYPERLINK(\"https://evil.example\")"
    table = list(csv.reader(io.StringIO(oversight.to_csv(rows))))
    assert table[0] == list(oversight.CSV_COLUMNS)
    why = table[1][oversight.CSV_COLUMNS.index("why")]
    assert why.startswith("'=")
    assert table[1][oversight.CSV_COLUMNS.index("row_hash")] == SEAL


def test_the_route_is_the_principals_alone(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], client: TestClient,
) -> None:
    _made(erp, roster)
    owner = {"x-caller-email": OWNER}
    got = client.get("/erpnext/drafts/oversight", headers=owner)
    assert got.status_code == 200 and len(got.json()["records"]) == 1
    as_csv = client.get("/erpnext/drafts/oversight?format=csv", headers=owner)
    assert as_csv.status_code == 200 and as_csv.headers["content-type"].startswith("text/csv")
    assert "attachment" in as_csv.headers["content-disposition"]
    assert client.get("/erpnext/drafts/oversight", headers={"x-caller-email": "ben@co.example"}).status_code == 403
    assert client.get("/erpnext/drafts/oversight?since=not-a-date", headers=owner).status_code == 422


def test_each_export_leaves_a_private_audit_row(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The record is accountability evidence, so taking it is evidenced too:
    # what was asked and how many came back, never the records.
    import openexecutive.audit as audit

    rows: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(audit, "log_event", lambda event, _summary, **kw: rows.append((event, kw)))
    _made(erp, roster)
    owner = {"x-caller-email": OWNER}
    assert client.get("/erpnext/drafts/oversight?format=csv&since=2026-10-01", headers=owner).status_code == 200
    ((event, kw),) = [row for row in rows if row[0] == "erpnext_oversight_exported"]
    assert kw["private"] is True and kw["actor"] == OWNER
    assert kw["details"] == {
        "since": "2026-10-01T00:00:00+00:00", "until": None, "format": "csv", "erpnext": False,
        "erpnext_answered": None, "records": 1,
    }
    rows.clear()
    assert client.get("/erpnext/drafts/oversight", headers={"x-caller-email": "ben@co.example"}).status_code == 403
    assert not [row for row in rows if row[0] == "erpnext_oversight_exported"]

    async def no_answer(_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise RuntimeError("ERPNext is down")

    monkeypatch.setattr(oversight, "with_erpnext_state", no_answer)
    assert client.get("/erpnext/drafts/oversight?erpnext=1", headers=owner).status_code == 502
    ((_, kw),) = [row for row in rows if row[0] == "erpnext_oversight_exported"]
    assert kw["details"]["erpnext"] is True and kw["details"]["erpnext_answered"] is False


# The approval gate's own numbers (``oversight.gate``).

_T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
ANN = "Ann <ann@co.example>"


def _decided(
    who: str | None, status: str, seconds: float | None = None, *, outcome: str = "",
    erp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A record as ``records`` makes it, with just what the gate reads."""
    return {
        "proposed_at": _T0.isoformat(), "status": status, "outcome": outcome, "resolved_by": who,
        "resolved_at": (_T0 + timedelta(seconds=seconds)).isoformat() if seconds is not None else None,
        "erpnext": erp,
    }


def _person(approved: int, dismissed: int, seconds: float, who: str = ANN) -> list[dict[str, Any]]:
    return [_decided(who, "approved_unchanged", seconds) for _ in range(approved)] + [
        _decided(who, "rejected", seconds) for _ in range(dismissed)
    ]


def test_the_gate_counts_rates_and_percentiles_on_a_known_set() -> None:
    ben = "Ben <ben@co.example>"
    rows = [
        _decided(ANN, "approved_unchanged", 10), _decided(ANN, "approved_with_edit", 20),
        _decided(ANN, "failed", 30), _decided(ANN, "approved_unchanged", 40), _decided(ANN, "rejected", 5),
        _decided(ben, "rejected", 50),
        _decided(None, "closed_externally", 3600, outcome="expired"),
        _decided(None, "closed_externally", 60, outcome="replaced: ERPNext's reading changed"),
        _decided(None, "proposed"), _decided(None, "executed", 1),
    ]
    got = oversight.gate(rows)
    # Approved: made, or approved and then not made (failed); not the one
    # nobody tapped (executed). Expired and replaced are counted apart.
    assert got["everyone"]["range"] == {
        "decided": 6, "approved": 4, "dismissed": 2, "approval_rate": 0.6667,
        "median_seconds": 25.0, "p10_seconds": 13.0, "expired": 1, "replaced": 1,
    }
    assert got["everyone"]["before"] is None, "a range with no start has nothing before it"
    by = {p["resolved_by"]: p["range"] for p in got["people"]}
    assert by[ANN] == {
        "decided": 5, "approved": 4, "dismissed": 1, "approval_rate": 0.8,
        "median_seconds": 25.0, "p10_seconds": 13.0,
    }
    assert by[ben] == {
        "decided": 1, "approved": 0, "dismissed": 1, "approval_rate": 0.0,
        "median_seconds": None, "p10_seconds": None,
    }
    assert got["flags"] == [] and "not how long the card was read" in got["note"]


def test_a_range_with_nothing_decided_has_nulls_not_a_division_by_zero() -> None:
    empty = {"decided": 0, "approved": 0, "dismissed": 0, "approval_rate": None, "median_seconds": None,
             "p10_seconds": None}
    got = oversight.gate([], [])
    assert got["everyone"] == {"range": {**empty, "expired": 0, "replaced": 0}, "before": empty}
    assert (got["people"], got["flags"]) == ([], [])
    waiting = oversight.gate([_decided(None, "proposed"), _decided(None, "closed_externally", 9, outcome="expired")])
    assert waiting["everyone"]["range"] == {**empty, "expired": 1, "replaced": 0}
    # Someone who decided nothing in the range before has nulls there.
    (ann,) = oversight.gate(_person(10, 0, 30), [])["people"]
    assert ann["before"] == empty


@pytest.mark.parametrize(("now", "before", "fires"), [
    (_person(10, 0, 30), _person(8, 2, 60), True),       # 100% from 80%, median halved, 10 decided
    (_person(9, 0, 30), _person(8, 2, 60), False),       # 9 decided
    (_person(19, 1, 30), _person(8, 2, 60), True),       # 95% exactly
    (_person(18, 2, 30), _person(8, 2, 60), False),      # 90%
    (_person(10, 0, 31), _person(8, 2, 60), False),      # the median fell by less than half
    (_person(10, 0, 30), _person(10, 0, 60), False),     # 100% then too: not higher
    (_person(10, 0, 30), [], False),                     # nothing decided before to compare with
    (_person(10, 0, 30), None, False),                   # no range before
])
def test_the_approving_more_faster_flag_at_each_threshold(
    now: list[dict[str, Any]], before: list[dict[str, Any]] | None, fires: bool,
) -> None:
    flags = oversight.gate(now, before)["flags"]
    assert [f["flag"] for f in flags] == (["approving_more_faster"] if fires else [])
    if fires:
        assert flags[0]["resolved_by"] == ANN and "median 30s (before: 60s)" in flags[0]["why"]


def _submitted(n: int, seconds: float, unchanged: bool | None = True, state: str = "submitted") -> list[dict[str, Any]]:
    return [
        _decided(ANN, "approved_unchanged", seconds, erp={"state": state, "unchanged": unchanged}) for _ in range(n)
    ]


@pytest.mark.parametrize(("rows", "erpnext", "fires"), [
    (_submitted(10, 29), True, True),
    (_submitted(10, 29), False, False),                               # ERPNext wasn't asked
    (_submitted(9, 29), True, False),                                 # 9 submitted
    (_submitted(10, 29) + _submitted(1, 29, False), True, False),     # one changed
    (_submitted(10, 29) + _submitted(1, 29, None), True, True),       # an unknown answer doesn't count
    (_submitted(10, 30), True, False),                                # median not under 30s
    (_submitted(9, 29) + _submitted(1, 29, state="draft"), True, False),  # a draft isn't submitted
])
def test_the_unchanged_and_fast_flag_at_each_threshold(rows: list[dict[str, Any]], erpnext: bool, fires: bool) -> None:
    flags = oversight.gate(rows, erpnext=erpnext)["flags"]
    assert [f["flag"] for f in flags] == (["unchanged_and_fast"] if fires else [])


def test_someone_elses_card_counts_in_the_gate_without_its_content(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any],
) -> None:
    _made(erp, roster)
    (record,) = oversight.records(reader=roster.teammate)
    assert record["private"] is True
    got = oversight.gate([record])
    assert got["everyone"]["range"]["approved"] == 1
    ((person, numbers),) = [(p["resolved_by"], p["range"]) for p in got["people"]]
    assert "Olivia Owner" in person and numbers["median_seconds"] is not None
    assert "Restock" not in json.dumps(got), "the summary carries no card content"


def test_the_route_returns_the_gate_with_the_range_before_and_keeps_the_csv(
    erp: FakeErpnext, roster: SimpleNamespace, signed: dict[str, Any], client: TestClient,
) -> None:
    _made(erp, roster)
    owner = {"x-caller-email": OWNER}
    now = datetime.now(UTC)
    since, until = now - timedelta(days=1), now + timedelta(days=1)
    asked = {"since": since.isoformat(), "until": until.isoformat()}
    gate = client.get("/erpnext/drafts/oversight", params=asked, headers=owner).json()["gate"]
    assert gate["everyone"]["range"]["approved"] == 1 and gate["everyone"]["before"]["decided"] == 0
    assert gate["range"] == asked
    assert gate["before_range"] == {"since": (since - timedelta(days=2)).isoformat(), "until": since.isoformat()}
    assert gate["thresholds"]["min_decided"] == oversight.GATE_MIN_DECIDED
    unbounded = client.get("/erpnext/drafts/oversight", headers=owner).json()["gate"]
    assert unbounded["before_range"] is None and unbounded["everyone"]["before"] is None
    long_ago = {"since": "2020-01-01", "until": "2020-02-01"}
    empty = client.get("/erpnext/drafts/oversight", params=long_ago, headers=owner).json()["gate"]
    assert empty["everyone"]["range"]["approval_rate"] is None and empty["people"] == []
    # A range that runs backwards, or whose range before it would fall
    # before year 1, is refused before anything is audited or read.
    for bad in (
        {"since": (now + timedelta(days=2)).isoformat()}, {"since": "2026-10-20", "until": "2026-10-01"},
        {"since": "1000-01-01"}, {"since": "0001-01-01", "until": "9999-12-31"},
    ):
        assert client.get("/erpnext/drafts/oversight", params=bad, headers=owner).status_code == 422, bad
    # The CSV stays one row per record with the same columns: no summary.
    as_csv = client.get("/erpnext/drafts/oversight?format=csv", headers=owner).text
    assert next(csv.reader(io.StringIO(as_csv))) == list(oversight.CSV_COLUMNS) and len(oversight.CSV_COLUMNS) == 23
    assert "approval_rate" not in as_csv
