"""Every module that decides access by a Person reads ``Person.active`` (or
``people.store.ACTIVE_SQL`` / ``active_sql``), never ``archived`` alone: a
suspended person must be out of sign-in, chat, approvals, mail and chasing
everywhere at once.

The scan finds every ``x.archived`` read, ``getattr(x, "archived", …)`` and
``archived = 0`` SQL fragment in ``openexecutive/``. A module may keep one
only when it is listed below with why: it means "ever existed" (history,
an address that must stay unique), it writes the flag, or it isn't about
People at all. A new access check that reads ``archived`` fails here.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "openexecutive"

_ARCHIVED_READ = re.compile(
    r'(?<![\w.])\w+\.archived\b(?!_)|getattr\(\s*\w+\s*,\s*"archived"|\barchived\s*=\s*0\b'
)

# module -> (how many reads it may keep, why)
ALLOWED: dict[str, tuple[int, str]] = {
    "people/models.py": (1, "Person.active is defined from it"),
    "integrations/erpnext_demo_people.py": (
        1,
        "the demo admin console's archive answers \"already\" for an archived demo person",
    ),
    "people/store.py": (
        16,
        "active_sql itself; address_holder, alias_holder and chat_id_holder keep a "
        "suspended person's addresses and chat ids theirs; list_people(include_suspended=True); "
        "archive, suspend and restore write the flags; list_directory_managed and "
        "find_person_by_directory_subject give the directory sync every managed row, "
        "suspended ones included, so it can restore them; claim_principal and add_principal "
        "count a suspended principal as the principal, and add_principal (as address_holder) "
        "a suspended person's address as still theirs; archive_demo_person guards its UPDATE",
    ),
    "api/routes/people.py": (
        3,
        "suspend and restore refuse an archived row; a suspended person's working "
        "style is kept, so the principal can still read it",
    ),
    "attunement/open_loops.py": (1, "an archived and a suspended owner get different refusal reasons"),
    "memory/replay.py": (1, "history: replays a chat that already happened"),
    "orchestrator/people_tools.py": (1, "an output field of list_people, not a check"),
    "fixtures/store.py": (3, "the generated_fixtures table, not People"),
    "integrations/discord_bot.py": (1, "a Discord thread's archived flag, not a Person"),
}

# Modules the plan names as deciding access; each must read the predicate.
MUST_READ_ACTIVE = [
    "api/routes/auth.py",
    "api/routes/chat.py",
    "api/routes/sessions.py",
    "api/routes/erpnext_drafts.py",
    "attunement/style.py",
    "delegation/action_cards.py",
    "delegation/reminders.py",
    "integrations/erpnext_drafts.py",
    "memory/chat_review.py",
    "orchestrator/app_control_catalog.py",
    "orchestrator/artifact_records.py",
    "orchestrator/content_trust.py",
    "orchestrator/department_gate.py",
    "orchestrator/outbound_guard.py",
    "orchestrator/people_tools.py",
    "orchestrator/schedule_tools.py",
    "orchestrator/teammate_mail.py",
    "people/channel.py",
    "people/held_messages.py",
    "people/reach.py",
    "people/roster_requests.py",
    "people/store.py",
    "scheduler/nudge_engine.py",
    "scheduler/runner.py",
]

_ACTIVE_READ = re.compile(r"\.active\b|\bACTIVE_SQL\b|\bactive_sql\(|list_people\(|find_person_by_")


def _archived_reads() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(ROOT.rglob("*.py")):
        count = sum(
            len(_ARCHIVED_READ.findall(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        if count:
            found[path.relative_to(ROOT).as_posix()] = count
    return found


def test_no_module_decides_access_by_archived_alone() -> None:
    found = _archived_reads()
    unexpected = {m: n for m, n in found.items() if m not in ALLOWED}
    assert not unexpected, (
        f"{unexpected}: read Person.active (or people.store.ACTIVE_SQL) for an access "
        "check, or add the module to ALLOWED with why it means 'ever existed'"
    )
    grown = {m: (n, ALLOWED[m][0]) for m, n in found.items() if m in ALLOWED and n > ALLOWED[m][0]}
    assert not grown, f"{grown}: (found, allowed). A new archived read needs a reason in ALLOWED"


def test_the_allow_list_is_not_stale() -> None:
    """A module that no longer reads archived comes off the list, so the
    counts keep meaning something."""
    found = _archived_reads()
    stale = {m: (found.get(m, 0), n) for m, (n, _) in ALLOWED.items() if found.get(m, 0) != n}
    assert not stale, f"{stale}: (found, allowed) — update ALLOWED"


def test_every_access_module_reads_the_predicate() -> None:
    missing = [
        m for m in MUST_READ_ACTIVE
        if not _ACTIVE_READ.search((ROOT / m).read_text(encoding="utf-8"))
    ]
    assert not missing, missing
