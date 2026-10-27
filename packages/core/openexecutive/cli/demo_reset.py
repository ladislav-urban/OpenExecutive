"""``openexecutive demo-reset --bundle <dir>``: put the demo company back.

The demo install resets ERPNext's demo company every night, and oe_connector
writes OpenExecutive's half of the same company as a bundle in the curated
fixtures' format (oe_connector's ``docs/demo/openexecutive-demo-data-plan.md``):
``profile.yaml``, ``people.yaml``, ``departments.yaml``, ``memory.json``,
``docs/*.md`` and a ``bundle.json`` naming the format, the company, when it was
made and ERPNext's site. This applies it.

What it does (decided by the owner, Oct 27, 2026):

- **Replaces the company:** the profile, ``company/docs/`` and their
  collection, departments and goals, decisions, initiatives, advice, briefing
  alerts, scheduled actions, the watch list, and the fictional roster (every
  Person the directory doesn't manage, except the principal).
- **Deletes demo people's own state** (``Person.demo``): their chats, cards,
  memory and question count, through ``PERSON_STATE`` / ``SESSION_STATE``.
- **Keeps** every directory-managed Person as it is, everyone's sign-in,
  the workspace settings, keys, the audit log and usage records, and the
  principal's own chats, memory and cards.

It refuses a bundle whose ``bundle.json`` is missing, of another format or
version, or names a company other than the profile's or ``DEMO_RESET_COMPANY``
(unset refuses every bundle, so a real company is never replaced by one). The
roster may hold no principal and no contact handles, so nobody in it can be
messaged, and ``memory.json`` no scheduled actions. Nothing is changed until
the whole bundle has passed.

Run it with the API stopped (``docker compose run``, not ``exec``): the
fixture lock is per process, and a running API keeps the company's caches and
an open vector store.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ConfigDict, ValidationError

from openexecutive.fixtures.generator import DepartmentSpec, MemorySpec, PersonSpec

logger = logging.getLogger(__name__)

BUNDLE_FORMAT = "openexecutive-demo-bundle"
# Raised only for a change an older reader would misread; oe_connector's writer
# (``oe_connector/demo_data/openexecutive.py``) sets the same number.
BUNDLE_FORMAT_VERSION = 1

BUNDLE_FILES = ("bundle.json", "profile.yaml", "people.yaml", "departments.yaml", "memory.json")
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DOCS = 200
MAX_DOC_BYTES = 1024 * 1024

# Each demo person's own rows: (table, column holding their person id).
PERSON_STATE: tuple[tuple[str, str], ...] = (
    ("chat_topics", "owner_person_id"),
    ("session_shares", "person_id"),
    ("session_drive_files", "person_id"),
    ("session_drive_searches", "person_id"),
    ("chat_review_turns", "person_id"),
    ("chat_review_days", "person_id"),
    ("chat_review_weeks", "person_id"),
    ("chat_review_lessons", "person_id"),
    ("chat_review_lesson_uses", "person_id"),
    # Their cards: ERPNext drafts and every other decision only they approve.
    ("decision_instances", "approver_person_id"),
    ("facts", "recorded_by_person_id"),
    ("fact_approval_rules", "person_id"),
    ("history_notes", "person_id"),
    ("history_person_settings", "person_id"),
    ("history_excluded", "person_id"),
    ("history_chat_passes", "person_id"),
    ("history_reminders", "person_id"),
    ("history_work_style_sharing", "person_id"),
    ("attunement_profile_history", "person_id"),
    ("attunement_profiles", "person_id"),
    ("proactive_outcomes", "person_id"),
    ("held_messages", "person_id"),
    ("demo_questions", "person_id"),
)

# Rows of their chats, by the chat's session id; the sessions go last.
SESSION_STATE: tuple[tuple[str, str], ...] = (
    ("chat_messages", "session_id"),
    ("session_shares", "session_id"),
    ("session_drive_files", "session_id"),
    ("session_drive_searches", "session_id"),
    ("chat_review_turns", "session_id"),
    ("chat_review_lessons", "session_id"),
    ("facts", "session_id"),
    ("onboarding_interviews", "session_id"),
)


class BundleError(ValueError):
    """The bundle can't be applied; nothing was changed."""


@dataclass(frozen=True)
class Bundle:
    directory: Path
    company: str
    made_at: str
    erpnext_site: str


class _BundlePerson(PersonSpec):
    # Unknown keys refused, so a contact handle (email, slack_user_id, ...)
    # can't slip in under the model's defaults.
    model_config = ConfigDict(extra="forbid")


