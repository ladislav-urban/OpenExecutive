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

The bundle comes from another host, so it is untrusted: it is first copied
into a private directory without following links (``stage_bundle``), then
checked and applied from that copy only, so it can't change between the two.
It is refused, with nothing changed, when ``bundle.json`` is missing, of
another format or version, or names a company other than the profile's or
``DEMO_RESET_COMPANY`` (unset refuses every bundle, so a real company is never
replaced by one). Nobody it adds can be reached: the roster may hold no
principal, no contact handle and no name of a Person the reset keeps (an alert
or a department naming them would message a real person), departments may
not act on their own (``auto_execute``), and ``memory.json`` may hold no
scheduled actions.

Run it with the API stopped (``docker compose run``, not ``exec``): the API
holds ``api_lock`` shared while it runs, and the reset takes it exclusively,
so it refuses next to a running API (or another reset).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import yaml
from pydantic import ConfigDict, ValidationError

from openexecutive.departments.models import AuthorityLevel
from openexecutive.fixtures.generator import DepartmentSpec, MemorySpec, PersonSpec

logger = logging.getLogger(__name__)

BUNDLE_FORMAT = "openexecutive-demo-bundle"
# Raised only for a change an older reader would misread; oe_connector's writer
# (``oe_connector/demo_data/openexecutive.py``) sets the same number.
BUNDLE_FORMAT_VERSION = 1

BUNDLE_FILES = ("bundle.json", "profile.yaml", "people.yaml", "departments.yaml", "memory.json")
MAX_FILE_BYTES = 1024 * 1024
MAX_DOCS = 200
MAX_DOC_BYTES = 1024 * 1024

LOCK_NAME = ".demo_reset.lock"

# Each demo person's own rows: (table, column holding their person id). A
# table that isn't there (created on first use) is skipped.
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
    ("brief_requests", "person_id"),
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
    ("outbound_context", "recipient_person_id"),
    ("person_notification_settings", "person_id"),
    ("take_the_lead_allowed", "person_id"),
    ("workflow_runs", "owner_person_id"),
    ("dynamic_workflows", "owner_person_id"),
    ("delegation_settings", "person_id"),
    ("delegation_voice", "person_id"),
    ("delegation_voice_history", "person_id"),
    ("delegation_drafts", "person_id"),
    ("delegation_inbox_watch", "person_id"),
    ("delegation_inbox_messages", "person_id"),
    ("delegation_handle_it", "person_id"),
    ("delegation_handled", "person_id"),
    ("delegation_reminders", "person_id"),
    ("delegation_mail_read", "person_id"),
    ("delegation_training", "person_id"),
    ("delegation_disclosure", "person_id"),
    ("delegation_calendar", "person_id"),
    ("delegation_saved_files", "person_id"),
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
    ("brief_requests", "session_id"),
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


# ── Staging: a private copy, read without following links ───────────────────

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_FD = os.open in os.supports_dir_fd and _NOFOLLOW != 0


def _copy_fd(fd: int, name: str, limit: int, dest: Path) -> None:
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BundleError(f"{name} is not a plain file")
        if info.st_size > limit:
            raise BundleError(f"{name} is larger than {limit} bytes")
        with os.fdopen(os.dup(fd), "rb") as source:
            data = source.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise BundleError(f"{name} is larger than {limit} bytes")
    dest.write_bytes(data)


def _open_in(dir_fd: int | None, parent: Path, name: str, flags: int) -> int:
    """Open ``name`` in a directory without following a link at it."""
    try:
        if _DIR_FD and dir_fd is not None:
            return os.open(name, flags | _NOFOLLOW, dir_fd=dir_fd)
        # No dir_fd (Windows): the link check and the open are two steps.
        if (parent / name).is_symlink():
            raise BundleError(f"{name} is a link")
        return os.open(parent / name, flags | _NOFOLLOW)
    except FileNotFoundError:
        raise BundleError(f"{name} is missing") from None
    except OSError as error:
        raise BundleError(f"{name} can't be opened as a plain file ({error.strerror})") from None


