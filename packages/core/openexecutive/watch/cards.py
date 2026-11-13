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

**Cards** (``leave_drafts``), only once ``route`` named the reviewer, and only
of kinds that are on (a DocType listed for drafts or changes; a block only
while ``erpnext_block.unavailable`` says nothing; a bank match only while
``erpnext_match.unavailable`` says nothing). For each ``DraftSpec``, in
the job's order: none while the same finding's card waits or once one was
left that day (for a ``once`` job, ever); at most ``card_cap_day`` a day across the job's slots
(``settings.take_day_cards``), ``erpnext_drafts.JOB_OPEN_MAX`` waiting for
the job, and never the reviewer past ``OPEN_CARDS_MAX`` less the headroom.
The fields are the job's ``check_fields`` output; ERPNext checks them as the
reviewer (``erpnext_caller.draft_check_for_job``, audited ``watch_job_check``
with a reason code, never ERPNext's text); the check must read back the card
(a new draft: the operation, DocType and what the job expects; a change:
``erpnext_changes``' reading, and a draft the reviewer owns or is assigned;
a block: ``erpnext_block``'s; a bank match: ``erpnext_match``'s, with the
reviewer neither the statement's importer nor a voucher's creator), and
give a fingerprint; a bank match must also fit what is left of the job's
``amount_cap_day`` for the day's cards (``settings.take_day_amount``); then
``erpnext_drafts.leave_job_card`` leaves the card. A check ERPNext refuses
leaves no card, and the notice says how many in code's words. A finding no
card was left for is told as its ``DraftSpec.notice``, when the job gave one.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
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
        "The ERP didn't run every read as the job's own user, so no {noun} were prepared. They go only "
        "to a named reviewer of a job the ERP runs as its own user."
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
    "check_currency", "check_not_whole", "check_reviewer_sod", "check_amount_cap",
    "check_fill_currency", "check_fill_total", "check_fill_sod", "check_fill_assignee", "check_fill_collides",
    "check_fill_sender", "check_already_open",
})
# What a run's notice says of the findings no card was left for, by code,
# when the code says more than "ERPNext didn't accept them" (``{n}`` how
# many, ``{noun}`` the job's card noun).
CHECK_LINES: dict[str, str] = {
    "check_currency": "{n} {noun} weren't prepared: not matched, the account is in another currency than the company's.",
    "check_not_whole": "{n} {noun} weren't prepared: the voucher wouldn't match the whole bank line in the ERP.",
    "check_reviewer_sod": (
        "{n} {noun} weren't prepared: they need a different reviewer, since the ERP names the reviewer as the "
        "statement's importer or a voucher's creator."
    ),
    "check_amount_cap": "{n} {noun} weren't prepared: today's matching limit is reached.",
    "check_fill_currency": "{n} {noun} weren't prepared: the invoice is in another currency than the company's.",
    "check_fill_total": (
        "{n} {noun} weren't prepared: the order or receipt wouldn't bill the total on the supplier's invoice."
    ),
    "check_fill_sod": (
        "{n} {noun} weren't prepared: they need a different reviewer, since the ERP names the reviewer as a "
        "creator of the order or receipt they bill."
    ),
    "check_fill_assignee": (
        "{n} {noun} weren't prepared: only the person the mail pull assigned a draft to approves its fill, "
        "and that isn't the reviewer. An ERP operator points the Purchase Invoice assignment rule at the "
        "job's reviewer, or the drafts are filled by hand."
    ),
    "check_fill_collides": "{n} {noun} weren't prepared: the supplier already has an invoice with that number.",
    "check_fill_sender": "{n} {noun} weren't prepared: the ERP can't trust the mail's sender to fill an invoice.",
    "check_already_open": "{n} {noun} weren't prepared: a task for each is already open in the ERP.",
}


