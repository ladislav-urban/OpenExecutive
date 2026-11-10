# ruff: noqa: F811 — pytest fixtures imported from other test modules share their names with the test parameters.
"""Phase 1's payment-checks job (PR 5b): oe_connector's list slots mirrored in
the shape checker (watch/registry.py, against oe_connector's own vectors), a
job's slots (watch/settings.py, watch/runner.py), the duplicate and bank rules
and the cards they leave (watch/jobs/payment_checks.py), the hold card for a
duplicate draft, the block card for a duplicate submitted invoice
(integrations/erpnext_block.py, integrations/erpnext_drafts.py), and the one
job supplier_bank_changed opens to (orchestrator/mcp_gateway.py)."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.integrations import erpnext_block, erpnext_changes
from openexecutive.integrations import erpnext_drafts as drafts
from openexecutive.memory.decision_ledger import (
    STATUS_APPROVED_UNCHANGED,
    STATUS_FAILED,
    STATUS_PROPOSED,
    get_decision_instance,
    list_instances,
)
from openexecutive.orchestrator import erpnext_caller, mcp_gateway
from openexecutive.orchestrator.erpnext_caller import ASSERTION_ARG, watch_run
from openexecutive.orchestrator.erpnext_tool_pin import TOOL_DIGESTS
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.people import store as people_store
from openexecutive.prompts.connected_systems import (
    ERPNEXT_DRAFT_TOOLS,
    ERPNEXT_WITHHELD_TOOLS,
    JOB_WITHHELD_EXCEPTIONS,
)
from openexecutive.watch import registry, runner
from openexecutive.watch import settings as watch_settings
from openexecutive.watch.jobs import _example, collections, payment_checks
from openexecutive.watch.jobs.payment_checks import Invoice
from openexecutive.watch.registry import CardSpec, DraftSpec, WatchJob, WatchRead

from .test_delegation_tools import OWNER, TEAM, db, fresh_turn_state  # noqa: F401
from .test_erpnext_drafts import FakeErpnext, _owner_turn, erp, roster  # noqa: F401
from .test_watch_collections import (  # noqa: F401
    REVIEWER,
    approvals,
    fresh_checks,
    signing,
    team,
)
from .test_watch_jobs import (  # noqa: F401
    DAY,
    TODAY,
    VECTORS,
    _alerts,
    _audit_rows,
    alerts_db,
    model,
    triage,
)

SHAPES = json.loads((Path(__file__).parent / "watch_shape_vectors.json").read_text(encoding="utf-8"))
JOB = payment_checks.JOB
PI = "Purchase Invoice"
ACTING = {"kind": "watch", "job": "payment-checks", "as_job_user": True, "user": "oe-watch-payment-checks@erp.example"}
SETTINGS = {"company": "Acme", "due_within_days": 14, "block_submitted": False}
FINGERPRINT = "e" * 64
RELEASE = (TODAY + timedelta(days=14)).isoformat()


def _pi(name: str, supplier: str, bill_no: str, bill_date: str, total: float, *, docstatus: int = 1,
        outstanding: float | None = None, posted: str | None = None, due: str = "2026-11-20",
        on_hold: int = 0, currency: str = "EUR") -> dict[str, Any]:
    return {
        "name": name, "supplier": supplier, "bill_no": bill_no, "bill_date": bill_date,
        "posting_date": posted or bill_date, "grand_total": total, "currency": currency,
        "outstanding_amount": total if outstanding is None else outstanding, "docstatus": docstatus,
        "on_hold": on_hold, "due_date": due,
    }


# Orion: a draft keyed "inv 42" against a paid, submitted "INV-0042" (high).
# Vanta: a second submitted invoice, same amount and bill date as the first
# (medium); its bank details changed after its last payment. Low Co: the same
# amount two days apart (low, never proposed).
SUP_1 = _pi("SUP-1", "Orion Switchgear", "INV-0042", "2026-10-01", 1200.0, outstanding=0.0, posted="2026-10-02")
PI_D1 = _pi("PI-D1", "Orion Switchgear", "inv 42", "2026-10-01", 1200.0, docstatus=0, due="2026-11-15")
PI_V1 = _pi("PI-V1", "Vanta Cable Mills", "VC-778", "2026-10-20", 500.0, posted="2026-10-21", due="2026-11-20")
PI_V2 = _pi("PI-V2", "Vanta Cable Mills", "VC-779", "2026-10-20", 500.0, posted="2026-10-25", due="2026-11-22")
PI_L1 = _pi("PI-L1", "Low Co", "A1", "2026-10-01", 300.0, outstanding=0.0)
PI_L2 = _pi("PI-L2", "Low Co", "B2", "2026-10-03", 300.0, docstatus=0, due="2026-11-18")
DUE = [PI_D1, PI_V1, PI_V2, PI_L2]
HISTORY = [SUP_1, PI_V1, PI_V2, PI_L1]
BANK = [
    {"supplier": "Vanta Cable Mills", "has_bank_account": True, "bank_details_changed_after_last_payment": True,
     "bank_details_changed_on": "2026-11-02", "last_paid_on": "2026-10-15"},
    {"supplier": "Orion Switchgear", "has_bank_account": True, "bank_details_changed_after_last_payment": False,
     "bank_details_changed_on": "2025-01-02", "last_paid_on": "2026-10-15"},
]
HOLD = {"set": {"on_hold": 1, "hold_comment": "Possible duplicate of SUP-1: same bill number"}}


def _list(rows: list[dict[str, Any]], acting: Any = ACTING, **extra: Any) -> str:
    out: dict[str, Any] = {"doctype": PI, "rows": rows, "truncated": False, **extra}
    if acting is not None:
        out["acting"] = acting
    return json.dumps(out)


def _bank(rows: list[dict[str, Any]] = BANK, acting: Any = ACTING) -> str:
    out: dict[str, Any] = {"suppliers": rows, "notes": ["Only whether and when."]}
    if acting is not None:
        out["acting"] = acting
    return json.dumps(out)


def _reads(erp: FakeErpnext, **kw: Any) -> None:
    erp.replies.extend([kw.get("due", _list(DUE)), kw.get("history", _list(HISTORY)), kw.get("bank", _bank())])


def _hold_checked(name: str = "PI-D1", whose: str = "yours", comment: str = HOLD["set"]["hold_comment"]) -> str:
    return json.dumps({
        "ok": True, "operation": "update", "doctype": PI, "name": name, "fingerprint": FINGERPRINT,
        "summary": {
            "operation": "update", "doctype": PI, "name": name, "whose": whose, "modified": "2026-11-09 10:00",
            "changes": [{"field": "on_hold", "before": 0, "after": 1},
                        {"field": "hold_comment", "before": None, "after": comment}],
            "lines": [], "removed": [],
        },
    })


def _block_checked(name: str = "PI-V2", other: str = "PI-V1", release: str = RELEASE, **extra: Any) -> str:
    summary = {
        "operation": "block", "doctype": PI, "name": name, "modified": "2026-11-09 10:00",
        "supplier": "Vanta Cable Mills", "company": "Acme", "currency": "EUR", "bill_no": "VC-779",
        "bill_date": "2026-10-20", "due_date": "2026-11-22", "grand_total": 500.0, "outstanding_amount": 500.0,
        "paid_amount": 0.0,
        "duplicate_of": {"name": other, "bill_no": "VC-778", "bill_date": "2026-10-20", "grand_total": 500.0,
                         "outstanding_amount": 500.0, "modified": "2026-10-21 09:00", "docstatus": 1},
        "reason": "the same amount and supplier invoice date", "release_date": release,
        "hold_comment": f"Possible duplicate of {other} (the same amount and supplier invoice date). Held through "
                        "Dynocortex; held until someone unblocks it.",
        "payments_under_way": {"draft_payment_entries": [], "open_payment_requests": [], "payment_orders": ["PO-1"]},
        **extra,
    }
    return json.dumps({"ok": True, "operation": "block", "doctype": PI, "name": name, "summary": summary,
                       "fingerprint": FINGERPRINT})


def _save(by: int, **kw: Any) -> None:
    watch_settings.save(
        JOB, enabled=kw.get("enabled", True), settings=kw.get("settings", dict(SETTINGS)),
        reviewer_person_id=kw.get("reviewer"), set_reviewer="reviewer" in kw,
        configured_by_person_id=by, now=datetime.now(UTC),
    )


@pytest.fixture
def job_on(erp: FakeErpnext, team: SimpleNamespace, signing: None, monkeypatch: pytest.MonkeyPatch) -> WatchJob:
    """Payment checks on, reviewed by Cara, with Purchase Invoice updates listed."""
    monkeypatch.setenv("ERPNEXT_WATCH_JOBS_ENABLED", "true")
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    monkeypatch.setenv("ERPNEXT_DRAFT_UPDATE_DOCTYPES", "Purchase Invoice")
    monkeypatch.delenv("ERPNEXT_BLOCK_INVOICE", raising=False)
    monkeypatch.setattr(erpnext_block, "_company_today", lambda: TODAY)
    _save(team.principal, reviewer=team.cara)
    return JOB


@pytest.fixture
def pin(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Whether the gateway serves draft_block_invoice as pinned."""
    state = SimpleNamespace(on=True)

    async def pinned() -> bool:
        return bool(state.on)

    monkeypatch.setattr(erpnext_block, "pinned", pinned)
    return state


