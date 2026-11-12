# ruff: noqa: F811 — pytest fixtures imported from other test modules share their names with the test parameters.
"""Phase 2's invoice-capture job: the owner's sender gate, the code-only
choice of what a mailed invoice bills (watch/jobs/invoice_capture.py), the
fill card left for the draft's assignee (watch/cards.py) and approved with
its own checks and day cap (integrations/erpnext_fill.py,
integrations/erpnext_drafts.py), the $mail_draft and $mail_file slots, and
a repeated read whose one call may fail (watch/registry.py, watch/runner.py)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.integrations import erpnext_changes, erpnext_fill
from openexecutive.integrations import erpnext_drafts as drafts
from openexecutive.memory.decision_ledger import (
    STATUS_APPROVED_UNCHANGED,
    STATUS_FAILED,
    STATUS_PROPOSED,
    get_decision_instance,
    list_instances,
)
from openexecutive.orchestrator import mcp_gateway
from openexecutive.orchestrator.erpnext_caller import ASSERTION_ARG
from openexecutive.orchestrator.erpnext_tool_pin import TOOL_DIGESTS
from openexecutive.prompts.connected_systems import ERPNEXT_WITHHELD_TOOLS, JOB_WITHHELD_EXCEPTIONS
from openexecutive.watch import registry, runner
from openexecutive.watch import settings as watch_settings
from openexecutive.watch.jobs import invoice_capture
from openexecutive.watch.registry import WatchJob, WatchRead

from .test_delegation_tools import OWNER, TEAM, db, fresh_turn_state  # noqa: F401
from .test_erpnext_drafts import FakeErpnext, erp, roster  # noqa: F401
from .test_watch_collections import (  # noqa: F401
    REVIEWER,
    approvals,
    signing,
    team,
)
from .test_watch_jobs import (  # noqa: F401
    DAY,
    TODAY,
    _alerts,
    _audit_rows,
    alerts_db,
    model,
    triage,
)

JOB = invoice_capture.JOB
PI, PO, PR = "Purchase Invoice", "Purchase Order", "Purchase Receipt"
ACTING = {"kind": "watch", "job": "invoice-capture", "as_job_user": True, "user": "oe-watch-invoice-capture@erp.example"}
SETTINGS = {"company": "Acme", "days_back": 14, "amount_cap_day": 50_000}
FINGERPRINT = "f" * 64
MAKER = "buyer@co.example"
GOOD = {
    "sender_from": "billing@orion.example", "sender_from_reason": "", "authserv_id": None,
    "sender_checked": "dmarc-enforced", "sender_checked_reason": "",
    "sender_policy": "reject", "sender_policy_domain": "orion.example",
}


def _draft(name: str, supplier: str | None, *, file: str | None = "F-1", assigned: Any = (REVIEWER,),
           empty: bool = True, **facts: Any) -> dict[str, Any]:
    return {
        "name": name, "company": "Acme", "created": f"{DAY} 08:00:00", "empty": empty,
        "sender": "billing@orion.example", "email_account": "Invoices", "mail": f"COMM-{name}", "mail_reason": "",
        **{**GOOD, **facts},
        "supplier": {"name": supplier, "supplier_name": supplier, "tax_id": ""} if supplier else None,
        "supplier_reason": "" if supplier else "no Contact holds the sender's address",
        "contacts": [],
        "attachment": {"file": file, "file_name": "invoice.pdf", "size": 1000, "sha256": "0" * 64} if file else None,
        "attachment_reason": "" if file else "the mail has no file",
        "assigned_to": list(assigned),
    }


def _mail(rows: list[dict[str, Any]], *, truncated: bool = False) -> str:
    return json.dumps({"company": "Acme", "period": {"from": DAY, "to": DAY}, "drafts": rows, "truncated": truncated,
                       "acting": ACTING})


def _pdf(name: str, *, number: str = "INV-2026-77", day: str = "2026-11-02", totals: Any = None,
         orders: Any = (), currencies: Any = ("EUR",), no_text: bool = False) -> str:
    fields: dict[str, Any] = {
        "invoice_number": {"value": number, "page": 1, "source": "document"},
        "invoice_date": {"value": day, "page": 1, "source": "document"},
        "totals": [{"label": "grand total", "value": "1,200.00", "page": 1}] if totals is None else list(totals),
        "currencies": list(currencies),
        "purchase_order_references": [
            {"value": ref, "page": 1, "source": "document", "purchase_order_you_can_open": can} for ref, can in orders
        ],
    }
    return json.dumps({"doctype": PI, "name": name, "file": {"file": "F-1"}, "text": [], "tables": [],
                       "fields": {} if no_text else fields, "no_text": no_text, "truncated": False, "notes": [],
                       "acting": ACTING})


def _list(rows: list[dict[str, Any]], doctype: str) -> str:
    return json.dumps({"doctype": doctype, "rows": rows, "truncated": False, "acting": ACTING})


ORDERS = [{"name": "PO-1", "supplier": "Orion", "currency": "EUR", "grand_total": 1200.0,
           "transaction_date": "2026-10-20", "per_received": 100, "per_billed": 0, "status": "To Bill"}]
RECEIPTS = [{"name": "PR-7", "supplier": "Vanta", "currency": "EUR", "grand_total": 500.0,
             "posting_date": "2026-10-25", "per_billed": 0, "status": "To Bill"}]
COMPANY = [{"name": "Acme", "default_currency": "EUR"}]
# PI-1: Orion's, naming its open PO-1 (a card). PI-2: Vanta's, naming no
# order, at PR-7's 500 (a card). PI-3: a domain that asks for nothing (the
# gate). PI-4: no supplier. PI-5: two orders named. PI-6: an unreadable PDF.
DRAFTS = [
    _draft("PI-1", "Orion"),
    _draft("PI-2", "Vanta", sender_from="ar@vanta.example", sender_policy_domain="vanta.example"),
    _draft("PI-3", "Orion", sender_policy="none"),
    _draft("PI-4", None),
    _draft("PI-5", "Orion"),
    _draft("PI-6", "Orion"),
]


def _reads(erp: FakeErpnext) -> None:
    erp.replies.append(_mail(DRAFTS))
    erp.replies.extend([
        _pdf("PI-1", orders=[("PO-1", True)]),
        _pdf("PI-2", number="V-500", totals=[{"label": "total", "value": "500,00", "page": 1}]),
        _pdf("PI-5", orders=[("PO-1", True), ("PO-2", True)]),
        json.dumps({"error": "'invoice.pdf' couldn't be read (parse failed)."}),
    ])
    erp.replies.extend([_list(ORDERS, PO), _list(RECEIPTS, PR), _list(COMPANY, "Company")])


def _summary(name: str, source: tuple[str, str], total: float, *, supplier: str = "Orion",
             sender_from: str = "billing@orion.example", bill_no: str = "INV-2026-77", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "operation": "fill", "doctype": PI, "name": name, "company": "Acme",
        "source": {"doctype": source[0], "name": source[1], "modified": "2026-10-20 10:00"},
        "supplier": {"name": supplier, "supplier_name": supplier, "tax_id": ""},
        "sender": sender_from, "sender_from": sender_from, "sender_from_reason": "", "authserv_id": None,
        "sender_checked": "dmarc-enforced", "sender_policy": "reject",
        "sender_policy_domain": sender_from.split("@")[1], "contacts": [], "attachment": None,
        "currency": "EUR", "credit_to": "Creditors - A",
        "rows": [{"row": 1, "item_code": "CABLE", "po_detail": "x", "qty_mapped": 10, "rate_mapped": total / 10,
                  "qty": 10, "rate": total / 10, "amount": total, "lowered": None}],
        "taxes": [], "totals": {"net_total": total, "grand_total": total},
        "bill_no": bill_no, "bill_date": "2026-11-02", "bill_no_collides": None, "creators": [MAKER],
        "assigned_to": [REVIEWER], "can_write": True, "draft_modified": f"{DAY} 08:00",
    }
    out.update(extra)
    return out


def _checked(summary: dict[str, Any]) -> str:
    return json.dumps({"ok": True, "operation": "fill", "doctype": PI, "name": summary["name"], "summary": summary,
                       "fingerprint": FINGERPRINT})


def _pi1(**extra: Any) -> str:
    return _checked(_summary("PI-1", (PO, "PO-1"), 1200.0, **extra))


def _pi2(**extra: Any) -> str:
    return _checked(_summary("PI-2", (PR, "PR-7"), 500.0, supplier="Vanta", sender_from="ar@vanta.example",
                             bill_no="V-500", **extra))


def _save(by: int, **kw: Any) -> None:
    watch_settings.save(
        JOB, enabled=kw.get("enabled", True), settings=kw.get("settings", dict(SETTINGS)),
        reviewer_person_id=kw.get("reviewer"), set_reviewer="reviewer" in kw,
        configured_by_person_id=by, now=datetime.now(UTC),
    )


@pytest.fixture
def pin(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(on=True)

    async def pinned() -> bool:
        return bool(state.on)

    monkeypatch.setattr(erpnext_fill, "pinned", pinned)
    return state


@pytest.fixture
def job_on(erp: FakeErpnext, team: SimpleNamespace, signing: None, pin: SimpleNamespace,
           monkeypatch: pytest.MonkeyPatch) -> WatchJob:
    """Invoice capture on, reviewed by Cara, with ERPNEXT_INVOICE_FILL and the pin."""
    monkeypatch.setenv("ERPNEXT_WATCH_JOBS_ENABLED", "true")
    monkeypatch.setenv("ERPNEXT_DRAFTS_TEAMMATES", "true")
    monkeypatch.setenv("ERPNEXT_INVOICE_FILL", "true")
    monkeypatch.setattr(erpnext_fill, "company_today", lambda: TODAY)
    _save(team.principal, reviewer=team.cara)
    return JOB


def _run(job: WatchJob = JOB, day: str = DAY) -> runner.RunOutcome:
    assert runner.claim(job.id, day, datetime.now(UTC), slots=("",)) == "run"
    return asyncio.run(runner.run_job(job, day))


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


# --------------------------------------------------------------------------- #
# The owner's gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("facts", "allowed"), [
    ({}, True),
    ({"sender_policy": "quarantine", "authserv_id": "mx.co.example"}, True),
    ({"sender_policy": "quarantine", "authserv_id": None}, False),
    ({"sender_policy": "quarantine", "authserv_id": "  "}, False),
    ({"sender_policy": "none"}, False),
    ({"sender_policy": "missing"}, False),
    ({"sender_policy": "lookup_failed"}, False),
    ({"sender_from": None}, False),
    ({"sender_from": ""}, False),
    ({"sender_checked": None}, False),
])
def test_the_gate_is_the_owners_rule(facts: dict[str, Any], allowed: bool) -> None:
    assert erpnext_fill.fill_allowed({**GOOD, **facts}) is allowed


@pytest.mark.parametrize("missing", ["sender_checked", "sender_from", "sender_policy"])
def test_a_missing_key_never_passes_the_gate(missing: str) -> None:
    facts = {k: v for k, v in GOOD.items() if k != missing}
    assert erpnext_fill.fill_allowed(facts) is False
    assert erpnext_fill.fill_allowed("reject") is False  # type: ignore[arg-type]


def test_the_gate_says_why() -> None:
    assert "quarantine" in erpnext_fill.gate_reason({**GOOD, "sender_policy": "quarantine"})
    assert "wasn't confirmed: forwarded" in erpnext_fill.gate_reason(
        {**GOOD, "sender_from": None, "sender_from_reason": "forwarded"})
    assert "none" in erpnext_fill.gate_reason({**GOOD, "sender_policy": "none"})


# --------------------------------------------------------------------------- #
# Reading the PDF, choosing the source
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("text", "expected"), [
    ("2026-11-02", ("2026-11-02", "")),
    ("15.10.2026", ("2026-10-15", "")),
    ("10/15/2026", ("2026-10-15", "")),
    ("05/05/2026", ("2026-05-05", "")),
    ("03/04/2026", ("", "ambiguous_date")),
    ("12 Nov 2026", ("2026-11-12", "")),
    ("November 12, 2026", ("2026-11-12", "")),
    ("31/02/2026", ("", "no_date")),
    ("soon", ("", "no_date")),
    (None, ("", "no_date")),
])
def test_invoice_dates(text: Any, expected: tuple[str, str]) -> None:
    assert invoice_capture.normalize_date(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("1,234.56", 1234.56), ("1.234,56", 1234.56), ("500,00", 500.0), ("1,200", 1200.0), ("$ 99.5", 99.5),
    ("12,34,5", None), ("-", None), ("0.00", None), ("abc", None),
])
def test_amounts(text: str, expected: float | None) -> None:
    assert invoice_capture.parse_amount(text) == expected


def test_the_total_is_one_figure_under_the_first_label() -> None:
    assert invoice_capture.pdf_total([{"label": "Total", "value": "10.00"},
                                      {"label": "Grand Total", "value": "12.00"}]) == (12.0, "")
    assert invoice_capture.pdf_total([{"label": "total", "value": "10.00"},
                                      {"label": "total", "value": "11.00"}]) == (None, "no_total")
    assert invoice_capture.pdf_total([]) == (None, "no_total")


def _d(supplier: str = "Orion") -> invoice_capture.Draft:
    return invoice_capture.Draft("PI-1", GOOD, supplier, supplier, "F-1", True, "")


def _p(**kw: Any) -> invoice_capture.Pdf:
    return invoice_capture.read_pdf(json.loads(_pdf("PI-1", **kw)))


OPEN_PO = invoice_capture.Open(PO, "PO-1", "Orion", "EUR", 1200.0)
OPEN_PR = invoice_capture.Open(PR, "PR-7", "Orion", "EUR", 1200.0)


def test_one_open_order_named_is_the_source() -> None:
    assert invoice_capture.choose_source(_d(), _p(orders=[("PO-1", True)]), [OPEN_PO], [], "EUR") == (OPEN_PO, "")


@pytest.mark.parametrize(("orders", "open_orders", "why"), [
    ([("PO-1", True), ("PO-2", True)], [OPEN_PO], "several_orders"),
    ([("PO-1", False)], [OPEN_PO], "order_not_open"),
    ([("PO-1", True)], [], "order_not_open"),
    ([("PO-1", True)], [invoice_capture.Open(PO, "PO-1", "Vanta", "EUR", 1200.0)], "order_not_open"),
])
def test_an_order_reference_that_isnt_one_open_order_of_theirs(orders: Any, open_orders: Any, why: str) -> None:
    assert invoice_capture.choose_source(_d(), _p(orders=orders), open_orders, [], "EUR") == (None, why)


def test_no_reference_takes_the_one_receipt_at_the_total() -> None:
    assert invoice_capture.choose_source(_d(), _p(), [], [OPEN_PR], "EUR") == (OPEN_PR, "")
    other = invoice_capture.Open(PR, "PR-8", "Orion", "EUR", 1200.0)
    assert invoice_capture.choose_source(_d(), _p(), [], [OPEN_PR, other], "EUR") == (None, "several_receipts")
    assert invoice_capture.choose_source(_d(), _p(), [], [], "EUR") == (None, "no_receipt")
    assert invoice_capture.choose_source(_d("Vanta"), _p(), [], [OPEN_PR], "EUR") == (None, "no_receipt")


def test_a_pdf_in_another_currency_needs_a_person() -> None:
    assert invoice_capture.choose_source(_d(), _p(currencies=["USD"]), [OPEN_PO], [], "EUR") == (None, "other_currency")


def test_a_pdf_naming_several_currencies_needs_a_person() -> None:
    # A EUR invoice that mentions USD in its bank details is never taken as USD.
    pdf = _p(currencies=["EUR", "USD"])
    assert invoice_capture.choose_source(_d(), pdf, [OPEN_PO], [OPEN_PR], "EUR") == (None, "several_currencies")
    assert invoice_capture.choose_source(_d(), _p(currencies=[]), [], [OPEN_PR], "EUR") == (OPEN_PR, "")


def test_an_order_reference_the_job_cant_read_never_falls_to_a_receipt() -> None:
    pdf = _p(orders=[("", True)])
    assert pdf.reason == "unread_order_ref" and pdf.orders == ()
    bad = json.loads(_pdf("PI-1"))
    bad["fields"]["purchase_order_references"] = "PO-1"
    assert invoice_capture.read_pdf(bad).reason == "unread_order_ref"


def test_a_cut_receipts_read_never_tells_exactly_one() -> None:
    assert invoice_capture.choose_source(_d(), _p(), [], [OPEN_PR], "EUR", receipts_cut=True) == (None, "receipts_cut")
    assert invoice_capture.documents_cut({"rows": [], "truncated": True})
    assert not invoice_capture.documents_cut({"rows": [], "truncated": False})


def test_an_invoice_date_is_within_a_year_back_and_a_month_ahead() -> None:
    today = date(2026, 11, 11)
    assert invoice_capture._date_in_range("2026-11-02", today)
    assert invoice_capture._date_in_range("2025-11-11", today)
    assert not invoice_capture._date_in_range("2025-11-09", today)
    assert invoice_capture._date_in_range("2026-12-12", today)
    assert not invoice_capture._date_in_range("2099-01-01", today)
    assert not invoice_capture._date_in_range("1990-01-01", today)


def _build(rows: list[dict[str, Any]]) -> list[Any]:
    reads = (
        json.loads(_mail(rows)), {}, {}, {},
        {"rows": [{"name": "Acme", "default_currency": "EUR"}], "truncated": False},
    )
    settings = {**invoice_capture.JOB.default_settings, "company": "Acme"}
    return invoice_capture.build(reads, settings, date.fromisoformat(DAY))


def test_a_mail_the_gate_keeps_is_never_told_as_one_a_person_fills() -> None:
    # A spoofed supplier from a p=none domain, with no Supplier matched: the gate's notice, never "a person fills".
    found = _build([_draft("PI-9", None, sender_policy="none", sender_from=None)])
    summaries = [c.summary for c in found]
    assert "Mailed invoices that can't be filled from here" in summaries
    assert "Mailed invoices a person fills" not in summaries
    gate = next(c for c in found if c.summary == "Mailed invoices that can't be filled from here")
    values = dict(gate.items[0].values)
    # An unconfirmed sender is said to be unconfirmed, never shown as who sent it.
    assert "Sent from" not in values and values["Sender (not confirmed)"] == "billing@orion.example"


def test_a_run_counts_the_pdfs_it_couldnt_read() -> None:
    assert runner.RunOutcome("done", unread=2).detail()["unread"] == 2
    assert "unread" not in runner.RunOutcome("done").detail()


def test_what_a_pdf_lacks_needs_a_person() -> None:
    assert _p(no_text=True).reason == "no_text"
    assert _p(totals=[]).reason == "no_total"
    assert _p(number="").reason == "no_number"
    assert _p(day="03/04/2026").reason == "ambiguous_date"
    assert invoice_capture.read_pdf({"unread": True}).reason == "unread"


# --------------------------------------------------------------------------- #
# The job, the slots and the reads
# --------------------------------------------------------------------------- #


def test_the_job_is_listed_with_its_reads_slots_and_bounds() -> None:
    assert registry.get("invoice-capture") is JOB
    assert [r.tool for r in JOB.reads] == [
        "mail_invoices_to_fill", "read_attachment", "list_records", "list_records", "list_records"]
    assert JOB.reads[1].repeat == invoice_capture.MAX_PDFS and JOB.reads[1].may_fail
    assert JOB.slots == invoice_capture.SLOTS and JOB.weekdays == frozenset(range(5))
    listing = invoice_capture.listing()
    assert listing["as_job_user"] is True
    assert listing["reads"][1] == {"tool": "read_attachment", "doctype": PI, "name": "$mail_draft", "file": "$mail_file"}
    for slot in ("$mail_draft", "$mail_file"):
        assert slot in registry.SLOTS


def test_the_mail_slots_take_a_document_name_only() -> None:
    shape = invoice_capture.ATTACHMENT_SHAPE
    assert registry.shape_mismatch(shape, {"doctype": PI, "name": "PI-1", "file": "F-1"}, TODAY) == ""
    assert registry.shape_mismatch(shape, {"doctype": PI, "name": ["PI-1"], "file": "F-1"}, TODAY) != ""
    assert registry.shape_mismatch(shape, {"doctype": "Sales Invoice", "name": "PI-1", "file": "F-1"}, TODAY) != ""


def test_the_mail_slots_stand_only_as_a_read_attachment_reads_name_and_file() -> None:
    shape = dict(invoice_capture.ATTACHMENT_SHAPE)
    assert registry.shape_problem(shape) == "" and registry.mail_tool_problem("read_attachment", shape) == ""
    for tool in ("get_record", "list_records", "describe_doctype"):
        assert registry.mail_tool_problem(tool, shape)
        # A read tool with the slots is unsound; any other tool isn't a job's at all.
        with pytest.raises(ValueError, match="unsound|may not list"):
            WatchJob(id="x-test", title="X", reads=(WatchRead(tool, shape, lambda s, d, p: None, "a draft"),),
                     build=lambda r, s, d: [], schedule=JOB.schedule, card_cap_day=1, default_settings={})
    for unsound in (
        {"doctype": PI, "filters": {"name": ["=", "$mail_draft"]}},
        {"doctype": PI, "filters": {"name": "$mail_draft"}},
        {"doctype": PI, "name": ["$mail_draft"], "file": "$mail_file"},
        {"doctype": PI, "name": "$mail_file", "file": "$mail_draft"},
        {"doctype": PI, "name": "$mail_draft"},
        {"doctype": "Sales Invoice", "name": "$mail_draft", "file": "$mail_file"},
    ):
        assert registry.shape_problem(unsound), unsound


def test_only_a_repeated_read_may_fail_on_its_own() -> None:
    with pytest.raises(ValueError, match="repeated read"):
        WatchJob(id="x-test", title="X", reads=(WatchRead("list_records", {"doctype": "Item"}, lambda s, d, p: None,
                                                          "items", may_fail=True),),
                 build=lambda r, s, d: [], schedule=JOB.schedule, card_cap_day=1, default_settings={})


def test_the_capture_reads_open_to_this_job_alone() -> None:
    owned = JOB_WITHHELD_EXCEPTIONS["invoice-capture"]
    assert owned == frozenset({"erpnext__mail_invoices_to_fill", "erpnext__read_attachment"})
    assert owned <= ERPNEXT_WITHHELD_TOOLS
    assert not any(owned & tools for job, tools in JOB_WITHHELD_EXCEPTIONS.items() if job != "invoice-capture")


def test_the_pin_holds_the_fill_tools() -> None:
    assert {"draft_fill_purchase_invoice", "mail_invoices_to_fill", "read_attachment"} <= set(TOOL_DIGESTS)
    assert erpnext_fill.TOOL in mcp_gateway.ERPNEXT_JOB_WRITE_TOOLS


def test_chat_and_the_panel_can_never_propose_a_fill() -> None:
    assert erpnext_changes.enabled("fill") == ()
    assert erpnext_changes.OPERATION_TOOLS["fill"] == erpnext_fill.TOOL


def test_check_fields_takes_only_a_whole_fill() -> None:
    good = {"source": {"doctype": PO, "name": "PO-1"}, "lower": [], "bill_no": "INV-1", "bill_date": "2026-11-02"}
    assert invoice_capture.check_fields("fill", PI, good, "PI-1") == good
    assert isinstance(invoice_capture.check_fields("match", PI, good, "PI-1"), str)
    for bad in (
        {**good, "lower": [{"row": 1, "field": "qty", "value": 1}]},
        {**good, "source": {"doctype": "Sales Order", "name": "SO-1"}},
        {**good, "bill_no": "!bad"},
        {**good, "bill_date": "02/11/2026"},
        {k: v for k, v in good.items() if k != "bill_no"},
    ):
        assert isinstance(invoice_capture.check_fields("fill", PI, bad, "PI-1"), str)


# --------------------------------------------------------------------------- #
# A run
# --------------------------------------------------------------------------- #


def test_a_run_leaves_the_assignee_two_fill_cards_and_the_notices(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any],
) -> None:
    _reads(erp)
    erp.replies.extend([_pi1(), _pi2()])
    outcome = _run(job_on)
    assert (outcome.status, outcome.routed, outcome.draft_cards) == ("done", "reviewer", 2)
    tools = [name for name, _sent in erp.calls]
    assert tools == ["erpnext__mail_invoices_to_fill"] + ["erpnext__read_attachment"] * 4 + [
        "erpnext__list_records"] * 3 + ["erpnext__draft_check"] * 2
    # Only the drafts the gate let through, with a supplier and a file.
    assert [_plain(erp.calls[i][1])["name"] for i in range(1, 5)] == ["PI-1", "PI-2", "PI-5", "PI-6"]
    assert _plain(erp.calls[8][1]) == {"operation": "fill", "doctype": PI, "name": "PI-1", "fields": {
        "source": {"doctype": PO, "name": "PO-1"}, "lower": [], "bill_no": "INV-2026-77", "bill_date": "2026-11-02"}}
    assert _plain(erp.calls[9][1])["fields"]["source"] == {"doctype": PR, "name": "PR-7"}
    cards = {drafts._payload(c)["name"]: c for c in _job_cards()}
    assert set(cards) == {"PI-1", "PI-2"}
    payload = drafts._payload(cards["PI-1"])
    assert (payload["operation"], payload["tool"]) == ("fill", "erpnext__draft_fill_purchase_invoice")
    assert payload["slice"]["grand_total"] == 1200.0 and payload["slice"]["assigned_to"] == [REVIEWER]
    shown = next(c for c in drafts.cards(team_person(team.cara)) if "PI-1" in c.why)
    labels = {f["label"]: f["value"] for f in shown.fields}
    assert labels["Bills"] == "Purchase Order PO-1" and labels["Grand total"] == "1,200.00 EUR"
    assert labels["On the supplier's PDF: total"] == "1,200.00 EUR"
    bodies = " ".join(a.body for a in _alerts())
    assert "PI-3" in bodies and "PI-4" in bodies and "PI-5" in bodies and "PI-6" in bodies
    assert invoice_capture.GATE_LINE in bodies and invoice_capture.PERSON_LINE in bodies
    assert "couldn't be read" in bodies or invoice_capture.WHY["unread"] in bodies


def team_person(person_id: int) -> Any:
    from openexecutive.people import store as people_store

    return people_store.get_person(person_id)


@pytest.mark.parametrize(("reply", "code", "said"), [
    (lambda: _pi1(currency="USD"), "check_fill_currency", "another currency"),
    (lambda: _pi1(totals={"net_total": 1100.0, "grand_total": 1100.0}), "check_fill_total", "total"),
    (lambda: _pi1(assigned_to=["someone@co.example"]), "check_fill_assignee", "points the Purchase Invoice assignment rule"),
    (lambda: _pi1(bill_no_collides="PI-0"), "check_fill_collides", "already has an invoice"),
    (lambda: _pi1(creators=[REVIEWER]), "check_fill_sod", "different reviewer"),
    (lambda: _pi1(sender_policy="quarantine"), "check_fill_sender", "can't trust"),
])
def test_what_erpnext_reads_back_must_be_the_whole_fill(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any], reply: Any, code: str, said: str,
) -> None:
    _reads(erp)
    erp.replies.extend([reply(), _pi2()])
    outcome = _run(job_on)
    assert outcome.draft_cards == 1
    assert {r.details["result"] for r in _audit_rows("watch_job_check")} == {code, "ok"}
    assert any(said in a.body for a in _alerts())


def test_a_finding_kept_from_the_reviewer_goes_to_the_principal(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any],
) -> None:
    _reads(erp)
    erp.replies.extend([_pi1(creators=[REVIEWER]), _pi2()])
    _run(job_on)
    kept = next(a for a in _alerts() if "Likely fill for PI-1" in a.body)
    assert kept.routed_to_person_id == team.principal


def test_the_days_amount_cap_holds_a_card_back(
    job_on: WatchJob, erp: FakeErpnext, team: SimpleNamespace, triage: list[Any],
) -> None:
    _save(team.principal, reviewer=team.cara, settings={**SETTINGS, "amount_cap_day": 1300})
    _reads(erp)
    erp.replies.extend([_pi1(), _pi2()])
    outcome = _run(job_on)
    assert outcome.draft_cards == 1
    assert "today's matching limit is reached" in " ".join(a.body for a in _alerts())


@pytest.mark.parametrize("off", ["env", "pin"])
def test_the_fill_card_needs_its_switch_and_the_pin(
    job_on: WatchJob, erp: FakeErpnext, triage: list[Any], pin: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    off: str,
) -> None:
    if off == "env":
        monkeypatch.setenv("ERPNEXT_INVOICE_FILL", "false")
    else:
        pin.on = False
    _reads(erp)
    outcome = _run(job_on)
    assert outcome.draft_cards == 0
    assert "erpnext__draft_check" not in [name for name, _sent in erp.calls]
    assert any("Likely fill for PI-1" in a.body for a in _alerts())


def test_an_unreadable_pdf_is_that_drafts_alone(
    job_on: WatchJob, erp: FakeErpnext, triage: list[Any],
) -> None:
    _reads(erp)
    erp.replies.extend([_pi1(), _pi2()])
    outcome = _run(job_on)
    assert outcome.status == "done"


# --------------------------------------------------------------------------- #
# Approving a fill card
# --------------------------------------------------------------------------- #


@pytest.fixture
def cards(job_on: WatchJob, erp: FakeErpnext, triage: list[Any]) -> dict[str, Any]:
    _reads(erp)
    erp.replies.extend([_pi1(), _pi2()])
    _run(job_on)
    return {drafts._payload(c)["name"]: c for c in _job_cards()}


def _done(name: str) -> str:
    return json.dumps({"ok": True, "operation": "fill", "doctype": PI, "name": name, "already_done": False,
                       "row_hash": "a" * 64, "chain_index": 9})


def test_approving_a_fill_sends_exactly_its_arguments(
    cards: dict[str, Any], erp: FakeErpnext, team: SimpleNamespace, approvals: None,
) -> None:
    card = cards["PI-1"]
    erp.replies.append(_done("PI-1"))
    made = _approve(card)
    assert (made["operation"], made["name"]) == ("fill", "PI-1")
    tool, sent = erp.calls[-1]
    assert tool == "erpnext__draft_fill_purchase_invoice"
    assert _plain(sent) == {
        "name": "PI-1", "source": {"doctype": PO, "name": "PO-1"}, "lower": [], "bill_no": "INV-2026-77",
        "bill_date": "2026-11-02", "idempotency_key": card.idempotency_key, "asked_by": "job:invoice-capture",
        "configured_by": OWNER, "expected_fingerprint": FINGERPRINT,
    }
    done = get_decision_instance(card.id)
    assert done is not None and done.status == STATUS_APPROVED_UNCHANGED


@pytest.mark.parametrize("off", ["env", "pin"])
def test_a_fill_approved_after_a_switch_went_off_is_refused(
    cards: dict[str, Any], erp: FakeErpnext, approvals: None, pin: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch, off: str,
) -> None:
    if off == "env":
        monkeypatch.setenv("ERPNEXT_INVOICE_FILL", "false")
    else:
        pin.on = False
    assert _refusal(cards["PI-1"]).code == "fill_off"


@pytest.mark.parametrize("tamper", ["gate", "assignee", "total", "fields"])
def test_a_tampered_fill_or_slice_is_refused(
    cards: dict[str, Any], erp: FakeErpnext, approvals: None, tamper: str,
) -> None:
    from openexecutive.memory.decision_ledger import update_open_payload

    card = cards["PI-1"]
    payload = drafts._payload(card)
    kept = dict(payload["slice"])
    if tamper == "gate":
        kept["sender_policy"] = "none"
    elif tamper == "assignee":
        kept["assigned_to"] = ["someone@co.example"]
    elif tamper == "total":
        kept["grand_total"] = "1200"
    else:
        payload = {**payload, "fields": {**payload["fields"], "bill_no": "OTHER-1"}}
    assert update_open_payload(card.id, {**payload, "slice": kept})
    refused = _refusal(card)
    assert refused.code in ("malformed", "not_assignee")
    assert not any(t == "erpnext__draft_fill_purchase_invoice" for t, _s in erp.calls)


@pytest.mark.parametrize(("said", "code"), [
    ("This draft changed since its card was proposed: the draft, its mail, the supplier or the Purchase Order "
     "is not what the card showed.", "changed_since_proposed"),
    ("PI-0 already carries bill number INV-2026-77 for this supplier. Nothing was filled.", "bill_no_in_use"),
    ("the draft is assigned to someone else: only its assignee approves its fill.", "not_assignee"),
    ("The mail's sender can't be trusted to fill an invoice: its domain asks no DMARC enforcement.",
     "sender_untrusted"),
    ("a fill comes only from the invoice-capture job's card, which names who set it up.", "not_from_job"),
    ("Separation of duties: you made the Purchase Order this fill bills.", "separation_of_duties"),
    # ERPNext's record of who set the job up (ladislav-urban/oe_connector#552).
    ("The card says the job 'invoice-capture' was set up by a@x.example, but ERPNext records b@x.example in "
     "oe_connector_watch_jobs. Nothing was made.", "configurer_not_recorded"),
    ("ERPNext records no one as having set up the job 'invoice-capture': an ERPNext operator adds "
     "\"configured_by\" to its entry.", "configurer_not_recorded"),
])
def test_a_fills_final_refusals_close_the_card(
    cards: dict[str, Any], erp: FakeErpnext, approvals: None, said: str, code: str,
) -> None:
    erp.replies.append(json.dumps({"error": said}))
    refused = _refusal(cards["PI-1"])
    assert (refused.status, refused.code) == (409, code)
    closed = get_decision_instance(cards["PI-1"].id)
    assert closed is not None and closed.status == STATUS_FAILED


def test_a_timed_out_fill_waits_again_and_counts_once(
    cards: dict[str, Any], erp: FakeErpnext, team: SimpleNamespace, approvals: None,
) -> None:
    _save(team.principal, reviewer=team.cara, settings={**SETTINGS, "amount_cap_day": 1300})
    erp.replies.append(json.dumps({"error": "ERPNext is busy"}))
    assert _refusal(cards["PI-1"]).code == "draft_failed"
    waiting = get_decision_instance(cards["PI-1"].id)
    assert waiting is not None and waiting.status == STATUS_PROPOSED
    erp.replies.append(_done("PI-1"))
    assert _approve(cards["PI-1"])["name"] == "PI-1"
    assert _refusal(cards["PI-2"]).code == "fill_limit"


def test_the_month_after_lists_the_fills_to_the_principal(
    cards: dict[str, Any], erp: FakeErpnext, approvals: None,
) -> None:
    erp.replies.append(_done("PI-1"))
    _approve(cards["PI-1"])
    erp.replies.append(json.dumps({"error": "ERPNext is busy"}))
    assert _refusal(cards["PI-2"]).code == "draft_failed"
    done = get_decision_instance(cards["PI-1"].id)
    assert done is not None and done.resolved_at
    month_later = date.fromisoformat(done.resolved_at[:10]).replace(day=1) + timedelta(days=40)
    [notice] = invoice_capture.month_notices(SETTINGS, month_later)
    assert notice.to_principal
    values = {i.summary: dict(i.values) for i in notice.items}
    assert values["Invoice PI-1"]["Bills"] == "Purchase Order PO-1"
    assert values["Invoice PI-2"]["Outcome"] == invoice_capture.UNKNOWN_OUTCOME



# oe_connector's own fill refusals, as its draft_fill.py words them at
# ladislav-urban/oe_connector#545's head (a0b1ff34), filled with sample values.
# The markers are matched against these words, so rewording what the person
# is told must never touch a marker.
CONNECTOR_REFUSALS = (
    "PI-0 already carries bill number INV-2026-77 for this supplier. Nothing was filled.",
    "the draft is assigned to someone else: only its assignee approves its fill.",
    "The mail's sender can't be trusted to fill an invoice: its domain asks no DMARC enforcement"
    " (its domain's DMARC policy: none).",
    "a fill comes only from the invoice-capture job's card, which names who set it up.",
    "Purchase Invoice ACC-PINV-2026-00001 isn't empty: only an empty mailed draft is filled.",
    "Purchase Invoice ACC-PINV-2026-00001 isn't a draft.",
)


def test_every_fill_marker_still_matches_the_connectors_words() -> None:
    from openexecutive.integrations import erpnext_fill

    for marker, _code, _shown in erpnext_fill.FINAL:
        assert any(marker in said for said in CONNECTOR_REFUSALS), marker
    # What the person is told is ours, in the product's words.
    for _marker, _code, shown in erpnext_fill.FINAL:
        assert "ERPNext" not in shown