# Checks refused because ERPNext keeps the finding from the reviewer: its
# row goes to the principal instead.
KEPT_CODES = frozenset({"check_permission", "check_not_theirs", "check_reviewer_sod", "check_fill_sod"})
# Checks refused for good while the finding stands: a once job holds the
# finding as if carded (``_mark_carded``) rather than check it every run.
HELD_CHECK_CODES = KEPT_CODES | {"check_already_open"}


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
    # The findings no card was left for (none waits either): each is told as
    # its ``notice``, when the job gave one.
    unproposed: list[registry.DraftSpec] = field(default_factory=list)
    # How many findings each ``CHECK_LINES`` code left without a card.
    codes: dict[str, int] = field(default_factory=dict)
    # The findings ERPNext kept from the reviewer (their permissions, or
    # separation of duties): their notices go to the principal instead.
    kept_from_reviewer: set[str] = field(default_factory=set)

    @property
    def all_refused_for_permissions(self) -> bool:
        return self.checks > 0 and self.permission_refused == self.checks


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
    return (
        "permission" in text or "not permitted" in text or "not allowed" in text
        # draft_update's own: a draft that is neither theirs nor assigned to them.
        or "isn't assigned to you" in text or "cannot see it" in text
    )


def _echo_refusal(
    spec: registry.DraftSpec, fields: dict[str, Any], checked: dict[str, Any], email: str = "",
) -> str | None:
    """Why ERPNext's check doesn't read back the card the job built, as a
    reason code, or None. A new draft: the operation, DocType and what the
    job expects in the summary. A change: ``erpnext_changes``' own reading
    (the same document and change, and whose: the reviewer's own draft or one
    assigned to them, else ``check_not_theirs``). A block:
    ``erpnext_block``'s. A bank match: ``erpnext_match``'s, as the reviewer
    (``email``)."""
    summary = checked.get("summary")
    if spec.operation == "block":
        from openexecutive.integrations.erpnext_block import echo_refusal

        return echo_refusal(spec.name, fields, checked, dict(spec.expect))
    if spec.operation == "match":
        from openexecutive.integrations import erpnext_match

        return erpnext_match.echo_refusal(spec.name, fields, checked, dict(spec.expect), email)
    if spec.operation == "fill":
        from openexecutive.integrations import erpnext_fill

        return erpnext_fill.echo_refusal(spec.name, fields, checked, dict(spec.expect), email)
    if checked.get("doctype") != spec.doctype or checked.get("operation", "create") != spec.operation:
        return "check_mismatch"
    if not isinstance(summary, dict) or any(summary.get(k) != v for k, v in spec.expect.items()):
        return "check_mismatch"
    if spec.operation in ("update", "comment"):
        from openexecutive.integrations.erpnext_changes import _WHOSE, summary_refusal

        if checked.get("name") != spec.name:
            return "check_mismatch"
        if spec.operation == "update" and summary.get("whose") not in _WHOSE:
            return "check_not_theirs"
        if summary_refusal(spec.operation, spec.doctype, spec.name, fields, checked) is not None:
            return "check_mismatch"
    return None


async def _check(
    job: registry.WatchJob, day: str, spec: registry.DraftSpec, fields: dict[str, Any], reviewer: Any, email: str,
) -> tuple[str, dict[str, Any]]:
    """ERPNext's check of one card as the reviewer: ("ok", the result) or a
    reason code with {}."""
    from openexecutive.integrations import erpnext_drafts as drafts
    from openexecutive.orchestrator import erpnext_caller

    arguments: dict[str, Any] = {"operation": spec.operation, "doctype": spec.doctype, "fields": fields}
    if spec.operation != "create":
        arguments["name"] = spec.name
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
        said = drafts._refusal_text(raw)
        if spec.doctype == "ToDo" and spec.operation == "create" and drafts.already_open(said):
            # oe_connector's duplicate-ToDo guard: one is already open.
            return "check_already_open", {}
        return ("check_permission" if _permission(said) else "check_refused"), {}
    why = _echo_refusal(spec, fields, checked, email)
    if why is not None:
        return why, {}
    if drafts.fingerprint_of(checked) is None:
        return "check_no_fingerprint", {}
    return "ok", checked