@pytest.fixture
def blocking(job_on: WatchJob, team: SimpleNamespace, pin: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> WatchJob:
    """All three switches on: the job's block_submitted, ERPNEXT_BLOCK_INVOICE and the pin."""
    monkeypatch.setenv("ERPNEXT_BLOCK_INVOICE", "true")
    _save(team.principal, settings={**SETTINGS, "block_submitted": True})
    return job_on


def _run(job: WatchJob = JOB, day: str = DAY, slot: str = "") -> runner.RunOutcome:
    slots = (slot,) if slot else ("",)
    assert runner.claim(job.id, day, datetime.now(UTC), slots=slots) == "run"
    return asyncio.run(runner.run_job(job, day, slot=slot))


def _job_cards() -> list[Any]:
    return [c for c in list_instances(drafts.DECISION_CLASS, limit=1000) if drafts._payload(c).get("job") == JOB.id]


def _approve(card: Any) -> dict[str, Any]:
    fresh = get_decision_instance(card.id)
    assert fresh is not None
    return asyncio.run(drafts.approve(fresh, caller=None, resolver=fresh.approver_person_id))


def _refusal(card: Any) -> drafts.ApproveRefused:
    with pytest.raises(drafts.ApproveRefused) as refused:
        _approve(card)
    return refused.value


def _plain(sent: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in sent.items() if k != ASSERTION_ARG}


def _oracle(slot: str, value: Any) -> bool:
    return any(type(value) is type(held) and value == held for held in SHAPES["slot_oracle"].get(slot, []))


# --------------------------------------------------------------------------- #
# List slots (ladislav-urban/oe_connector#536), against oe_connector's own vectors
# --------------------------------------------------------------------------- #


def test_the_slot_oracle_names_exactly_our_slots() -> None:
    assert set(SHAPES["slot_oracle"]) == set(registry.SLOTS)
    assert set(registry.LIST_SLOTS) == {"$parties", "$customers"}


@pytest.mark.parametrize("case", SHAPES["shapes"], ids=lambda c: c["note"])
def test_a_reads_soundness_matches_oe_connectors(case: dict[str, Any]) -> None:
    why = registry.shape_problem(case["read"])
    assert (why == "") is case["valid"], why


@pytest.mark.parametrize("case", SHAPES["matches"], ids=lambda c: c["note"])
def test_arguments_fit_a_read_as_oe_connector_says(case: dict[str, Any]) -> None:
    assert registry.shape_problem(case["read"]) == "", "a match case needs a sound read"
    why = registry.shape_mismatch(case["read"], case["arguments"], TODAY, holds=_oracle)
    assert (why == "") is case["ok"], why


def test_an_unsound_read_is_refused_when_its_job_is_defined() -> None:
    for shape in (
        {"filters": {"supplier": "$parties"}},
        {"filters": {"supplier": ["like", "$party"]}},
        {"filters": {"status": ["in", ["A", "B"]]}},
        {"filters": {"company": "$Company"}},
    ):
        with pytest.raises(ValueError, match="unsound"):
            WatchJob(id="example", title="t", reads=(WatchRead("list_records", shape, _example._receivables, "r"),),
                     build=_example.build, schedule=time(7), card_cap_day=1, default_settings={})


def test_both_payment_checks_filters_are_in_oe_connectors_vectors() -> None:
    sound = [case["read"] for case in SHAPES["shapes"] if case["valid"]]
    assert payment_checks.DUE_SHAPE["filters"] in [r.get("filters") for r in sound]
    assert payment_checks.HISTORY_SHAPE["filters"] in [r.get("filters") for r in sound]
    for read in JOB.reads:
        assert registry.shape_problem(read.shape) == ""


def test_a_literal_is_its_json_type_and_value_in_the_runners_check() -> None:
    args = payment_checks._due(SETTINGS, TODAY)
    assert isinstance(args, dict)
    assert registry.shape_mismatch(payment_checks.DUE_SHAPE, args, TODAY) == ""
    for bad in (["<", 2.0], ["<", True], ["<=", 2], ["<", 2, 3], "2"):
        broken = {**args, "filters": {**args["filters"], "docstatus": bad}}
        assert registry.shape_mismatch(payment_checks.DUE_SHAPE, broken, TODAY), bad
    far = {**args, "filters": {**args["filters"], "due_date": ["<=", "2029-01-01"]}}
    assert registry.shape_mismatch(payment_checks.DUE_SHAPE, far, TODAY)
    # Only the top-level fields is a subset; a nested key named fields isn't.
    assert registry.shape_mismatch({"filters": {"fields": ["=", "x"]}}, {"filters": {"fields": ["x"]}}, TODAY)


# --------------------------------------------------------------------------- #
# The job in the registry
# --------------------------------------------------------------------------- #


def test_payment_checks_is_listed_hourly_on_weekdays_with_its_shared_reads() -> None:
    assert registry.get("payment-checks") is JOB
    listed = VECTORS["payment_checks_job"]
    assert listed["id"] == JOB.id and listed["as_job_user"] is True
    assert [{"tool": r.tool, **r.shape} for r in JOB.reads] == listed["reads"]
    assert JOB.slots == tuple(time(h) for h in range(8, 19)) and JOB.schedule == time(8)
    assert JOB.weekdays == frozenset(range(5)) and JOB.card_cap_day == 10
    assert JOB.default_settings == {"company": "", "due_within_days": 14, "block_submitted": False}
    assert registry.check_settings(JOB, {**SETTINGS, "due_within_days": 400})["due_within_days"] == 400
    for bad in ({**SETTINGS, "due_within_days": 0}, {**SETTINGS, "due_within_days": 401},
                {**SETTINGS, "block_submitted": 1}, {**SETTINGS, "block_submitted": "yes"}):
        with pytest.raises(registry.SettingsError):
            registry.check_settings(JOB, bad)


def test_the_bank_tool_opens_to_payment_checks_alone() -> None:
    assert dict(JOB_WITHHELD_EXCEPTIONS) == {
        "collections": frozenset({"erpnext__customer_payment_record"}),
        "payment-checks": frozenset({"erpnext__supplier_bank_changed"}),
    }
    assert "erpnext__supplier_bank_changed" in ERPNEXT_WITHHELD_TOOLS
    bank = WatchRead("supplier_bank_changed", payment_checks.BANK_SHAPE, payment_checks._bank, "b")
    for other in ("collections", "example", "payment-check"):
        with pytest.raises(ValueError):
            WatchJob(id=other, title="t", reads=(bank,), build=payment_checks.build, schedule=time(8),
                     card_cap_day=1, default_settings={})
    record = WatchRead("customer_payment_record", collections.RECORD_SHAPE, collections._record, "r")
    with pytest.raises(ValueError):
        WatchJob(id="payment-checks", title="t", reads=(record,), build=payment_checks.build, schedule=time(8),
                 card_cap_day=1, default_settings={})


def test_slots_are_distinct_times_in_order_from_the_schedule() -> None:
    base = dict(id="example", title="t", reads=_example.JOB.reads, build=_example.build, card_cap_day=1,
                default_settings={"company": "", "limit": 200})
    for slots, schedule in (((time(9), time(8)), time(9)), ((time(8), time(8)), time(8)), ((time(9),), time(8))):
        with pytest.raises(ValueError):
            WatchJob(schedule=schedule, slots=slots, **base)
    with pytest.raises(ValueError):
        WatchJob(schedule=time(8), weekdays=frozenset({7}), **base)


def test_the_due_slots_by_day_and_time() -> None:
    assert JOB.due_slots(time(7, 59), 1) == ()
    assert JOB.due_slots(time(8, 0), 1) == ("08:00",)
    assert JOB.due_slots(time(10, 30), 1) == ("08:00", "09:00", "10:00")
    assert JOB.due_slots(time(23, 0), 4)[-1] == "18:00" and len(JOB.due_slots(time(23, 0), 4)) == 11
    assert JOB.due_slots(time(12, 0), 5) == () and JOB.due_slots(time(12, 0), 6) == ()
    # A once-a-day job: its schedule, every day, with the slot "".
    assert collections.JOB.due_slots(time(7, 0), 6) == ("",) and collections.JOB.due_slots(time(6, 59), 2) == ()


# --------------------------------------------------------------------------- #
# The rules, on fixed data
# --------------------------------------------------------------------------- #


def _inv(row: dict[str, Any]) -> Invoice:
    [inv] = payment_checks.invoices({"rows": [row]}, required=True)
    return inv


@pytest.mark.parametrize(("a", "b", "same"), [
    ("INV-0042", "inv 42", True), ("INV 0042", "inv-42", True), ("A-001/7", "a 1 7", True),
    ("INV-0042", "INV-0043", False), ("42A", "42 a", True), ("", "", True),
])
def test_bill_numbers_are_normalized(a: str, b: str, same: bool) -> None:
    assert (payment_checks.normalized_bill(a) == payment_checks.normalized_bill(b)) is same
    assert payment_checks.normalized_bill("INV-0042") == "inv 42"


def test_confidence_high_medium_low_or_none() -> None:
    base = _inv(SUP_1)
    assert payment_checks.match(_inv(PI_D1), base) == "high"
    # The same number, another amount, dates within 60 days: still high.
    assert payment_checks.match(_inv({**PI_D1, "grand_total": 999.0, "bill_date": "2026-11-20"}), base) == "high"
    # The same number but 61 days apart and another amount: not a number match.
    assert payment_checks.match(_inv({**PI_D1, "grand_total": 999.0, "bill_date": "2026-12-01"}), base) is None
    # A number that isn't a real one (fewer than 2 digits) never matches by number.
    assert payment_checks.match(_inv({**PI_D1, "bill_no": "A1", "grand_total": 9.0}),
                                _inv({**SUP_1, "bill_no": "a-1"})) is None
    assert payment_checks.match(_inv(PI_V2), _inv(PI_V1)) == "medium"
    assert payment_checks.match(_inv(PI_L2), _inv(PI_L1)) == "low"
    assert payment_checks.match(_inv({**PI_L2, "bill_date": "2026-10-05"}), _inv(PI_L1)) is None
    # Another currency, or a missing bill date, never matches.
    assert payment_checks.match(_inv({**PI_V2, "currency": "USD"}), _inv(PI_V1)) is None
    assert payment_checks.match(_inv({**PI_V2, "bill_date": None}), _inv(PI_V1)) is None


def test_which_is_the_copy() -> None:
    due = payment_checks.invoices({"rows": DUE}, required=True)
    history = payment_checks.invoices({"rows": HISTORY}, required=True)
    found = payment_checks.duplicates(due, history)
    assert [(d.copy.name, d.original.name, d.confidence) for d in found] == [
        ("PI-D1", "SUP-1", "high"), ("PI-V2", "PI-V1", "medium"), ("PI-L2", "PI-L1", "low"),
    ]
    # A draft against a submitted invoice is the copy; the earlier submitted
    # one never is; one already on hold, paid or not submitted-against isn't raised.
    held = payment_checks.invoices({"rows": [{**PI_D1, "on_hold": 1}, PI_V1]}, required=True)
    assert payment_checks.duplicates(held, history) == []
    two_drafts = payment_checks.invoices({"rows": [PI_D1, {**PI_D1, "name": "PI-D2"}]}, required=True)
    assert payment_checks.duplicates(two_drafts, []) == []


def test_bank_changes_after_the_last_payment_or_new_and_never_paid() -> None:
    rows = [
        *BANK,
        {"supplier": "New Co", "has_bank_account": True, "bank_details_changed_after_last_payment": True,
         "bank_details_changed_on": (TODAY - timedelta(days=5)).isoformat(), "last_paid_on": None},
        {"supplier": "Old Never Paid", "has_bank_account": True, "bank_details_changed_after_last_payment": True,
         "bank_details_changed_on": "2025-01-01", "last_paid_on": None},
        {"supplier": "Unknown", "has_bank_account": True, "bank_details_changed_after_last_payment": None,
         "bank_details_changed_on": "2026-11-01", "last_paid_on": None},
    ]
    found = payment_checks.bank_changes({"suppliers": rows}, TODAY)
    assert [(c.supplier, c.last_paid_on) for c in found] == [("Vanta Cable Mills", "2026-10-15"), ("New Co", "")]
    assert payment_checks.bank_changes({}, TODAY) == []


def test_build_leaves_a_hold_a_notice_for_the_submitted_one_a_weak_match_and_the_bank_line() -> None:
    specs = payment_checks.build(
        (json.loads(_list(DUE)), json.loads(_list(HISTORY)), json.loads(_bank())), SETTINGS, TODAY,
    )
    holds = [s for s in specs if isinstance(s, DraftSpec)]
    notices = [s for s in specs if isinstance(s, CardSpec)]
    [hold] = holds
    assert (hold.operation, hold.doctype, hold.name, dict(hold.fields)) == ("update", PI, "PI-D1", HOLD)
    assert hold.notice is not None and payment_checks.HOLD_LINE in hold.notice.notes
    assert [n.summary for n in notices] == [
        "Possible duplicate submitted invoice: PI-V2", "Possible duplicate draft invoice: PI-L2",
        "Bank details changed: Vanta Cable Mills",
    ]
    assert notices[0].notes == (payment_checks.BLOCK_LINE,)
    assert notices[1].notes == (payment_checks.LOW_LINE,)
    # The bank notice's line is code's, word for word.
    assert notices[2].notes == (
        "Confirm the change with the supplier by phone, on a number you already have, before paying.",
    )
    assert ("Invoices due", "PI-V1, PI-V2") in notices[2].values


def test_build_proposes_a_block_only_with_the_setting() -> None:
    specs = payment_checks.build(
        (json.loads(_list(DUE)), json.loads(_list(HISTORY)), {}), {**SETTINGS, "block_submitted": True}, TODAY,
    )
    blocks = [s for s in specs if isinstance(s, DraftSpec) and s.operation == "block"]
    [block] = blocks
    assert (block.name, dict(block.fields)) == ("PI-V2", {"duplicate_of": "PI-V1", "release_date": RELEASE})
    assert dict(block.expect) == {"supplier": "Vanta Cable Mills", "company": "Acme"}
    assert block.notice is not None and block.notice.notes == (payment_checks.BLOCK_LINE,)


def test_the_hold_comment_is_codes_template_cleaned_and_capped() -> None:
    assert payment_checks.hold_comment("SUP-1", "high") == "Possible duplicate of SUP-1: same bill number"
    assert payment_checks.hold_comment("SUP-1", "medium") == "Possible duplicate of SUP-1: same amount and date"
    dirty = payment_checks.hold_comment("SUP\n-1\x07" + "x" * 400, "high")
    assert "\n" not in dirty and "\x07" not in dirty and len(dirty) <= 280


def test_check_fields_takes_only_the_jobs_own_hold_or_block() -> None:
    assert payment_checks.check_fields("update", PI, HOLD, "PI-D1") == HOLD
    for fields, name in (
        ({"set": {**HOLD["set"], "on_hold": True}}, "PI-D1"),
        ({"set": {**HOLD["set"], "hold_comment": "Pay this now"}}, "PI-D1"),
        ({"set": {**HOLD["set"], "remarks": "x"}}, "PI-D1"),
        ({**HOLD, "lines": {}}, "PI-D1"),
        ({"set": {"on_hold": 1, "hold_comment": "Possible duplicate of PI-D1: same bill number"}}, "PI-D1"),
        (HOLD, ""),
    ):
        assert isinstance(payment_checks.check_fields("update", PI, fields, name), str), fields
    assert isinstance(payment_checks.check_fields("create", PI, HOLD, ""), str)
    assert isinstance(payment_checks.check_fields("update", "Sales Invoice", HOLD, "PI-D1"), str)
    block = {"duplicate_of": "PI-V1", "release_date": RELEASE}
    assert erpnext_block.block_fields(block, "PI-V2", today=TODAY) == block
    for fields, name in (
        ({"duplicate_of": "PI-V2", "release_date": RELEASE}, "PI-V2"),
        ({"duplicate_of": "PI-V1", "release_date": DAY}, "PI-V2"),
        ({"duplicate_of": "PI-V1", "release_date": (TODAY + timedelta(days=91)).isoformat()}, "PI-V2"),
        ({"duplicate_of": "PI-V1"}, "PI-V2"),
        ({**block, "hold_comment": "x"}, "PI-V2"),
    ):
        assert isinstance(erpnext_block.block_fields(fields, name, today=TODAY), str), fields


# --------------------------------------------------------------------------- #
# Slots: the claim, the day's cap and the failure notice
# --------------------------------------------------------------------------- #


def _rows(job: str = "payment-checks", day: str = DAY) -> dict[str, tuple[str, int, str]]:
    conn = watch_settings.connect()
    try:
        return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT slot, status, attempts, error FROM watch_runs WHERE job = ? AND day = ?", (job, day))}
    finally:
        conn.close()


def test_two_slots_a_day_each_run_once() -> None:
    now = datetime(2026, 11, 10, 8, 5, tzinfo=UTC)
    assert runner.claim("payment-checks", DAY, now, slots=("08:00",)) == "run"
    assert runner.claim("payment-checks", DAY, now, slots=("08:00",)) is None
    runner._finish("payment-checks", DAY, runner.RunOutcome("done"), [], path=None, slot="08:00")
    later = now + timedelta(hours=1)
    assert runner.claim("payment-checks", DAY, later, slots=("08:00", "09:00")) == "run"
    assert runner.claim("payment-checks", DAY, later, slots=("08:00", "09:00")) is None
    assert {s: v[0] for s, v in _rows().items()} == {"08:00": "done", "09:00": "running"}


def test_only_the_latest_due_slot_is_claimed_and_the_earlier_ones_skipped() -> None:
    now = datetime(2026, 11, 10, 11, 5, tzinfo=UTC)
    assert runner.claim("payment-checks", DAY, now, slots=("08:00", "09:00", "10:00", "11:00")) == "run"
    rows = _rows()
    assert {s: v[0] for s, v in rows.items()} == {
        "08:00": "skipped", "09:00": "skipped", "10:00": "skipped", "11:00": "running",
    }
    assert rows["08:00"][1] == 0


def test_a_stale_slot_is_retried_once_while_it_is_still_the_latest() -> None:
    now = datetime(2026, 11, 10, 8, 5, tzinfo=UTC)
    assert runner.claim("payment-checks", DAY, now, slots=("08:00",)) == "run"
    stale = now + runner.STALE_AFTER
    assert runner.claim("payment-checks", DAY, stale, slots=("08:00",)) == "run"
    assert _rows()["08:00"][:2] == ("running", 2)
    assert runner.claim("payment-checks", DAY, stale + runner.STALE_AFTER, slots=("08:00",)) == "stalled"


def test_a_stale_slot_a_later_one_overtook_is_failed_not_retried() -> None:
    now = datetime(2026, 11, 10, 8, 50, tzinfo=UTC)
    assert runner.claim("payment-checks", DAY, now, slots=("08:00",)) == "run"
    # At 09:05 the 08:00 run is still running and fresh: 09:00 waits for it.
    assert runner.claim("payment-checks", DAY, now + timedelta(minutes=15), slots=("08:00", "09:00")) is None
    # Once it's stale, 09:00 overtakes it: 08:00 fails, and is never retried.
    at = now + runner.STALE_AFTER
    assert runner.claim("payment-checks", DAY, at, slots=("08:00", "09:00")) == "run"
    rows = _rows()
    assert rows["08:00"] == ("failed", 1, "run_overtaken") and rows["09:00"][0] == "running"


def test_the_runs_table_moves_to_a_slot_key_keeping_its_rows(db: Path) -> None:
    for table in ("watch_runs", "watch_days"):
        conn = sqlite3.connect(str(db))
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
        conn.close()
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE watch_runs (job TEXT NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL,
        started_at TEXT NOT NULL, finished_at TEXT, attempts INTEGER NOT NULL DEFAULT 1,
        cards INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', reads TEXT NOT NULL DEFAULT '[]',
        failure_noticed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (job, day))""")
    conn.execute("INSERT INTO watch_runs (job, day, status, started_at, cards, failure_noticed) "
                 "VALUES ('collections', '2026-11-09', 'failed', 'x', 2, 1)")
    conn.commit()
    conn.close()
    conn = watch_settings.connect()
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(watch_runs)")}
        row = conn.execute("SELECT slot, status, cards, detail FROM watch_runs WHERE job = 'collections'").fetchone()
        noticed = conn.execute("SELECT failure_noticed FROM watch_days WHERE job = 'collections'").fetchone()
    finally:
        conn.close()
    assert {"slot", "detail"} <= columns
    assert row == ("", "failed", 2, "{}") and noticed == (1,)
    # A day already noticed leaves no second failure notice.
    assert watch_settings.take_failure_notice("collections", "2026-11-09") is False
    # A slot and a once-a-day row of the same day are rows of their own.
    assert runner.claim("collections", "2026-11-09", datetime.now(UTC), slots=("07:00",)) == "run"


def test_the_days_card_count_is_taken_in_a_transaction_across_slots() -> None:
    assert watch_settings.take_day_cards("payment-checks", DAY, 7, 10) == 7
    assert watch_settings.take_day_cards("payment-checks", DAY, 7, 10) == 3
    assert watch_settings.take_day_cards("payment-checks", DAY, 1, 10) == 0
    assert watch_settings.take_day_cards("payment-checks", DAY, -2, 10) == -2
    assert watch_settings.day_cards("payment-checks", DAY) == 8
    assert watch_settings.take_day_cards("payment-checks", DAY, -20, 10) == -8
    assert watch_settings.day_cards("payment-checks", DAY) == 0
    assert watch_settings.take_day_cards("payment-checks", "2026-11-11", 1, 10) == 1


def test_one_failure_notice_per_job_and_day_across_slots() -> None:
    assert watch_settings.take_failure_notice("payment-checks", DAY) is True
    assert watch_settings.take_failure_notice("payment-checks", DAY) is False
    assert watch_settings.take_failure_notice("collections", DAY) is True
    assert watch_settings.take_failure_notice("payment-checks", "2026-11-11") is True


def test_the_tick_starts_only_the_latest_due_slot_on_a_weekday(
    job_on: WatchJob, monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[tuple[str, str, str]] = []

    async def fake_run(job: WatchJob, day: str, *, slot: str = "", path: Any = None) -> runner.RunOutcome:
        started.append((job.id, day, slot))
        return runner.RunOutcome("done")

    monkeypatch.setattr(runner, "run_job", fake_run)

    async def tick(now: datetime) -> int:
        count = runner.maybe_run(now)
        for task in list(runner._tasks.values()):
            await task
        return count

    # Tuesday 10:30: the 10:00 slot only; 08:00 and 09:00 skipped.
    assert asyncio.run(tick(datetime(2026, 11, 10, 10, 30, tzinfo=UTC))) == 1
    assert started == [("payment-checks", DAY, "10:00")]
    assert {s: v[0] for s, v in _rows().items()} == {"08:00": "skipped", "09:00": "skipped", "10:00": "running"}
    # Saturday: nothing.
    assert asyncio.run(tick(datetime(2026, 11, 14, 10, 30, tzinfo=UTC))) == 0


def test_the_days_card_cap_counts_across_slots(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    one = dataclasses.replace(JOB, card_cap_day=1)
    monkeypatch.setitem(registry._JOBS, JOB.id, one)
    second_draft = _pi("PI-D2", "Orion Switchgear", "INV-42", "2026-10-01", 1200.0, docstatus=0, due="2026-11-16")
    _reads(erp, due=_list([PI_D1, second_draft]), bank=_bank([]))
    erp.replies.append(_hold_checked())
    first = _run(one, slot="08:00")
    assert (first.draft_cards, first.draft_capped) == (1, 1)
    # 09:00: the first card waits; the second finding is over the day's cap.
    _reads(erp, due=_list([PI_D1, second_draft]), bank=_bank([]))
    second = _run(one, slot="09:00")
    assert (second.draft_cards, second.draft_repeats, second.draft_capped, second.checks) == (0, 1, 1, 0)
    assert watch_settings.day_cards(JOB.id, DAY) == 1
    # Its notice is left once that day, whatever the slot.
    assert [a.headline for a in _alerts()].count(f"Payment checks: 1 found on {DAY}") == 1


# --------------------------------------------------------------------------- #
# A run: the hold card, the notices and the bank-facts refusal
# --------------------------------------------------------------------------- #


def test_a_run_leaves_the_reviewer_a_hold_card_and_the_notices(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any],
) -> None:
    _reads(erp)
    erp.replies.append(_hold_checked())
    outcome = _run(job_on, slot="08:00")
    assert (outcome.status, outcome.routed, outcome.draft_cards, outcome.checks) == ("done", "reviewer", 1, 1)
    tools = [name for name, _sent in erp.calls]
    assert tools == ["erpnext__list_records", "erpnext__list_records", "erpnext__supplier_bank_changed",
                     "erpnext__draft_check"]
    for index in range(3):
        claims = erp.claims(index)
        assert (claims["kind"], claims["sub"]) == ("watch", "payment-checks")
    # Read 2 and 3 are derived from read 1's suppliers, soonest due first.
    assert _plain(erp.calls[1][1])["filters"]["supplier"] == ["in", ["Orion Switchgear", "Low Co", "Vanta Cable Mills"]]
    assert _plain(erp.calls[2][1]) == {"suppliers": ["Orion Switchgear", "Low Co", "Vanta Cable Mills"]}
    # The check: the hold as the reviewer, with the draft's name.
    check = _plain(erp.calls[3][1])
    assert check == {"operation": "update", "doctype": PI, "name": "PI-D1", "fields": HOLD}
    assert (erp.claims(3)["sub"], erp.claims(3)["purpose"]) == (REVIEWER, "draft")
    [card] = _job_cards()
    payload = drafts._payload(card)
    assert (payload["operation"], payload["name"], payload["fields"], payload["tool"]) == (
        "update", "PI-D1", HOLD, "erpnext__draft_update")
    assert (payload["source"], payload["asked_by"], payload["configured_by_person_id"]) == (
        "watch", REVIEWER, team.principal)
    [shown] = drafts.cards(people_store.get_person(team.cara))
    assert (shown.operation, shown.job_title) == ("update", "Payment checks")
    assert ("Whose", "yours") in [(f["label"], f["value"]) for f in shown.fields]
    # Three notices for the reviewer: the submitted duplicate, the weak match, the bank change.
    notices = _alerts()
    assert len(notices) == 3 and {a.routed_to_person_id for a in notices} == {team.cara}
    bank = next(a for a in notices if "Vanta Cable Mills" in a.body and "Bank details changed" in a.body)
    assert bank.body.rstrip().endswith(payment_checks.BANK_LINE) or payment_checks.BANK_LINE in bank.body
    assert bank.body.index("</untrusted_content>") < bank.body.index(payment_checks.BANK_LINE)
    assert triage == []


def test_a_hold_card_needs_the_reviewer_to_see_the_draft(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any],
) -> None:
    # ERPNext reads the draft as someone else's: no card, the finding is a notice.
    _reads(erp, bank=_bank([]))
    erp.replies.append(_hold_checked(whose="someone else's"))
    outcome = _run(job_on)
    assert (outcome.draft_cards, outcome.check_refused) == (0, 1)
    assert _job_cards() == []
    held = [a for a in _alerts() if "PI-D1" in a.body]
    assert len(held) == 1 and payment_checks.HOLD_LINE in held[0].body
    [row] = [r for r in _audit_rows("watch_job_check") if r.details["result"] != "ok"]
    assert row.details["result"] == "check_not_theirs"


def test_a_draft_that_isnt_the_reviewers_is_refused_by_erpnext_and_told_to_the_principal(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any],
) -> None:
    _reads(erp, due=_list([PI_D1]), bank=_bank([]))
    erp.replies.append(json.dumps({"error": "Purchase Invoice PI-D1 is someone else's draft and isn't assigned to you."}))
    outcome = _run(job_on)
    assert (outcome.draft_cards, outcome.routed, outcome.routed_principal) == (0, "principal", "checks_refused")
    [notice] = _alerts()
    assert notice.routed_to_person_id == team.principal and payment_checks.HOLD_LINE in notice.body
    assert "isn't assigned to you" not in notice.body


def test_a_run_for_the_principal_tells_the_duplicate_draft_as_a_notice(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any],
) -> None:
    _save(team.principal, reviewer=None)
    _reads(erp, due=_list([PI_D1]), bank=_bank([]))
    outcome = _run(job_on)
    assert (outcome.routed, outcome.checks) == ("principal", 0)
    assert not [c for c in erp.calls if c[0] == "erpnext__draft_check"]
    [notice] = _alerts()
    assert notice.routed_to_person_id == team.principal
    assert "Possible duplicate draft invoice: PI-D1" in notice.body
    assert "Name a reviewer in Settings → Watch jobs to get the hold cards." in notice.body


def test_bank_checks_off_in_erpnext_stop_the_run_with_one_notice(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any],
) -> None:
    refusal = ("Tool error: Whether suppliers' bank details changed is off on this site: an ERPNext operator "
               "turns it on with oe_connector_bank_facts, since it draws on Bank Account.")
    _reads(erp, bank=refusal)
    outcome = _run(job_on, slot="08:00")
    assert (outcome.status, outcome.error) == ("failed", "bank_facts_off")
    [notice] = _alerts()
    assert notice.routed_to_person_id == team.principal
    assert notice.body.startswith("Supplier bank checks are off in ERPNext's settings.")
    assert "oe_connector_bank_facts" not in notice.body and "Bank Account" not in notice.body
    assert _job_cards() == []
    # The same refusal at 09:00 leaves no second notice that day.
    _reads(erp, bank=json.dumps({"error": refusal}))
    assert _run(job_on, slot="09:00").error == "bank_facts_off"
    assert len(_alerts()) == 1


def test_the_bank_tool_is_read_under_payment_checks_own_marker_only(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace,
) -> None:
    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    args = {"suppliers": ["Vanta Cable Mills"]}
    erp.replies.append(_bank())
    with watch_run("payment-checks", "erpnext__supplier_bank_changed", args, today=TODAY):
        out = json.loads(asyncio.run(gateway.call_tool({"name": "erpnext__supplier_bank_changed", "arguments": args})))
    assert out["suppliers"]
    claims = erp.claims()
    assert (claims["kind"], claims["sub"], claims["tool"]) == ("watch", "payment-checks", "supplier_bank_changed")
    # Chat, any other marker and collections' own read never reach it.
    for context in (
        lambda: set_session(_owner_turn(team)),
        erpnext_caller.brief_run,
        lambda: erpnext_caller.draft_check_run(args),
        lambda: watch_run("payment-checks", "erpnext__list_records", payment_checks._due(SETTINGS, TODAY), today=TODAY),
    ):
        with context():
            refused = json.loads(asyncio.run(gateway.call_tool({"name": "erpnext__supplier_bank_changed",
                                                                "arguments": args})))
        assert "error" in refused
    assert len(erp.calls) == 1


def test_only_payment_checks_marker_opens_the_bank_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = erpnext_caller.arguments_digest({"suppliers": ["X"]})
    for job_id, expected in (("payment-checks", True), ("collections", False), ("example", False)):
        call = erpnext_caller.WatchCall(job_id, "erpnext__supplier_bank_changed", digest)
        monkeypatch.setattr(erpnext_caller, "watch_call", lambda c=call: c)
        assert mcp_gateway._job_reads_withheld("erpnext__supplier_bank_changed") is expected
        assert mcp_gateway._job_reads_withheld("erpnext__customer_payment_record") is False


def test_a_withheld_read_and_the_block_are_discovered_on_their_own_call() -> None:
    for tool in ("erpnext__supplier_bank_changed", "erpnext__customer_payment_record", "erpnext__draft_block_invoice"):
        text = f"Error: Tool '{tool}' {mcp_gateway._UNDISCOVERED_MARKER}"
        assert mcp_gateway._is_undiscovered_refusal(tool, text, ("erpnext",)) is True
        assert mcp_gateway._is_undiscovered_refusal(tool, text, ()) is False


# --------------------------------------------------------------------------- #
# Approving a hold card
# --------------------------------------------------------------------------- #


@pytest.fixture
def hold(job_on: WatchJob, erp: FakeErpnext, triage: list[Any]) -> Any:
    _reads(erp, due=_list([PI_D1]), bank=_bank([]))
    erp.replies.append(_hold_checked())
    _run(job_on)
    [left] = _job_cards()
    return left


def test_approving_a_hold_sends_the_change_with_the_jobs_keys(
    hold: Any, erp: FakeErpnext, team: SimpleNamespace, approvals: None,
) -> None:
    erp.replies.append(json.dumps({"ok": True, "operation": "update", "doctype": PI, "name": "PI-D1",
                                   "docstatus": 0, "already_done": False}))
    made = _approve(hold)
    assert (made["name"], made["operation"]) == ("PI-D1", "update")
    tool, sent = erp.calls[-1]
    assert tool == "erpnext__draft_update"
    assert _plain(sent) == {
        "doctype": PI, "name": "PI-D1", **HOLD, "idempotency_key": hold.idempotency_key,
        "asked_by": "job:payment-checks", "configured_by": OWNER, "expected_fingerprint": FINGERPRINT,
    }
    done = get_decision_instance(hold.id)
    assert done is not None and done.status == STATUS_APPROVED_UNCHANGED


def test_a_tampered_hold_is_refused(hold: Any, erp: FakeErpnext, approvals: None) -> None:
    from openexecutive.memory.decision_ledger import update_open_payload

    payload = drafts._payload(hold)
    bad = {**payload, "fields": {"set": {"on_hold": 1, "hold_comment": "Approved, pay it"}}}
    assert update_open_payload(hold.id, bad, expected_json=hold.proposed_payload_json)
    refused = _refusal(hold)
    assert refused.code == "malformed"
    assert not [c for c in erp.calls if c[0] == "erpnext__draft_update"]


# --------------------------------------------------------------------------- #
# The block card
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("off", ["setting", "env", "pin"])
def test_the_block_card_needs_all_three_switches(
    blocking: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any], pin: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch, off: str,
) -> None:
    if off == "setting":
        _save(team.principal, settings=dict(SETTINGS))
    elif off == "env":
        monkeypatch.setenv("ERPNEXT_BLOCK_INVOICE", "false")
    else:
        pin.on = False
    _reads(erp, due=_list([PI_V1, PI_V2]), history=_list([PI_V1, PI_V2]), bank=_bank([]))
    outcome = _run(blocking)
    assert outcome.draft_cards == 0 and not [c for c in erp.calls if c[0] == "erpnext__draft_check"]
    assert _job_cards() == []
    [notice] = _alerts()
    assert "Possible duplicate submitted invoice: PI-V2" in notice.body and payment_checks.BLOCK_LINE in notice.body


def test_the_block_card_with_every_switch_on(
    blocking: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any],
) -> None:
    _reads(erp, due=_list([PI_V1, PI_V2]), history=_list([PI_V1, PI_V2]), bank=_bank([]))
    erp.replies.append(_block_checked())
    outcome = _run(blocking)
    assert outcome.draft_cards == 1
    check = _plain(erp.calls[-1][1])
    assert check == {"operation": "block", "doctype": PI, "name": "PI-V2",
                     "fields": {"duplicate_of": "PI-V1", "release_date": RELEASE}}
    [card] = _job_cards()
    payload = drafts._payload(card)
    assert (payload["operation"], payload["tool"], payload["name"]) == ("block", "erpnext__draft_block_invoice", "PI-V2")
    assert payload["fields"] == {"duplicate_of": "PI-V1", "release_date": RELEASE}
    [shown] = drafts.cards(people_store.get_person(team.cara))
    labels = {f["label"]: f["value"] for f in shown.fields}
    assert shown.operation == "block"
    assert (labels["Likely repeats"], labels["Release date"], labels["Payment orders"]) == (
        "Purchase Invoice PI-V1", RELEASE, "PO-1")
    assert any(f"held until someone unblocks it, at the latest {RELEASE}" in n for n in shown.notes)
    assert _alerts() == []


def test_a_block_check_that_doesnt_echo_the_card_leaves_none(
    blocking: WatchJob, erp: FakeErpnext, triage: list[Any],
) -> None:
    _reads(erp, due=_list([PI_V1, PI_V2]), history=_list([PI_V1, PI_V2]), bank=_bank([]))
    erp.replies.append(_block_checked(release=(TODAY + timedelta(days=20)).isoformat()))
    outcome = _run(blocking)
    assert (outcome.draft_cards, outcome.check_refused) == (0, 1)
    [notice] = _alerts()
    assert payment_checks.BLOCK_LINE in notice.body


@pytest.fixture
def block_card(blocking: WatchJob, erp: FakeErpnext, triage: list[Any]) -> Any:
    _reads(erp, due=_list([PI_V1, PI_V2]), history=_list([PI_V1, PI_V2]), bank=_bank([]))
    erp.replies.append(_block_checked())
    _run(blocking)
    [left] = _job_cards()
    return left


def test_approving_a_block_sends_exactly_its_arguments(
    block_card: Any, erp: FakeErpnext, team: SimpleNamespace, approvals: None,
) -> None:
    erp.replies.append(json.dumps({"ok": True, "operation": "block", "doctype": PI, "name": "PI-V2",
                                   "already_done": False, "row_hash": "a" * 64, "chain_index": 3}))
    made = _approve(block_card)
    assert (made["operation"], made["name"], made["row_hash"]) == ("block", "PI-V2", "a" * 64)
    tool, sent = erp.calls[-1]
    assert tool == "erpnext__draft_block_invoice"
    # No doctype and no hold comment: the release date is the card's own.
    assert _plain(sent) == {
        "name": "PI-V2", "duplicate_of": "PI-V1", "release_date": RELEASE,
        "idempotency_key": block_card.idempotency_key, "asked_by": "job:payment-checks",
        "configured_by": OWNER, "expected_fingerprint": FINGERPRINT,
    }
    claims = erp.claims()
    assert (claims["sub"], claims["purpose"]) == (REVIEWER, "draft")
    done = get_decision_instance(block_card.id)
    assert done is not None and done.status == STATUS_APPROVED_UNCHANGED


@pytest.mark.parametrize(("said", "code"), [
    ("This draft changed since its card was proposed: the invoice changed. Nothing was held.", "changed_since_proposed"),
    ("Separation of duties: you set up the job that proposed this block.", "separation_of_duties"),
    ("Blocking limit: 10 holds from Dynocortex are open on this site.", "blocking_limit"),
    ("PI-V2 was unblocked by a person after an earlier hold; it isn't blocked again.", "unblocked_before"),
    ("a block comes only from the payment-checks job's card, which names who set the job up.", "not_from_job"),
])
def test_a_blocks_final_refusals_close_the_card(
    block_card: Any, erp: FakeErpnext, approvals: None, said: str, code: str,
) -> None:
    erp.replies.append(json.dumps({"error": said}))
    refused = _refusal(block_card)
    assert (refused.status, refused.code) == (409, code)
    closed = get_decision_instance(block_card.id)
    assert closed is not None and closed.status == STATUS_FAILED


def test_another_refusal_leaves_the_block_waiting(block_card: Any, erp: FakeErpnext, approvals: None) -> None:
    erp.replies.append(json.dumps({"error": "ERPNext is busy"}))
    refused = _refusal(block_card)
    assert refused.code == "draft_failed"
    again = get_decision_instance(block_card.id)
    assert again is not None and again.status == STATUS_PROPOSED


@pytest.mark.parametrize("off", ["env", "setting", "pin"])
def test_a_block_approved_after_a_switch_went_off_is_refused(
    block_card: Any, erp: FakeErpnext, team: SimpleNamespace, approvals: None, pin: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch, off: str,
) -> None:
    if off == "env":
        monkeypatch.delenv("ERPNEXT_BLOCK_INVOICE")
    elif off == "setting":
        _save(team.principal, settings=dict(SETTINGS))
    else:
        pin.on = False
    calls = len(erp.calls)
    refused = _refusal(block_card)
    assert (refused.status, refused.code) == (409, "block_off")
    assert len(erp.calls) == calls
    waiting = get_decision_instance(block_card.id)
    assert waiting is not None and waiting.status == STATUS_PROPOSED


def test_a_block_from_anywhere_but_the_job_is_refused(
    block_card: Any, erp: FakeErpnext, approvals: None,
) -> None:
    from openexecutive.memory.decision_ledger import update_open_payload

    payload = drafts._payload(block_card)
    for bad in ({**payload, "source": "chat"}, {**payload, "job": "collections"}):
        fresh = get_decision_instance(block_card.id)
        assert fresh is not None
        assert update_open_payload(block_card.id, bad, expected_json=fresh.proposed_payload_json)
        refused = _refusal(block_card)
        # Not the job's card (malformed), or collections' own row, which is off.
        assert refused.code in ("malformed", "job_off")
    assert not [c for c in erp.calls if c[0] == "erpnext__draft_block_invoice"]


def test_a_blocks_configurer_is_refused(
    blocking: WatchJob, erp: FakeErpnext, team: SimpleNamespace, approvals: None,
) -> None:
    reviewer = people_store.get_person(team.cara)
    decision_id, _new = drafts.leave_job_card(
        blocking, DAY, "block", PI, {"duplicate_of": "PI-V1", "release_date": RELEASE},
        json.loads(_block_checked()), reviewer=reviewer, configured_by_person_id=team.cara,
        dedup_key=runner.dedup_key(JOB.id, "duplicate:PI-V2:PI-V1"), why="Hold", name="PI-V2",
    )
    left = get_decision_instance(decision_id)
    refused = _refusal(left)
    assert refused.code == "separation_of_duties"
    assert erp.calls == []


def test_chat_and_the_panel_can_never_propose_a_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERPNEXT_DRAFT_UPDATE_DOCTYPES", "Purchase Invoice")
    assert erpnext_changes.enabled("block") == ()
    assert "block" in erpnext_changes.OPERATIONS
    assert erpnext_changes.OPERATION_TOOLS["block"] == "erpnext__draft_block_invoice"
    refused = erpnext_changes.check_change("block", PI, "PI-V2", {"duplicate_of": "PI-V1", "release_date": RELEASE})
    assert isinstance(refused, str)
    # The draft tools stay a static set without it; the pin holds it.
    assert "erpnext__draft_block_invoice" not in ERPNEXT_DRAFT_TOOLS
    assert "draft_block_invoice" in TOOL_DIGESTS


def test_the_pin_is_the_gateways_own_reading(erp: FakeErpnext) -> None:
    gateway = mcp_gateway.get_active_gateway()
    assert gateway is not None
    assert asyncio.run(erpnext_block.pinned()) is False
    gateway._erpnext_verified.add(erpnext_block.TOOL)
    assert asyncio.run(erpnext_block.pinned()) is True


def test_the_api_lists_the_jobs_slots(job_on: WatchJob) -> None:
    from openexecutive.api.routes import watch_jobs as watch_route

    out = watch_route._job_out(JOB)
    assert out.runs_at == "08:00" and out.slots[0] == "08:00" and out.slots[-1] == "18:00"
    assert out.weekdays == [0, 1, 2, 3, 4]
    daily = watch_route._job_out(collections.JOB)
    assert daily.slots == ["07:00"] and daily.weekdays == list(range(7))


def test_a_run_of_payment_checks_needs_a_date_of_its_own(job_on: WatchJob) -> None:
    # The due read's $date is today plus due_within_days, inside 400 days.
    args = payment_checks._due({**SETTINGS, "due_within_days": 400}, TODAY)
    assert isinstance(args, dict)
    assert args["filters"]["due_date"] == ["<=", (TODAY + timedelta(days=400)).isoformat()]
    assert registry.shape_mismatch(payment_checks.DUE_SHAPE, args, TODAY) == ""
    assert payment_checks._history(SETTINGS, TODAY, (json.loads(_list([])),)) is None
    assert payment_checks._bank(SETTINGS, TODAY, (json.loads(_list([])),)) is None
    assert date.fromisoformat(RELEASE) == TODAY + timedelta(days=erpnext_block.RELEASE_DAYS)
