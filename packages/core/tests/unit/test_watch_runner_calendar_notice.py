"""The runner's support for jobs that run on a calendar and keep one notice
current (month-end's close): ``WatchJob.runs_on``, a single read that may
fail on its own (``WatchRead.may_fail``), ``WatchJob.updates_notice`` and a
finding's own ``CardSpec.subject``. End to end with the fake ERPNext, as
test_watch_jobs.py drives the test job."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.watch import registry, runner
from openexecutive.watch.jobs import _example
from openexecutive.watch.registry import CardSpec, WatchJob, WatchRead

from ._log_capture import recording
from .test_delegation_tools import db, fresh_turn_state  # noqa: F401
from .test_erpnext_drafts import FakeErpnext, erp, roster  # noqa: F401
from .test_watch_jobs import (  # noqa: F401
    CANARY,
    DAY,
    _alerts,
    _ar,
    _enable,
    _run,
    _run_row,
    _tick,
    alerts_db,
    triage,
)


def _owed(read: Mapping[str, Any]) -> str:
    if read.get("unread") is True:
        return "not read"
    rows = read.get("rows")
    if not isinstance(rows, list):
        raise ValueError("no rows")
    return f"{sum(float(r['outstanding']) for r in rows):,.2f}"


def _calendar_job(
    job_id: str = "calendar", *, runs_on: Any = None, may_fail: bool = False, updates: bool = False,
    subject: str = "", once: bool = False, second_read: bool = False,
) -> WatchJob:
    """A job reading the receivables report (twice with ``second_read``,
    the first then ``may_fail``), whose one notice says what it read."""

    def build(reads: Sequence[Mapping[str, Any]], settings: Mapping[str, Any], today: date) -> list[CardSpec]:
        values = tuple((f"Read {n}", _owed(r)) for n, r in enumerate(reads, 1))
        return [CardSpec(key=f"state:{settings['company']}", summary="The state of things.", values=values,
                         subject=subject)]

    first = WatchRead("run_report", _example.RECEIVABLES_SHAPE, _example._receivables, "the first report",
                      may_fail=may_fail)
    second = WatchRead("run_report", _example.RECEIVABLES_SHAPE, _example._receivables, "the second report")
    return WatchJob(
        id=job_id, title="Calendar check", reads=(first, second) if second_read else (first,), build=build,
        schedule=time(7, 0), card_cap_day=3, default_settings={"company": "", "limit": 200},
        runs_on=runs_on, updates_notice=updates, once=once,
    )


@pytest.fixture
def register(
    erp: FakeErpnext, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> Any:
    """Register a job (by the tests alone), the master switch on and its row
    on with Acme's settings."""
    monkeypatch.setenv("ERPNEXT_WATCH_JOBS_ENABLED", "true")

    def go(job: WatchJob) -> WatchJob:
        monkeypatch.setitem(registry._JOBS, job.id, job)
        _enable(job, roster.principal)
        return job

    return go


def _acting(job_id: str) -> dict[str, Any]:
    return {"kind": "watch", "job": job_id, "as_job_user": True, "user": f"oe-watch-{job_id}@erp.example"}


# --------------------------------------------------------------------------- #
# runs_on
# --------------------------------------------------------------------------- #


def test_runs_on_gates_the_scheduled_run_by_day(
    register: Any, erp: FakeErpnext, triage: list[Any],  # noqa: F811
) -> None:
    seen: list[tuple[str, date]] = []

    def tenth_only(settings: Mapping[str, Any], day: date) -> bool:
        seen.append((settings["company"], day))
        return day.day == 10

    job = register(_calendar_job(runs_on=tenth_only))
    erp.replies.extend([_ar(("Acme Retail", 10.0), acting=_acting(job.id))] * 2)
    assert _tick(datetime(2026, 11, 10, 7, 0, tzinfo=UTC)) == 1
    assert _tick(datetime(2026, 11, 11, 7, 0, tzinfo=UTC)) == 0
    # Its current settings and the company's day.
    assert seen[0] == ("Acme", date(2026, 11, 10)) and seen[-1][1] == date(2026, 11, 11)
    assert len(erp.calls) == 1
    # Not due: nothing claimed for the day, so no run row.
    assert runner.claim(job.id, "2026-11-11", datetime.now(UTC)) == "run"