async def _kinds_off(job: registry.WatchJob, row: Any, specs: Sequence[registry.DraftSpec]) -> set[str]:
    """The kinds of card (operation, DocType) that are off on this server
    now: a new draft's DocType not listed, a change's not listed for updates,
    a block while the job's setting, ``ERPNEXT_BLOCK_INVOICE`` or the pin
    doesn't hold, a bank match while ``ERPNEXT_BANK_MATCH`` or the pin
    doesn't."""
    from openexecutive.integrations import erpnext_changes
    from openexecutive.integrations import erpnext_drafts as drafts

    off: set[str] = set()
    for operation, doctype in {(s.operation, s.doctype) for s in specs}:
        if operation == "create":
            on = doctype in drafts.enabled_doctypes()
        elif operation == "block":
            from openexecutive.integrations import erpnext_block

            on = job.id == erpnext_block.JOB_ID and await erpnext_block.unavailable(job, row) is None
        elif operation == "match":
            from openexecutive.integrations import erpnext_match

            on = (
                job.id == erpnext_match.JOB_ID and doctype == erpnext_match.DOCTYPE
                and await erpnext_match.unavailable() is None
            )
        elif operation == "fill":
            from openexecutive.integrations import erpnext_fill

            on = (
                job.id == erpnext_fill.JOB_ID and doctype == erpnext_fill.DOCTYPE
                and await erpnext_fill.unavailable() is None
            )
        else:
            on = doctype in erpnext_changes.enabled(operation)
        if not on:
            off.add(f"{operation}:{doctype}")
    return off


async def leave_drafts(
    job: registry.WatchJob, day: str, row: Any, run_route: Route, specs: Sequence[registry.DraftSpec],
    reasons: Mapping[str, str],
) -> DraftOutcome:
    """Check and leave the run's draft cards for the reviewer, within the
    caps; nothing when the run went to the principal (only after ``route``
    named the reviewer is any check made). The day's cap is counted across
    the job's slots (``settings.take_day_cards``): a card is taken from it
    before its check, and given back when none is left."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.integrations import erpnext_drafts as drafts
    from openexecutive.watch.runner import clean, dedup_key
    from openexecutive.watch.settings import take_day_cards

    out = DraftOutcome()
    if not specs:
        return out
    noun = job.card_noun
    reviewer = run_route.reviewer
    if reviewer is None or job.check_fields is None:
        out.withheld = len(specs)
        out.unproposed = list(specs)
        out.lines.append(ROUTE_LINES.get(run_route.reason, ROUTE_LINES["no_reviewer"]).format(noun=noun))
        return out
    if not drafts.drafts_on():
        out.withheld = len(specs)
        out.unproposed = list(specs)
        out.lines.append(f"Drafts of this kind are off on this server, so no {noun} were prepared.")
        return out
    off = await _kinds_off(job, row, specs)
    if off:
        held = [s for s in specs if f"{s.operation}:{s.doctype}" in off]
        out.withheld = len(held)
        out.unproposed.extend(held)
        specs = [s for s in specs if f"{s.operation}:{s.doctype}" not in off]
        # The job's own notices say what to do instead, when it gave them.
        if any(s.notice is None for s in held):
            out.lines.append(f"Drafts of this kind are off on this server, so no {noun} were prepared.")
        if not specs:
            return out
    email = normalize_email(reviewer.email or "")
    configured_by = getattr(row, "configured_by_person_id", None)
    if not isinstance(configured_by, int) or isinstance(configured_by, bool):
        out.withheld = len(specs)
        out.unproposed.extend(specs)
        out.lines.append(f"Nobody is recorded as having set up the job, so no {noun} were prepared.")
        return out
    # A finding gets one card a day, whatever became of it: a job that runs at
    # several slots never proposes a card again the hour after it was dismissed.
    if job.once:
        left_today, kept = _once_held(job, reviewer.id, specs)
        # A finding held because ERPNext kept it from the reviewer (on an
        # earlier run) is still kept: its row comes off the reviewer's
        # notice, and its notice is offered to the principal until one of
        # theirs has told it (the runner skips a finding told before).
        for spec in specs:
            if dedup_key(job.id, spec.key) in kept:
                out.kept_from_reviewer.add(spec.key)
                out.unproposed.append(spec)
    else:
        left_today = {
            drafts._payload(c).get("dedup_key") for c in drafts.job_cards(job.id, open_only=False)
            if drafts._payload(c).get("run_day") == day
        }
    for n, spec in enumerate(specs):
        key = dedup_key(job.id, spec.key)
        if key in left_today or drafts.job_card_waiting(key):
            out.repeats += 1
            continue
        if drafts.job_room(job.id, reviewer.id) is not None or take_day_cards(job.id, day, 1, job.card_cap_day) == 0:
            out.capped = len(specs) - n
            _unpropose(out, (s for s in specs[n:] if not drafts.job_card_waiting(dedup_key(job.id, s.key))))
            break
        left = False
        try:
            left = await _leave_one(job, day, spec, key, reviewer, email, configured_by, reasons, out)
        finally:
            if not left:
                # Not left after all: its place in the day's cap goes back.
                take_day_cards(job.id, day, -1, job.card_cap_day)
        if out.capped:
            # The reviewer's cards filled up while this one was checked.
            out.capped = len(specs) - n
            _unpropose(out, (s for s in specs[n + 1:] if not drafts.job_card_waiting(dedup_key(job.id, s.key))))
            break
    name = clean(getattr(reviewer, "full_name", "") or "", 80) or "the reviewer"
    if out.left:
        out.lines.append(f"{out.left} {noun} are waiting for {name} to approve.")
    if out.permission_refused:
        out.lines.append(
            f"{out.permission_refused} {noun} couldn't be prepared as {name}: their ERP access doesn't allow it."
        )
    if out.check_refused > out.permission_refused:
        out.lines.append(f"{out.check_refused - out.permission_refused} {noun} couldn't be prepared: the ERP didn't accept them.")
    for code, count in sorted(out.codes.items()):
        if code in CHECK_LINES:
            out.lines.append(CHECK_LINES[code].format(n=count, noun=noun))
    if out.capped:
        out.lines.append(f"{out.capped} more {noun} are due; they come once the waiting ones are handled.")
    return out


# A once job's card that keeps its finding from a second card: one a person
# approved or dismissed, or one whose approval may have made the draft.
_HELD_STATUSES = frozenset({
    "approved_unchanged", "approved_with_edit", "executed", "reversed", "rejected",
})


def _unpropose(out: DraftOutcome, specs: Iterable[registry.DraftSpec]) -> None:
    """Add ``specs`` to the findings no card was left for, each once: a kept
    finding is already there before the loop, and a duplicate would count
    twice in the notice's subject."""
    have = {s.key for s in out.unproposed}
    for spec in specs:
        if spec.key not in have:
            have.add(spec.key)
            out.unproposed.append(spec)


