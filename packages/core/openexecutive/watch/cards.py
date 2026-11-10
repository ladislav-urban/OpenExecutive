"""Who a watch job's run goes to, and the draft cards it leaves.

**Routing** (``route``). A run's notices and draft cards go to the job's
reviewer (``watch_jobs.reviewer_person_id``, set by the principal in
Settings, read here from the database and never from a model or a read)
only when all of these hold:

1. every read the run made reported, in oe_connector's own top-level
   ``acting``, that it ran as the job's own ERP user ``oe-watch-<job>@<site>``,
   the same user on every read, and at least one read was made (a skipped
   read counts neither way; ``runner._as_job_user``);
2. the reviewer's People row is on the team, active, not a demo person, not
   the principal, not whoever configured the job, and has an email;
3. teammates' drafts are on (``ERPNEXT_DRAFTS_TEAMMATES``) and sign-ins are
   signed (``api.caller.signing_on``), so the reviewer's approval is theirs.

Otherwise the principal gets the run's notices only, and the run records why
(``Route.reason``, ``routed_principal``). No job card ever goes to the
principal: they configure every job, and whoever configured a job never
approves its cards (separation of duties).

**Cards** (``leave_drafts``), only once ``route`` named the reviewer. For each
``DraftSpec``, in the job's order: none while the same finding's card waits;
at most ``card_cap_day`` a day, ``erpnext_drafts.JOB_OPEN_MAX`` waiting for
the job, and never the reviewer past ``OPEN_CARDS_MAX`` less the headroom.
The fields are the job's ``check_fields`` output; ERPNext checks them as the
reviewer (``erpnext_caller.draft_check_for_job``, audited ``watch_job_check``
with a reason code, never ERPNext's text); the check must echo the operation,
DocType and what the job expects (the invoice, the customer), and give a
fingerprint; then ``erpnext_drafts.leave_job_card`` leaves the card. A check
ERPNext refuses leaves no card, and the notice says how many in code's words.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from openexecutive.watch import registry

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_S = 60.0
# A check that didn't answer is tried once more (within the marker's cap).
CHECK_TRIES = 2

# Why a run went to the principal, by code, as its notice says it when the
# job had cards to propose. ``{noun}`` is the job's card noun.
ROUTE_LINES: dict[str, str] = {
    "no_reads": "No read was made, so no {noun} were prepared.",
    "not_job_user": (
        "ERPNext didn't run every read as the job's own user, so no {noun} were prepared. They go only "
        "to a named reviewer of a job ERPNext runs as its own user."
    ),
    "no_reviewer": "Name a reviewer in Settings → Watch jobs to get the {noun}.",
    "reviewer_unavailable": (
        "The job's reviewer can't receive them (no longer active on the team, or without an email), so "
        "no {noun} were prepared. Name another in Settings → Watch jobs."
    ),
    "reviewer_is_configurer": (
        "The job's reviewer also set it up, and can't approve its cards, so no {noun} were prepared. "
        "Name another reviewer in Settings → Watch jobs."
    ),
    "teammates_off": "Teammates' drafts are off on this server, so no {noun} were prepared for the reviewer.",
    "signing_off": "Signed sign-ins are off on this server, so no {noun} were prepared for the reviewer.",
    "lookup_failed": "The reviewer couldn't be looked up, so no {noun} were prepared.",
}
# What leaves no card, by code: our words, never ERPNext's.
CHECK_CODES = frozenset({
    "check_permission", "check_refused", "check_failed", "check_mismatch", "check_no_fingerprint",
    "check_fields", "job-check-in-turn", "job-check-with-other-marker", "job-check-needs-keys",
    "key-malformed", "job-check-unknown-job", "job-check-job-off", "job-check-arguments",
    "job-check-reviewer", "job-check-cap",
})


@dataclass(frozen=True)
class Route:
    """Who a run goes to: the reviewer (``reviewer`` set, ``reason`` "") or
    the principal (``reason`` a code in ``ROUTE_LINES``)."""

    person_id: int | None
    reviewer: Any | None = None
    reason: str = ""


def _principal_id() -> int | None:
    from openexecutive.people.store import find_principal_person

    try:
        principal = find_principal_person()
    except Exception:
        return None
    return principal.id if principal is not None else None


def route(job: registry.WatchJob, row: Any, reads: Sequence[Mapping[str, Any]]) -> Route:
    """Who this run's notices and cards go to (see the module's rules)."""
    from openexecutive.api.caller import signing_on
    from openexecutive.integrations.erpnext_drafts import teammates_on
    from openexecutive.people.store import get_person

    principal = _principal_id()
    made = [r for r in reads if not r.get("skipped")]
    if not made:
        return Route(principal, reason="no_reads")
    if not all(r.get("as_job_user") is True for r in made):
        return Route(principal, reason="not_job_user")
    reviewer_id = getattr(row, "reviewer_person_id", None)
    if not isinstance(reviewer_id, int) or isinstance(reviewer_id, bool):
        return Route(principal, reason="no_reviewer")
    try:
        person = get_person(reviewer_id)
    except Exception:
        logger.warning("watch: job %s's reviewer couldn't be read", job.id)
        return Route(principal, reason="lookup_failed")
    if (
        person is None or person.id is None or not person.active or person.kind != "team" or person.demo
        or person.is_principal or "@" not in (person.email or "")
    ):
        return Route(principal, reason="reviewer_unavailable")
    if person.id == getattr(row, "configured_by_person_id", None):
        return Route(principal, reason="reviewer_is_configurer")
    if not teammates_on():
        return Route(principal, reason="teammates_off")
    if not signing_on():
        return Route(principal, reason="signing_off")
    return Route(person.id, reviewer=person)


@dataclass
class DraftOutcome:
    left: int = 0
    repeats: int = 0
    capped: int = 0
    # Checks made, refused by ERPNext (any reason), and of those for the
    # reviewer's ERPNext permissions.
    checks: int = 0
    check_refused: int = 0
    permission_refused: int = 0
    # Findings not proposed: the run went to the principal, or drafts are off.
    withheld: int = 0
    lines: list[str] = field(default_factory=list)

    @property
    def all_refused_for_permissions(self) -> bool:
        return self.checks > 0 and self.permission_refused == self.checks


def _left_today(job_id: str, day: str) -> int:
    from openexecutive.integrations import erpnext_drafts as drafts

    return sum(1 for c in drafts.job_cards(job_id, open_only=False) if drafts._payload(c).get("run_day") == day)


def _audit_check(job: registry.WatchJob, day: str, operation: str, person_id: int, code: str) -> None:
    from openexecutive.audit import log_event

    try:
        log_event(
            "watch_job_check",
            f"Watch job {job.id} checked a card as person {person_id}: {code}",
            actor=f"watch:{job.id}",
            details={"job": job.id, "day": day, "operation": operation, "person_id": person_id, "result": code},
            private=True,
        )
    except Exception:
        logger.warning("watch: couldn't write job %s's check audit row", job.id)


def _permission(said: str) -> bool:
    """Whether ERPNext refused the check for the asker's own permissions
    (read here only to count it; never stored or shown)."""
    text = said.lower()
    return "permission" in text or "not permitted" in text or "not allowed" in text


async def _check(
    job: registry.WatchJob, day: str, spec: registry.DraftSpec, fields: dict[str, Any], reviewer: Any, email: str,
) -> tuple[str, dict[str, Any]]:
    """ERPNext's check of one card as the reviewer: ("ok", the result) or a
    reason code with {}."""
    from openexecutive.integrations import erpnext_drafts as drafts
    from openexecutive.orchestrator import erpnext_caller

    arguments: dict[str, Any] = {"operation": spec.operation, "doctype": spec.doctype, "fields": fields}
    raw: Any = None
    for _try in range(CHECK_TRIES):
        try:
            with erpnext_caller.draft_check_for_job(job.id, arguments, email, person_id=reviewer.id, day=day):
                raw = await asyncio.wait_for(drafts._call(erpnext_caller.DRAFT_CHECK_TOOL, arguments), CHECK_TIMEOUT_S)
            break
        except erpnext_caller.JobCheckRefused as refused:
            return refused.reason if refused.reason in CHECK_CODES else "check_refused", {}
        except Exception:
            # Its text may quote ERPNext: only that it failed is kept.
            logger.warning("watch: job %s's check didn't answer", job.id)
            raw = None
    if raw is None:
        return "check_failed", {}
    checked = drafts._result(raw)
    if checked.get("ok") is not True:
        return ("check_permission" if _permission(drafts._refusal_text(raw)) else "check_refused"), {}
    summary = checked.get("summary")
    if (
        checked.get("doctype") != spec.doctype or checked.get("operation", spec.operation) != spec.operation
        or not isinstance(summary, dict) or any(summary.get(k) != v for k, v in spec.expect.items())
    ):
        return "check_mismatch", {}
    if drafts.fingerprint_of(checked) is None:
        return "check_no_fingerprint", {}
    return "ok", checked


async def leave_drafts(
    job: registry.WatchJob, day: str, row: Any, run_route: Route, specs: Sequence[registry.DraftSpec],
    reasons: Mapping[str, str],
) -> DraftOutcome:
    """Check and leave the run's draft cards for the reviewer, within the
    caps; nothing when the run went to the principal (only after ``route``
    named the reviewer is any check made)."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.integrations import erpnext_drafts as drafts
    from openexecutive.watch.runner import MAX_REASON_CHARS, clean, dedup_key

    out = DraftOutcome()
    if not specs:
        return out
    noun = job.card_noun
    reviewer = run_route.reviewer
    if reviewer is None or job.check_fields is None:
        out.withheld = len(specs)
        out.lines.append(ROUTE_LINES.get(run_route.reason, ROUTE_LINES["no_reviewer"]).format(noun=noun))
        return out
    doctypes = {s.doctype for s in specs if s.operation == "create"}
    if not drafts.drafts_on() or doctypes - set(drafts.enabled_doctypes()):
        out.withheld = len(specs)
        out.lines.append(f"Drafts of this kind are off on this server, so no {noun} were prepared.")
        return out
    email = normalize_email(reviewer.email or "")
    configured_by = getattr(row, "configured_by_person_id", None)
    if not isinstance(configured_by, int) or isinstance(configured_by, bool):
        out.withheld = len(specs)
        out.lines.append(f"Nobody is recorded as having set up the job, so no {noun} were prepared.")
        return out
    budget = max(0, job.card_cap_day - _left_today(job.id, day))
    attempts = 0
    for n, spec in enumerate(specs):
        key = dedup_key(job.id, spec.key)
        if drafts.job_card_waiting(key):
            out.repeats += 1
            continue
        if attempts >= budget or drafts.job_room(job.id, reviewer.id) is not None:
            out.capped = len(specs) - n
            break
        attempts += 1
        fields = job.check_fields(spec.operation, spec.doctype, spec.fields)
        if isinstance(fields, str):
            _audit_check(job, day, spec.operation, reviewer.id, "check_fields")
            continue
        out.checks += 1
        code, checked = await _check(job, day, spec, fields, reviewer, email)
        pinned: dict[str, Any] | str = fields
        if code == "ok":
            pinned = drafts.pin_from_check(spec.doctype, fields, checked.get("summary"))
            again = job.check_fields(spec.operation, spec.doctype, pinned) if isinstance(pinned, dict) else "no"
            if isinstance(pinned, str) or again != pinned:
                code = "check_fields"
        _audit_check(job, day, spec.operation, reviewer.id, code)
        if code != "ok":
            out.check_refused += 1
            if code == "check_permission":
                out.permission_refused += 1
            continue
        if not isinstance(pinned, dict):
            continue
        # Read again after the check, with no await before the card is stored.
        if drafts.job_room(job.id, reviewer.id) is not None:
            out.capped = len(specs) - n
            break
        try:
            _id, new = drafts.leave_job_card(
                job, day, spec.operation, spec.doctype, pinned, checked,
                reviewer=reviewer, configured_by_person_id=configured_by, dedup_key=key,
                why=clean(spec.title, 140), reason=clean(reasons.get(spec.reason_key, ""), MAX_REASON_CHARS),
            )
        except Exception:
            logger.error("watch: job %s couldn't leave a card", job.id)
            continue
        if new:
            out.left += 1
        else:
            out.repeats += 1
    name = clean(getattr(reviewer, "full_name", "") or "", 80) or "the reviewer"
    if out.left:
        out.lines.append(f"{out.left} {noun} are waiting for {name} to approve.")
    if out.permission_refused:
        out.lines.append(
            f"{out.permission_refused} {noun} couldn't be prepared as {name}: their ERPNext access doesn't allow it."
        )
    if out.check_refused > out.permission_refused:
        out.lines.append(f"{out.check_refused - out.permission_refused} {noun} couldn't be prepared: ERPNext didn't accept them.")
    if out.capped:
        out.lines.append(f"{out.capped} more {noun} are due; they come once the waiting ones are handled.")
    return out