@pytest.mark.parametrize("answer", [RuntimeError(CANARY), "yes", 1])
def test_a_runs_on_that_fails_or_isnt_true_keeps_the_job_from_starting(
    register: Any, erp: FakeErpnext, answer: Any, caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    def runs_on(settings: Mapping[str, Any], day: date) -> Any:
        if isinstance(answer, Exception):
            raise answer
        return answer

    register(_calendar_job(runs_on=runs_on))
    with recording(caplog, "openexecutive"):
        assert _tick(datetime(2026, 11, 10, 7, 0, tzinfo=UTC)) == 0
    assert erp.calls == []
    assert CANARY not in caplog.text


def test_runs_on_must_be_callable() -> None:
    with pytest.raises(ValueError, match="runs_on"):
        _calendar_job(runs_on="every day")


# --------------------------------------------------------------------------- #
# A single read that may fail on its own
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("refusal", [
    json.dumps({"error": f"Permission denied for {CANARY}"}),
    f"Tool error: {CANARY}",
])
def test_a_may_fail_read_refused_is_not_read_and_the_run_goes_on(
    register: Any, erp: FakeErpnext, triage: list[Any], refusal: str,  # noqa: F811
) -> None:
    job = register(_calendar_job(may_fail=True, second_read=True))
    erp.replies.extend([refusal, _ar(("Acme Retail", 10.0), acting=_acting(job.id))])
    outcome = _run(job)
    assert (outcome.status, outcome.unread, outcome.cards) == ("done", 1, 1)
    [alert] = _alerts()
    assert "Read 1: not read" in alert.body and "Read 2: 10.00" in alert.body
    assert CANARY not in alert.body
    row = _run_row(job.id)
    assert row["status"] == "done"
    assert row["reads"] == [
        {"read": "the first report", "unread": True}, {"read": "the second report", "as_job_user": True},
    ]


def test_a_may_fail_read_that_times_out_is_not_read(
    register: Any, erp: FakeErpnext, triage: list[Any],  # noqa: F811
) -> None:
    job = register(_calendar_job(may_fail=True))
    erp.session.call_tool = AsyncMock(side_effect=TimeoutError())
    outcome = _run(job)
    assert (outcome.status, outcome.unread) == ("done", 1)
    [alert] = _alerts()
    assert "Read 1: not read" in alert.body


def test_a_read_not_read_says_nothing_about_who_the_reads_ran_as() -> None:
    from openexecutive.watch import cards

    job = _calendar_job(may_fail=True, second_read=True)
    row = SimpleNamespace(reviewer_person_id=None)
    # One read as the job's user and one refused: past the job-user check
    # (then held for want of a reviewer), not "not_job_user".
    assert cards.route(job, row, [
        {"read": "the first report", "unread": True}, {"read": "the second report", "as_job_user": True},
    ]).reason == "no_reviewer"
    # A read made as someone else still keeps it from the reviewer.
    assert cards.route(job, row, [
        {"read": "the first report", "unread": True}, {"read": "the second report", "as_job_user": False},
    ]).reason == "not_job_user"
    # Nothing read at all: the principal's, as with no reads.
    assert cards.route(job, row, [{"read": "the first report", "unread": True}]).reason == "no_reads"


def test_a_read_that_may_not_fail_still_ends_the_run(
    register: Any, erp: FakeErpnext, triage: list[Any],  # noqa: F811
) -> None:
    job = register(_calendar_job(second_read=True))
    erp.replies.append(json.dumps({"error": "Permission denied"}))
    outcome = _run(job)
    assert (outcome.status, outcome.error) == ("failed", "read_refused")
    assert len(erp.calls) == 1


def test_a_may_fail_read_still_ends_the_run_on_its_shape_or_another_jobs_answer(
    register: Any, erp: FakeErpnext, roster: SimpleNamespace, triage: list[Any],  # noqa: F811
) -> None:
    job = register(_calendar_job(may_fail=True, second_read=True))
    erp.replies.append(_ar(("Acme Retail", 10.0), acting={**_acting(job.id), "job": "collections"}))
    assert _run(job).error == "acting_job_mismatch"
    job = register(_calendar_job("calendar-b", may_fail=True))
    _enable(job, roster.principal, settings={"company": "", "limit": 200})
    calls = len(erp.calls)
    assert _run(job).error == "read_shape"
    assert len(erp.calls) == calls


# --------------------------------------------------------------------------- #
# updates_notice and a finding's own subject
# --------------------------------------------------------------------------- #


def test_an_updating_notice_is_rewritten_in_place_only_when_it_changed(
    register: Any, erp: FakeErpnext, triage: list[Any],  # noqa: F811
) -> None:
    job = register(_calendar_job(updates=True, subject="Close of October: 1 of 2 clear", once=True))
    erp.replies.append(_ar(("Acme Retail", 10.0), acting=_acting(job.id)))
    assert _run(job).cards == 1
    [first] = _alerts()
    assert first.headline == "Close of October: 1 of 2 clear"
    alert_store.set_status(first.id, "read")
    # The next day, the same figures: no change, so it stays as it was (the
    # day in its preamble alone isn't a change), and the told record of a
    # once job doesn't hold it back.
    erp.replies.append(_ar(("Acme Retail", 10.0), acting=_acting(job.id)))
    same = _run(job, "2026-11-11")
    assert (same.cards, same.updated, same.repeats) == (0, 0, 1)
    [kept] = _alerts()
    assert (kept.id, kept.status, kept.body) == (first.id, "read", first.body)
    # Changed figures: rewritten in place and unread again, still one notice.
    erp.replies.append(_ar(("Acme Retail", 25.0), acting=_acting(job.id)))
    changed = _run(job, "2026-11-12")
    assert (changed.cards, changed.updated) == (0, 1)
    [updated] = _alerts()
    assert updated.id == first.id and updated.status == "unread"
    assert "Read 1: 25.00" in updated.body and "2026-11-12" in updated.body
    assert _run_row(job.id, "2026-11-12")["status"] == "done"
    # Dealt with: the next change is a new notice.
    alert_store.set_status(first.id, "dismissed")
    erp.replies.append(_ar(("Acme Retail", 30.0), acting=_acting(job.id)))
    assert _run(job, "2026-11-13").cards == 1
    assert len(_alerts()) == 2


def test_without_updates_notice_an_open_notice_is_a_repeat(
    register: Any, erp: FakeErpnext, triage: list[Any],  # noqa: F811
) -> None:
    job = register(_calendar_job())
    erp.replies.extend([_ar(("Acme Retail", 10.0), acting=_acting(job.id)),
                        _ar(("Acme Retail", 25.0), acting=_acting(job.id))])
    _run(job)
    second = _run(job, "2026-11-11")
    assert (second.cards, second.updated, second.repeats) == (0, 0, 1)
    [alert] = _alerts()
    assert "Read 1: 10.00" in alert.body


def test_a_findings_subject_is_cleaned_and_capped(
    register: Any, erp: FakeErpnext, triage: list[Any],  # noqa: F811
) -> None:
    job = register(_calendar_job(subject="Close of **October**\nsee https://evil.example " + "x" * 300))
    erp.replies.append(_ar(("Acme Retail", 10.0), acting=_acting(job.id)))
    _run(job)
    [alert] = _alerts()
    assert "\n" not in alert.headline and "evil.example" not in alert.headline and "*" not in alert.headline
    assert len(alert.headline) <= runner.MAX_SUBJECT_CHARS
    # No subject of its own: the job's title and the count, as before.
    plain = register(_calendar_job("calendar-plain"))
    erp.replies.append(_ar(("Acme Retail", 10.0), acting=_acting(plain.id)))
    _run(plain)
    assert any(a.headline == f"Calendar check: 1 found on {DAY}" for a in _alerts())