def _once_held(
    job: registry.WatchJob, reviewer_id: int, specs: Sequence[registry.DraftSpec],
) -> tuple[set[Any], set[str]]:
    """The dedupe keys of a once job's findings that get no new card, and
    those of them held because ERPNext kept them from the reviewer
    (``kept_key``). No new card for: a
    card still waiting for the current reviewer and still the job's own
    (``check_fields`` builds it unchanged), one being approved, one whose
    approval was tried at all or went unanswered (its final ``closed_code``
    ``no_answer``: ERPNext may have made it), or one a person approved or
    dismissed. A card that expired, or that code closed or would refuse (the
    job's assignee or reviewer changed, a configurer refusal), doesn't hold
    its finding: a waiting one is closed here, its ``card:`` record dropped,
    and the finding gets a new card while it's due. A finding the ledger's
    listing no longer shows is held by its ``card:`` record. A finding whose
    check ERPNext refused for good is held by its ``held:`` or ``kept:``
    record whatever older card of the same key the ledger shows: only
    ``card:`` records (a card left) are dropped with a card that doesn't
    hold."""
    from openexecutive.integrations import erpnext_drafts as drafts
    from openexecutive.memory.decision_ledger import (
        STATUS_EXECUTING,
        STATUS_FAILED,
        STATUS_PROPOSED,
        close_externally,
    )
    from openexecutive.watch.runner import dedup_key
    from openexecutive.watch.settings import forget_told, told

    def tried(card: Any, payload: Mapping[str, Any], final: Mapping[str, Any]) -> bool:
        # An approval was tried: ERPNext may have made the draft.
        attempts = [p.get("attempts") for p in (payload, final)]
        return payload.get("tried") is True or final.get("tried") is True or any(
            isinstance(a, int) and not isinstance(a, bool) and a > 0 for a in attempts
        )

    assert job.check_fields is not None
    wanted = {dedup_key(job.id, s.key) for s in specs}
    held: set[Any] = set()
    seen: set[str] = set()
    for card in drafts.job_cards(job.id, open_only=False):
        payload = drafts._payload(card)
        key = payload.get("dedup_key")
        if key not in wanted:
            continue
        seen.add(key)
        fields = payload.get("fields")
        final = _final(card)
        # A tried card holds whatever became of it (a waiting one _tidy closed
        # as expired included): its try may have made the draft. A failed one
        # is told apart by its final closed_code below, since ERPNext answered.
        if card.status == STATUS_EXECUTING or (card.status != STATUS_FAILED and tried(card, payload, final)):
            held.add(key)
        elif card.status == STATUS_PROPOSED:
            target = payload.get("name")
            still = (
                drafts.card_live(card) and card.approver_person_id == reviewer_id and isinstance(fields, dict)
                and job.check_fields(
                    str(payload.get("operation", "create")), str(payload.get("doctype")), fields,
                    target if isinstance(target, str) else "",
                ) == fields
            )
            # Closed from proposed only: one that turned executing meanwhile
            # is an approval in flight, and holds its finding.
            if still or not close_externally(
                card.id, reason="expired" if not drafts.card_live(card) else "job_changed", proposed_only=True,
            ):
                held.add(key)
        elif card.status in _HELD_STATUSES or (
            # Unanswered (ERPNext may have made it), or a ToDo already open
            # in the ERP (that open one is the follow-up).
            card.status == STATUS_FAILED
            and final.get("closed_code") in (drafts.CLOSED_NO_ANSWER, drafts.CLOSED_ALREADY_OPEN)
        ):
            held.add(key)
    stale = seen - held
    if stale:
        forget_told(job.id, [carded_key(k) for k in stale])
    unseen = wanted - seen
    asked = [carded_key(k) for k in unseen] + [held_key(k) for k in wanted] + [kept_key(k) for k in wanted]
    recorded = told(job.id, asked) if asked else set()
    held |= {k for k in unseen if carded_key(k) in recorded}
    held |= {k for k in wanted if held_key(k) in recorded}
    kept = {k for k in wanted if kept_key(k) in recorded}
    return held | kept, kept