def stage_bundle(source: Path, dest: Path) -> Path:
    """Copy the bundle's files (and only them) from ``source`` into ``dest``."""
    if not source.is_dir():
        raise BundleError(f"{source} is not a directory")
    dest.mkdir(mode=0o700)
    src_fd = os.open(source, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)) if _DIR_FD else None
    try:
        for name in BUNDLE_FILES:
            _copy_fd(_open_in(src_fd, source, name, os.O_RDONLY), name, MAX_FILE_BYTES, dest / name)
        docs_fd: int | None = None
        if _DIR_FD:
            try:
                docs_fd = os.open(
                    "docs", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW, dir_fd=src_fd
                )
            except FileNotFoundError:
                return dest
            except OSError:
                raise BundleError("docs is not a plain directory") from None
        else:
            docs_path = source / "docs"
            if not docs_path.exists() and not docs_path.is_symlink():
                return dest
            if docs_path.is_symlink() or not docs_path.is_dir():
                raise BundleError("docs is not a plain directory")
        try:
            names = sorted(os.listdir(docs_fd if docs_fd is not None else source / "docs"))
            docs = [n for n in names if n.endswith(".md")]
            if len(docs) > MAX_DOCS:
                raise BundleError(f"docs holds more than {MAX_DOCS} documents")
            (dest / "docs").mkdir(mode=0o700)
            for name in docs:
                if name != Path(name).name or name.startswith("."):
                    raise BundleError(f"docs/{name} is not a plain document name")
                fd = _open_in(docs_fd, source / "docs", name, os.O_RDONLY)
                _copy_fd(fd, f"docs/{name}", MAX_DOC_BYTES, dest / "docs" / name)
        finally:
            if docs_fd is not None:
                os.close(docs_fd)
    finally:
        if src_fd is not None:
            os.close(src_fd)
    return dest


# ── Checks (on the staged copy) ──────────────────────────────────────────────


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BundleError(f"{path.name} is missing") from None
    except UnicodeDecodeError:
        raise BundleError(f"{path.name} is not UTF-8") from None


def _safe_yaml(path: Path) -> Any:
    text = _read_text(path)
    try:
        # Aliases refused: a few nested ones expand to gigabytes (alias bomb).
        if any(isinstance(token, yaml.AliasToken) for token in yaml.scan(text)):
            raise BundleError(f"{path.name} uses YAML aliases")
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        raise BundleError(f"{path.name} is not valid YAML: {error}") from None


def _load_yaml(path: Path, key: str) -> list[Any]:
    data = _safe_yaml(path)
    if not isinstance(data, dict) or not isinstance(data.get(key, []), list):
        raise BundleError(f"{path.name} must be a mapping with a {key!r} list")
    return list(data.get(key) or [])


def _name_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def read_bundle(directory: Path, expected_company: str, kept_names: set[str] | None = None) -> Bundle:
    """Check the whole (staged) bundle in ``directory``; raises ``BundleError``.

    ``kept_names`` are the Persons the reset keeps (``_kept_names``): the
    roster may not name one, or an alert or a department would reach them.
    """
    from openexecutive.memory.company_profile import CompanyProfile

    try:
        meta = json.loads(_read_text(directory / "bundle.json"))
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

    raw_profile = _safe_yaml(directory / "profile.yaml")
    try:
        profile = CompanyProfile.model_validate(
            raw_profile.get("company", raw_profile) if isinstance(raw_profile, dict) else raw_profile
        )
    except ValidationError as error:
        raise BundleError(f"profile.yaml does not validate: {error}") from None
    if profile.name.strip() != company.strip():
        raise BundleError(f"profile.yaml is for {profile.name!r}, not {company!r}")

    try:
        people = [_BundlePerson.model_validate(row) for row in _load_yaml(directory / "people.yaml", "people")]
        departments = [
            _BundleDepartment.model_validate(row)
            for row in _load_yaml(directory / "departments.yaml", "departments")
        ]
        memory = _BundleMemory.model_validate(json.loads(_read_text(directory / "memory.json")))
    except ValidationError as error:
        raise BundleError(f"the bundle does not validate: {error}") from None
    except json.JSONDecodeError as error:
        raise BundleError(f"memory.json is not valid JSON: {error}") from None

    names = [p.full_name.strip() for p in people]
    if not people or not departments:
        raise BundleError("the bundle has no people or no departments")
    if any(not n for n in names) or len({_name_key(n) for n in names}) != len(names):
        raise BundleError("people.yaml has an empty or repeated full_name")
    if any(p.is_principal for p in people):
        raise BundleError("people.yaml may not name a principal: the install's own stays")
    taken = sorted(n for n in names if _name_key(n) in (kept_names or set()))
    if taken:
        raise BundleError(f"people.yaml names someone this install keeps: {', '.join(taken)}")
    for dept in departments:
        if dept.head_person_name and dept.head_person_name.strip() not in names:
            raise BundleError(f"department {dept.slug!r}'s head is not in people.yaml")
        if dept.authority_level == AuthorityLevel.AUTO_EXECUTE.value:
            raise BundleError(f"department {dept.slug!r} may not act on its own (auto_execute)")
    for alert in memory.alerts:
        if alert.routed_to_person and alert.routed_to_person.strip() not in names:
            raise BundleError(f"alert {alert.headline!r} goes to someone not in people.yaml")
    return Bundle(directory=directory, company=company.strip(), made_at=str(made_at), erpnext_site=site.strip())


