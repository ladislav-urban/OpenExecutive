"""The watch jobs' settings, for the principal alone (``openexecutive.watch``).

Routes:
  GET /watch/jobs       — every job defined in code, with its row: on or off,
                          its settings, who configured it and when, its
                          reviewer, its last run, and the operator's master
                          switch (``ERPNEXT_WATCH_JOBS_ENABLED``).
  PUT /watch/jobs/{id}  — turn a job on or off, change its settings (exactly
                          the job's keys and types, within bounds, else 400)
                          or its reviewer (an active teammate, never the
                          principal or a demo person, else 400). Records who
                          configured it and writes a ``watch_job_configured``
                          audit row.

The reviewer gets the job's notices and its draft cards
(``watch.cards.route``); ``REVIEWER_NOTE`` says so, for Settings to show.
Whoever configures a job never approves its cards, so its reviewer is
someone else.

Both need the API to know the caller is the principal
(``delegation.verified.caller_refusal``: a signed principal, or local
login's operator): 409 ``caller_signing_required`` when it can't tell,
403 ``not_yours`` when it's someone else. A job is defined in code only, so
there is no route that adds one, and no chat action calls these
(``app_control``): a job is turned on by a tap, never a turn.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from openexecutive.api import caller as api_caller

router = APIRouter()
logger = logging.getLogger(__name__)


def _refuse(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _principal(request: Request) -> Any:
    """The principal, when the API knows the caller is them; else 409/403."""
    from openexecutive.delegation.verified import NOT_YOURS, caller_refusal
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    email = (getattr(principal, "email", None) or "").strip().lower()
    if principal is None or principal.id is None or "@" not in email:
        raise _refuse(409, "no_principal", "Watch jobs need the account owner's People entry, with their email.")
    refused = caller_refusal(api_caller.caller(request), email)
    if refused == NOT_YOURS:
        raise _refuse(403, "not_yours", "Only the account owner can see and change watch jobs.")
    if refused is not None:
        raise _refuse(
            409, "caller_signing_required",
            "Watch jobs need signed sign-ins on this server, so that nobody else can turn one on.",
        )
    return principal


class WatchRunOut(BaseModel):
    day: str
    # Its slot that day ("HH:MM"); "" for a job that runs once a day.
    slot: str = ""
    status: str
    cards: int
    error: str


# What Settings shows beside a job's reviewer, in its own words.
REVIEWER_NOTE = (
    "The reviewer sees this job's notices, with the figures ERPNext read as the job's own user, and "
    "approves its draft cards. It can't be you: whoever sets up a job never approves its cards. "
    "Without a reviewer, you get the notices only."
)


class WatchJobOut(BaseModel):
    id: str
    title: str
    enabled: bool
    settings: dict[str, Any]
    default_settings: dict[str, Any]
    configured_by_person_id: int | None
    configured_at: str | None
    reviewer_person_id: int | None
    # "HH:MM" in the company's time zone (its first slot, for a job that
    # runs several times a day).
    runs_at: str
    # Every time it runs each day ("HH:MM", company time), and on which days
    # (Monday 0 to Sunday 6).
    slots: list[str] = []
    weekdays: list[int] = []
    card_cap_day: int
    last_run: WatchRunOut | None = None
    # What Settings says beside the reviewer (REVIEWER_NOTE).
    reviewer_note: str = REVIEWER_NOTE


class WatchJobsOut(BaseModel):
    # ERPNEXT_WATCH_JOBS_ENABLED: off, no job runs whatever its row says.
    master_switch: bool
    jobs: list[WatchJobOut]


class WatchJobUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    settings: dict[str, Any] | None = None
    # Who gets the job's notices and cards (watch.cards.route); never the
    # principal, who configures every job.
    reviewer_person_id: int | None = None


def _last_run(job_id: str) -> WatchRunOut | None:
    from openexecutive.watch.settings import RUNS_TABLE, connect

    conn = connect()
    try:
        row = conn.execute(
            f"SELECT day, slot, status, cards, error FROM {RUNS_TABLE} "  # noqa: S608
            "WHERE job = ? AND status != 'skipped' ORDER BY day DESC, slot DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    return WatchRunOut(
        day=row[0], slot=row[1] or "", status=row[2], cards=int(row[3] or 0), error=row[4] or "",
    ) if row else None


def _job_out(job: Any) -> WatchJobOut:
    from openexecutive.watch.settings import effective_settings, get_row

    row = get_row(job.id)
    return WatchJobOut(
        id=job.id,
        title=job.title,
        enabled=row.enabled,
        settings=effective_settings(job, row),
        default_settings=dict(job.default_settings),
        configured_by_person_id=row.configured_by_person_id,
        configured_at=row.configured_at,
        reviewer_person_id=row.reviewer_person_id,
        runs_at=job.schedule.strftime("%H:%M"),
        slots=[key or at.strftime("%H:%M") for at, key in job.slot_keys()],
        weekdays=sorted(job.weekdays),
        card_cap_day=job.card_cap_day,
        last_run=_last_run(job.id),
    )


@router.get("/watch/jobs", response_model=WatchJobsOut)
def list_watch_jobs(request: Request) -> WatchJobsOut:
    from openexecutive.watch import registry
    from openexecutive.watch.settings import master_on

    _principal(request)
    return WatchJobsOut(
        master_switch=master_on(),
        jobs=[_job_out(job) for job in sorted(registry.JOBS.values(), key=lambda j: j.id)],
    )


def _check_reviewer(person_id: int) -> None:
    from openexecutive.people.store import get_person

    person = get_person(person_id)
    if person is None or not person.active or person.kind != "team":
        raise _refuse(400, "bad_reviewer", "The reviewer must be an active person on the team.")
    # Separation of duties: the principal configures every job, so they
    # never review one; a demo person approves nothing.
    if person.is_principal:
        raise _refuse(400, "bad_reviewer", "You set up the job, so someone else reviews it.")
    if person.demo:
        raise _refuse(400, "bad_reviewer", "A demo account can't review a job.")


@router.put("/watch/jobs/{job_id}", response_model=WatchJobOut)
def update_watch_job(job_id: str, body: WatchJobUpdate, request: Request) -> WatchJobOut:
    from openexecutive.audit import log_event
    from openexecutive.watch import registry
    from openexecutive.watch.settings import (
        ASSIGN_TO_SAVE,
        assign_to_problem,
        effective_settings,
        get_row,
        save,
    )

    principal = _principal(request)
    job = registry.get(job_id)
    if job is None:
        raise _refuse(404, "unknown_job", "There's no such watch job.")
    settings: dict[str, Any] | None = None
    if body.settings is not None:
        try:
            settings = registry.check_settings(job, body.settings)
        except registry.SettingsError as bad:
            raise _refuse(400, "bad_settings", str(bad)) from None
    # The settings the job will run with (the body's, else the stored ones):
    # its tasks never go to whoever sets it up, nor to a demo account. Checked
    # whenever settings are sent, and on every PUT that leaves the job on;
    # turning a job off without new settings is never refused, though its
    # stored assign_to went bad since.
    row = get_row(job.id)
    stays_on = body.enabled if body.enabled is not None else row.enabled
    effective = settings if settings is not None else effective_settings(job, row)
    checked = settings is not None or stays_on
    problem = assign_to_problem(effective.get("assign_to"), int(principal.id)) if checked else ""
    if problem:
        raise _refuse(400, "bad_settings", ASSIGN_TO_SAVE[problem])
    set_reviewer = "reviewer_person_id" in body.model_fields_set
    if set_reviewer and body.reviewer_person_id is not None:
        _check_reviewer(body.reviewer_person_id)
    before, after = save(
        job,
        enabled=body.enabled,
        settings=settings,
        reviewer_person_id=body.reviewer_person_id,
        set_reviewer=set_reviewer,
        configured_by_person_id=int(principal.id),
        now=datetime.now(UTC),
    )
    old, new = effective_settings(job, before), effective_settings(job, after)
    changed = sorted(key for key in new if old.get(key) != new.get(key))
    log_event(
        "watch_job_configured",
        f"Watch job {job.id} configured: {'on' if after.enabled else 'off'}",
        actor=api_caller.actor(request),
        details={
            "job": job.id,
            "enabled_before": before.enabled,
            "enabled_after": after.enabled,
            "settings_changed": changed,
            "reviewer_changed": before.reviewer_person_id != after.reviewer_person_id,
            "person_id": principal.id,
        },
        private=True,
    )
    return _job_out(job)