def _final(card: Any) -> dict[str, Any]:
    """A card's final payload (what its approval left: ``closed_code``,
    ``attempts``), or {}."""
    import json

    try:
        final = json.loads(getattr(card, "final_payload_json", None) or "{}")
    except (TypeError, ValueError):
        return {}
    return final if isinstance(final, dict) else {}


def carded_key(key: str) -> str:
    """A once job's record that a card was left for a finding (its dedupe
    digest ``key``), kept beside what its notices told (``watch_told``)."""
    return f"card:{key}"


def held_key(key: str) -> str:
    """A once job's record that ERPNext refused a finding's check for good
    while it stands (a ToDo already open for it): held, never dropped with
    an older card of the same key."""
    return f"held:{key}"


def kept_key(key: str) -> str:
    """A once job's record that ERPNext kept a finding from the reviewer:
    held, and its notice offered to the principal until one has told it."""
    return f"kept:{key}"


# How many card records this process couldn't write, logged with each.
_MARK_FAILURES: dict[str, int] = {}


def _mark_carded(job: registry.WatchJob, day: str, key: str, record: Any = None) -> None:
    from openexecutive.watch.settings import mark_told

    try:
        mark_told(job.id, [(record or carded_key)(key)], day)
    except Exception:
        # The ledger's own card still counts: only its listing is bounded.
        _MARK_FAILURES[job.id] = _MARK_FAILURES.get(job.id, 0) + 1
        logger.warning(
            "watch: job %s's card couldn't be recorded as left (%d such failures since start)",
            job.id, _MARK_FAILURES[job.id], exc_info=True,
        )