# ── The database ─────────────────────────────────────────────────────────────


def _tables(conn: sqlite3.Connection) -> dict[str, set[str]]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {n: {c[1] for c in conn.execute(f"PRAGMA table_info({n})")} for n in names}  # noqa: S608 — names from sqlite_master


def _select_in(conn: sqlite3.Connection, sql: str, values: list[Any]) -> list[Any]:
    """``sql`` with one ``{marks}`` placeholder list, run over ``values`` in chunks."""
    out: list[Any] = []
    for start in range(0, len(values), 500):
        chunk = values[start:start + 500]
        out += [r[0] for r in conn.execute(sql.format(marks=",".join("?" * len(chunk))), chunk)]
    return out


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
            sessions = _select_in(
                conn, "SELECT session_id FROM sessions WHERE caller_person_id IN ({marks})", person_ids
            )
        # A replay is keyed by the reviewed turn, so it goes before the turns.
        if "turn_row_id" in tables.get("chat_review_replays", set()) and "chat_review_turns" in tables:
            turns = {
                *_select_in(conn, "SELECT id FROM chat_review_turns WHERE person_id IN ({marks})", person_ids),
                *_select_in(conn, "SELECT id FROM chat_review_turns WHERE session_id IN ({marks})", sessions),
            }
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


def _roster(db_path: Path) -> tuple[list[int], list[int], set[str]]:
    """(demo people, the fictional roster, the kept people's names)."""
    from openexecutive.people import store as people_store

    if not db_path.exists():
        return [], [], set()
    people_store.initialize_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        # Never the principal, even one the directory marked demo.
        demo = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM people WHERE directory_managed = 1 AND directory_demo = 1 AND is_principal = 0"
            )
        ]
        fictional = [r[0] for r in conn.execute("SELECT id FROM people WHERE directory_managed = 0 AND is_principal = 0")]
        kept = {
            _name_key(r[0])
            for r in conn.execute("SELECT full_name FROM people WHERE directory_managed = 1 OR is_principal = 1")
        }
    finally:
        conn.close()
    return demo, fictional, kept