class _BundleDepartment(DepartmentSpec):
    model_config = ConfigDict(extra="forbid")


class _BundleMemory(MemorySpec):
    # No scheduled_actions: a past-dated one runs on the next scheduler tick.
    model_config = ConfigDict(extra="forbid")


def _read_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise BundleError(f"{path.name} is missing or not a plain file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise BundleError(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
    return path.read_text(encoding="utf-8")


def _load_yaml(path: Path, key: str) -> list[Any]:
    try:
        data = yaml.safe_load(_read_file(path)) or {}
    except yaml.YAMLError as error:
        raise BundleError(f"{path.name} is not valid YAML: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get(key, []), list):
        raise BundleError(f"{path.name} must be a mapping with a {key!r} list")
    return list(data.get(key) or [])


def _check_docs(docs_dir: Path) -> int:
    if not docs_dir.exists():
        return 0
    if docs_dir.is_symlink() or not docs_dir.is_dir():
        raise BundleError("docs is not a plain directory")
    docs = sorted(docs_dir.glob("*.md"))
    if len(docs) > MAX_DOCS:
        raise BundleError(f"docs holds more than {MAX_DOCS} documents")
    for doc in docs:
        # A link could pull any file the API can read into the knowledge base.
        if doc.is_symlink() or not doc.is_file():
            raise BundleError(f"docs/{doc.name} is not a plain file")
        if doc.stat().st_size > MAX_DOC_BYTES:
            raise BundleError(f"docs/{doc.name} is larger than {MAX_DOC_BYTES} bytes")
    return len(docs)


def read_bundle(directory: Path, expected_company: str) -> Bundle:
    """Check the whole bundle in ``directory``; raises ``BundleError``."""
    from openexecutive.memory.company_profile import CompanyProfile

    if not directory.is_dir():
        raise BundleError(f"{directory} is not a directory")
    if not (directory / "bundle.json").exists():
        raise BundleError("bundle.json is missing")
    try:
        meta = json.loads(_read_file(directory / "bundle.json"))
    except json.JSONDecodeError as error:
        raise BundleError(f"bundle.json is not valid JSON: {error}") from None
    if not isinstance(meta, dict):
        raise BundleError("bundle.json must be an object")
    if meta.get("format") != BUNDLE_FORMAT:
        raise BundleError(f"bundle.json's format is not {BUNDLE_FORMAT!r}")
    if meta.get("format_version") != BUNDLE_FORMAT_VERSION:
        raise BundleError(
            f"bundle.json's format_version is {meta.get('format_version')!r};"
            f" this OpenExecutive reads {BUNDLE_FORMAT_VERSION}"
        )
    company, made_at, site = meta.get("company"), meta.get("made_at"), meta.get("erpnext_site")
    if not isinstance(company, str) or not company.strip():
        raise BundleError("bundle.json names no company")
    if not isinstance(site, str) or not site.strip() or len(site) > 200:
        raise BundleError("bundle.json names no ERPNext site")
    try:
        datetime.fromisoformat(str(made_at))
    except ValueError:
        raise BundleError("bundle.json's made_at is not an ISO 8601 time") from None
    if not expected_company.strip():
        raise BundleError("DEMO_RESET_COMPANY is not set, so this install takes no demo bundle")
    if company.strip() != expected_company.strip():
        raise BundleError(f"the bundle is for {company!r}, not this install's {expected_company!r}")

    for name in BUNDLE_FILES:
        _read_file(directory / name)
    try:
        profile = CompanyProfile.load_from_yaml(directory / "profile.yaml")
    except (ValidationError, yaml.YAMLError, AttributeError) as error:
        raise BundleError(f"profile.yaml does not validate: {error}") from None
    if profile.name.strip() != company.strip():
        raise BundleError(f"profile.yaml is for {profile.name!r}, not {company!r}")

    try:
        people = [_BundlePerson.model_validate(row) for row in _load_yaml(directory / "people.yaml", "people")]
        departments = [
            _BundleDepartment.model_validate(row)
            for row in _load_yaml(directory / "departments.yaml", "departments")
        ]
        memory = _BundleMemory.model_validate(json.loads(_read_file(directory / "memory.json")))
    except ValidationError as error:
        raise BundleError(f"the bundle does not validate: {error}") from None
    except json.JSONDecodeError as error:
        raise BundleError(f"memory.json is not valid JSON: {error}") from None

    names = [p.full_name.strip() for p in people]
    if not people or not departments:
        raise BundleError("the bundle has no people or no departments")
    if any(not n for n in names) or len(set(names)) != len(names):
        raise BundleError("people.yaml has an empty or repeated full_name")
    if any(p.is_principal for p in people):
        raise BundleError("people.yaml may not name a principal: the install's own stays")
    for dept in departments:
        if dept.head_person_name and dept.head_person_name.strip() not in names:
            raise BundleError(f"department {dept.slug!r}'s head is not in people.yaml")
    for alert in memory.alerts:
        if alert.routed_to_person and alert.routed_to_person.strip() not in names:
            raise BundleError(f"alert {alert.headline!r} goes to someone not in people.yaml")
    _check_docs(directory / "docs")
    return Bundle(directory=directory, company=company.strip(), made_at=str(made_at), erpnext_site=site.strip())


def _tables(conn: sqlite3.Connection) -> dict[str, set[str]]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {n: {c[1] for c in conn.execute(f"PRAGMA table_info({n})")} for n in names}  # noqa: S608 — names from sqlite_master


def _delete_in(
    conn: sqlite3.Connection, tables: dict[str, set[str]], table: str, column: str, values: list[Any]
) -> int:
    if not values or column not in tables.get(table, set()):
        return 0
    deleted = 0
    for start in range(0, len(values), 500):
        chunk = values[start:start + 500]
        marks = ",".join("?" * len(chunk))
        # Table and column come from PERSON_STATE / SESSION_STATE, never the bundle.
        deleted += conn.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", chunk).rowcount  # noqa: S608
    return deleted


def clear_people_state(person_ids: list[int], db_path: Path) -> dict[str, int]:
    """Delete ``person_ids``' own chats, cards, memory and question count."""
    counts: dict[str, int] = {}
    if not person_ids or not db_path.exists():
        return counts
    conn = sqlite3.connect(str(db_path))
    try:
        tables = _tables(conn)
        sessions: list[Any] = []
        if "caller_person_id" in tables.get("sessions", set()):
            for start in range(0, len(person_ids), 500):
                chunk = person_ids[start:start + 500]
                sessions += [
                    r[0]
                    for r in conn.execute(
                        f"SELECT session_id FROM sessions WHERE caller_person_id IN ({','.join('?' * len(chunk))})",  # noqa: S608 — placeholders only
                        chunk,
                    )
                ]
        # A replay is keyed by the reviewed turn, so it goes before the turns.
        if "turn_row_id" in tables.get("chat_review_replays", set()) and "chat_review_turns" in tables:
            turns: set[Any] = set()
            for column, values in (("person_id", person_ids), ("session_id", sessions)):
                for start in range(0, len(values), 500):
                    chunk = values[start:start + 500]
                    turns.update(
                        r[0]
                        for r in conn.execute(
                            f"SELECT id FROM chat_review_turns WHERE {column} IN ({','.join('?' * len(chunk))})",  # noqa: S608 — fixed column
                            chunk,
                        )
                    )
            counts["chat_review_replays"] = _delete_in(
                conn, tables, "chat_review_replays", "turn_row_id", sorted(turns)
            )
        for table, column in SESSION_STATE:
            counts[table] = counts.get(table, 0) + _delete_in(conn, tables, table, column, sessions)
        for table, column in PERSON_STATE:
            counts[table] = counts.get(table, 0) + _delete_in(conn, tables, table, column, person_ids)
        counts["sessions"] = _delete_in(conn, tables, "sessions", "session_id", sessions)
        conn.commit()
    finally:
        conn.close()
    return {k: v for k, v in counts.items() if v}


def _roster_ids(db_path: Path) -> tuple[list[int], list[int]]:
    """(demo people, the fictional roster): who loses their own state."""
    from openexecutive.people import store as people_store

    if not db_path.exists():
        return [], []
    people_store.initialize_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        demo = [r[0] for r in conn.execute("SELECT id FROM people WHERE directory_managed = 1 AND directory_demo = 1")]
        fictional = [r[0] for r in conn.execute("SELECT id FROM people WHERE directory_managed = 0 AND is_principal = 0")]
    finally:
        conn.close()
    return demo, fictional


def seed_demo_people(people_path: Path) -> int:
    """Replace the fictional roster with ``people.yaml``'s; returns how many.

    Keeps the principal and every directory-managed Person as they are. A
    bundle row with a kept Person's name is not added again: departments and
    alerts naming it resolve to the kept Person. Caches name -> id for the
    department and alert seeders, as ``fixture_loader._seed_people`` does.
    """
    from openexecutive.cli import fixture_loader
    from openexecutive.people import store as people_store
    from openexecutive.people.models import AuthorityScope, AvailabilityWindow

    rows = [_BundlePerson.model_validate(r) for r in _load_yaml(people_path, "people")]
    db_path = people_store.DB_PATH
    people_store.initialize_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        tables = _tables(conn)
        kept = {
            r[1].strip(): r[0]
            for r in conn.execute("SELECT id, full_name FROM people WHERE directory_managed = 1 OR is_principal = 1")
        }
        gone = [r[0] for r in conn.execute("SELECT id FROM people WHERE directory_managed = 0 AND is_principal = 0")]
        for table in ("person_authority_scope", "person_availability", "person_emails"):
            _delete_in(conn, tables, table, "person_id", gone)
        if gone:
            marks = ",".join("?" * len(gone))
            conn.execute(
                f"UPDATE people SET reports_to_person_id = NULL WHERE reports_to_person_id IN ({marks})",  # noqa: S608
                gone,
            )
            _delete_in(conn, tables, "people", "id", gone)
        conn.commit()
    finally:
        conn.close()

    name_to_id = dict(kept)
    added = 0
    for row in rows:
        name = row.full_name.strip()
        if name in kept:
            continue
        person_id = people_store.upsert_person(
            full_name=name,
            role=row.role,
            preferred_channel=row.preferred_channel,
            response_sla_hours=row.response_sla_hours,
            department_slugs=list(row.department_slugs),
            db_path=db_path,
        )
        name_to_id[name] = person_id
        added += 1
        scopes = []
        for raw in row.authority_scope:
            try:
                scopes.append(AuthorityScope(raw))
            except ValueError:
                continue
        if scopes:
            people_store.set_authority_scope(person_id, scopes, db_path=db_path)
        windows = [AvailabilityWindow(**w.model_dump()) for w in row.availability]
        if windows:
            people_store.set_availability(person_id, windows, db_path=db_path)
    fixture_loader._seed_people._name_to_id = name_to_id  # type: ignore[attr-defined]
    return added


@contextmanager
def _process_lock(path: Path) -> Iterator[None]:
    """One reset at a time across processes (the fixture lock is per process)."""
    try:
        import fcntl
    except ImportError:  # Windows: the demo install runs on Linux
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BundleError("another demo reset is running") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def demo_reset(bundle_dir: Path, settings: Any) -> dict[str, Any]:
    """Apply the bundle in ``bundle_dir`` with the keep rules above."""
    from openexecutive.audit import log_event
    from openexecutive.cli import fixture_loader
    from openexecutive.memory.episodic import DB_PATH

    started = time.monotonic()
    bundle = read_bundle(bundle_dir, settings.demo_reset_company)
    with _process_lock(settings.company_profile_path.parent / ".demo_reset.lock"):
        async with fixture_loader._FIXTURE_OP_LOCK:
            demo_ids, fictional_ids = _roster_ids(DB_PATH)
            cleared = clear_people_state(sorted({*demo_ids, *fictional_ids}), DB_PATH)
            # Seeders that find no rows leave the old ones, so the company's
            # are emptied here first; the alerts' own rows go with them.
            if DB_PATH.exists():
                conn = sqlite3.connect(str(DB_PATH))
                try:
                    tables = _tables(conn)
                    for table in ("alert_held_actions", "alert_approvals", "department_goals", "departments"):
                        if table in tables:
                            conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed names
                    conn.commit()
                finally:
                    conn.close()
            summary = await fixture_loader._apply_state_from_source(bundle.directory, settings, demo_reset=True)
            # memory.json replaced every scheduled action, the built-in ones too.
            fixture_loader.rebootstrap_scheduled_actions()
    seconds = round(time.monotonic() - started, 1)
    summary.update(
        {
            "company": bundle.company,
            "bundle_made_at": bundle.made_at,
            "erpnext_site": bundle.erpnext_site,
            "demo_people": len(demo_ids),
            "people_state_cleared": cleared,
            "seconds": seconds,
        }
    )
    summary.pop("profile", None)
    log_event(
        "demo_reset",
        f"Demo company reset from the bundle made {bundle.made_at} ({bundle.erpnext_site})",
        actor="cli",
        details={
            "company": bundle.company,
            "docs_indexed": summary.get("docs_indexed"),
            "people_seeded": summary.get("people_seeded"),
            "demo_people": len(demo_ids),
            "seconds": seconds,
        },
    )
    return summary