async def _leave_one(
    job: registry.WatchJob, day: str, spec: registry.DraftSpec, key: str, reviewer: Any, email: str,
    configured_by: int, reasons: Mapping[str, str], out: DraftOutcome,
) -> bool:
    """Check one card as the reviewer and leave it; whether it was left (a
    new card). A finding with no card is added to ``out.unproposed``."""
    from openexecutive.integrations import erpnext_drafts as drafts
    from openexecutive.watch.runner import MAX_REASON_CHARS, clean

    assert job.check_fields is not None
    fields = job.check_fields(spec.operation, spec.doctype, spec.fields, spec.name)
    if isinstance(fields, str):
        _audit_check(job, day, spec.operation, reviewer.id, "check_fields")
        out.unproposed.append(spec)
        return False
    out.checks += 1
    code, checked = await _check(job, day, spec, fields, reviewer, email)
    pinned: dict[str, Any] | str = fields
    if code == "ok" and spec.operation == "create":
        pinned = drafts.pin_from_check(spec.doctype, fields, checked.get("summary"))
        again = job.check_fields(spec.operation, spec.doctype, pinned, spec.name) if isinstance(pinned, dict) else "no"
        if isinstance(pinned, str) or again != pinned:
            code = "check_fields"
    _audit_check(job, day, spec.operation, reviewer.id, code)
    if code != "ok":
        if code in CHECK_LINES:
            # Our own reading of ERPNext's answer, not ERPNext's refusal.
            out.codes[code] = out.codes.get(code, 0) + 1
        else:
            out.check_refused += 1
        if code in ("check_permission", "check_not_theirs"):
            out.permission_refused += 1
        if code in KEPT_CODES:
            out.kept_from_reviewer.add(spec.key)
        if job.once and code in HELD_CHECK_CODES:
            # A once job's finding ERPNext refused for good (a ToDo already
            # open for it, or kept from the reviewer, whose row goes to the
            # principal): held like a card left for it, so it isn't checked
            # again every run, using up the day's checks before the findings
            # behind it. A key with a period in it (a week) frees it then.
            # Its own record kind: a kept one is still kept on later runs.
            _mark_carded(job, day, key, kept_key if code in KEPT_CODES else held_key)
        out.unproposed.append(spec)
        return False
    if not isinstance(pinned, dict):
        out.unproposed.append(spec)
        return False
    kept: dict[str, Any] | None = None
    amount = 0.0
    if spec.operation in ("match", "fill"):
        from openexecutive.integrations import erpnext_fill, erpnext_match
        from openexecutive.watch.settings import take_day_amount

        # A bank match's or an invoice fill's slice of the check, kept for
        # approval, and its amount against the day's cap.
        money = erpnext_match if spec.operation == "match" else erpnext_fill
        kept = money.slice_of(checked.get("summary"))
        cap = money.amount_cap(job)
        found = money.card_amount(checked)
        if kept is None or cap is None or found is None:
            out.unproposed.append(spec)
            return False
        amount = found
        if not take_day_amount(job.id, day, amount, cap):
            _audit_check(job, day, spec.operation, reviewer.id, "check_amount_cap")
            out.codes["check_amount_cap"] = out.codes.get("check_amount_cap", 0) + 1
            out.unproposed.append(spec)
            return False
    # Read again after the check, with no await before the card is stored.
    left = False
    try:
        if drafts.job_room(job.id, reviewer.id) is not None:
            out.capped = 1
            out.unproposed.append(spec)
            return False
        try:
            _id, new = drafts.leave_job_card(
                job, day, spec.operation, spec.doctype, pinned, checked,
                reviewer=reviewer, configured_by_person_id=configured_by, dedup_key=key,
                why=clean(spec.title, 140), reason=clean(reasons.get(spec.reason_key, ""), MAX_REASON_CHARS),
                name=spec.name if spec.operation != "create" else None, kept=kept, also=spec.shown,
            )
        except Exception:
            logger.error("watch: job %s couldn't leave a card", job.id)
            out.unproposed.append(spec)
            return False
        left = new
    finally:
        if amount and not left:
            # Not a new card after all: its amount goes back to the day's cap.
            from openexecutive.watch.settings import take_day_amount

            take_day_amount(job.id, day, -amount, 0.0)
    if new:
        out.left += 1
        if job.once:
            _mark_carded(job, day, key)
    else:
        out.repeats += 1
    return new