def seed_demo_people(people_path: Path) -> int:
    """Replace the fictional roster with ``people.yaml``'s; returns how many.

    Keeps the principal and every directory-managed Person as they are, and
    resolves the bundle's names only to the Persons it adds (``read_bundle``
    refused a name of a kept one). Caches name -> id for the department and
    alert seeders, as ``fixture_loader._seed_people`` does.
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
            _name_key(r[0])
            for r in conn.execute("SELECT full_name FROM people WHERE directory_managed = 1 OR is_principal = 1")
        }
        if any(_name_key(row.full_name) in kept for row in rows):
            raise BundleError("people.yaml names someone this install keeps")
        gone = [r[0] for r in conn.execute("SELECT id FROM people WHERE directory_managed = 0 AND is_principal = 0")]
        for table in ("person_authority_scope", "person_availability", "person_emails"):
            _delete_in(conn, tables, table, "person_id", gone)
        if gone:
            for start in range(0, len(gone), 500):
                chunk = gone[start:start + 500]
                conn.execute(
                    "UPDATE people SET reports_to_person_id = NULL"
                    f" WHERE reports_to_person_id IN ({','.join('?' * len(chunk))})",  # noqa: S608 — placeholders only
                    chunk,
                )
            _delete_in(conn, tables, "people", "id", gone)
        conn.commit()
    finally:
        conn.close()

    name_to_id: dict[str, int] = {}
    for row in rows:
        name = row.full_name.strip()
        person_id = people_store.upsert_person(
            full_name=name,
            role=row.role,
            preferred_channel=row.preferred_channel,
            response_sla_hours=row.response_sla_hours,
            department_slugs=list(row.department_slugs),
            db_path=db_path,
        )
        name_to_id[name] = person_id
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
    return len(name_to_id)


def _drop_orphan_alert_rows(db_path: Path) -> None:
    """The replaced alerts' approvals and held actions."""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = _tables(conn)
        for table in ("alert_held_actions", "alert_approvals"):
            if "alert_id" in tables.get(table, set()) and "alerts" in tables:
                conn.execute(f"DELETE FROM {table} WHERE alert_id NOT IN (SELECT id FROM alerts)")  # noqa: S608 — fixed names
        conn.commit()
    finally:
        conn.close()


# ── The lock the API holds while it runs ────────────────────────────────────


def _lock_path(settings: Any) -> Path:
    return Path(settings.company_profile_path).parent / LOCK_NAME


@contextmanager
def api_lock(settings: Any) -> Iterator[None]:
    """Held shared by a running API (``api/main.py``'s lifespan), so a reset,
    which takes it exclusively, refuses while the API is up. Works across
    containers sharing the company volume (one kernel). No-op without flock."""
    handle = _open_lock(settings)
    if handle is None:
        yield
        return
    import fcntl

    try:
        fcntl.flock(handle, fcntl.LOCK_SH)
        yield
    finally:
        handle.close()


def _open_lock(settings: Any) -> IO[str] | None:
    try:
        import fcntl  # noqa: F401 — POSIX only; the demo install runs on Linux
    except ImportError:
        return None
    path = _lock_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a")


@contextmanager
def _reset_lock(settings: Any) -> Iterator[None]:
    handle = _open_lock(settings)
    if handle is None:
        yield
        return
    import fcntl

    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BundleError("the API (or another demo reset) is running: stop it first") from None
        yield
    finally:
        handle.close()


# ── The reset ───────────────────────────────────────────────────────────────


async def demo_reset(bundle_dir: Path, settings: Any) -> dict[str, Any]:
    """Apply the bundle in ``bundle_dir`` with the keep rules above."""
    from openexecutive.audit import log_event
    from openexecutive.cli import fixture_loader
    from openexecutive.memory.episodic import DB_PATH

    started = time.monotonic()
    with _reset_lock(settings), tempfile.TemporaryDirectory(prefix="oe-demo-bundle-") as tmp:
        staged = stage_bundle(bundle_dir, Path(tmp) / "bundle")
        async with fixture_loader._FIXTURE_OP_LOCK:
            demo_ids, fictional_ids, kept_names = _roster(DB_PATH)
            bundle = read_bundle(staged, settings.demo_reset_company, kept_names)
            try:
                summary = await fixture_loader._apply_state_from_source(staged, settings, demo_reset=True)
                cleared = clear_people_state(sorted({*demo_ids, *fictional_ids}), DB_PATH)
                _drop_orphan_alert_rows(DB_PATH)
                # memory.json replaced every scheduled action, the built-in ones too.
                fixture_loader.rebootstrap_scheduled_actions()
            except Exception as error:
                log_event(
                    "demo_reset_failed",
                    f"Demo company reset from the bundle made {bundle.made_at} failed part way",
                    actor="cli",
                    details={"company": bundle.company, "error": type(error).__name__},
                )
                raise
    seconds = round(time.monotonic() - started, 1)
    summary.pop("profile", None)
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
