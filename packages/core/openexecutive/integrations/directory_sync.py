"""Follow the identity provider's directory: People in step with one group.

The identity provider's realm is the one place people are added, given access
and removed (oe_connector's ``docs/directory-sync-plan.md``, "The directory
contract"). Every ``DIRECTORY_SYNC_INTERVAL_MINUTES`` a scheduler heartbeat
reads ``DIRECTORY_SYNC_GROUP``'s members (Keycloak's admin API, read-only, as
the ``DIRECTORY_SYNC_CLIENT_ID`` service account) and keeps the team People
it manages in step:

- an active member nobody has yet becomes a team Person with their name and
  email and nothing else (the principal fills in the rest);
- a linked member's name and email follow the directory;
- a managed Person who is no longer an active member is suspended
  (``people.store.suspend_person``, reversible) and signed out of every web
  session (``memory.ended_sessions``, as a back-channel logout would), and
  restored when they come back.

Active means ``enabled``, ``emailVerified`` and an email. The link is the
provider's user id (the ``sub`` of its tokens), never the email.

It runs inside the API process and writes the store directly: the People
routes refuse a service caller (``_require_roster_owner``). It never touches
the principal (only links them, by their verified email), a contact or a
Person it doesn't manage: an unmanaged Person with a member's email is a
*link candidate*, linked by ``openexecutive directory-link --email``.

A run stops and changes nothing when the settings can't be trusted, any read
fails (a partial read must never look like "everyone else was removed"), no
managed person came back active, or the safety brake trips: more than
``DIRECTORY_SYNC_MAX_SUSPEND`` suspensions, or more than
``DIRECTORY_SYNC_MAX_SHARE`` of the managed team from two up. A run with
``allow_mass_suspend`` skips the last two. The token and secret are never
logged or audited.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from openexecutive.config import Settings, get_settings
from openexecutive.people.models import Person

logger = logging.getLogger(__name__)

HEARTBEAT_KIND = "directory_sync_scan"
HEARTBEAT_CHANNEL = "__internal__"
HEARTBEAT_CHANNEL_REF = "directory_sync"
HEARTBEAT_INTENT = "Directory sync — keep People in step with the identity provider's group."

PAGE = 100
TIMEOUT_S = 20.0
TOKEN_MARGIN_S = 30
STATUS_TABLE = "directory_sync_status"
# What a run reports that changes something, and so is audited.
WRITES = ("created", "updated", "linked", "suspended", "restored")

_run_lock = threading.Lock()
_token_lock = threading.Lock()
_tokens: dict[tuple[str, str], tuple[str, float]] = {}


class DirectoryError(Exception):
    """Why a run stopped. It changed nothing."""


@dataclass(frozen=True)
class Member:
    id: str
    email: str
    full_name: str
    active: bool


class DirectoryReader(Protocol):
    """Reads one group's members. Keycloak today; Entra ID or Google
    Workspace can be added beside it."""

    def read_members(self) -> dict[str, Member]: ...


@dataclass
class Report:
    dry_run: bool
    # (action, who, detail): created, updated, linked, restored, suspended,
    # candidate, conflict, skipped, kept suspended, warning, failed.
    changes: list[tuple[str, str, str]] = field(default_factory=list)
    unchanged: int = 0
    stopped: str | None = None

    def add(self, action: str, who: str, detail: str = "") -> None:
        self.changes.append((action, who, detail))

    def count(self, action: str) -> int:
        return sum(1 for change in self.changes if change[0] == action)

    @property
    def wrote(self) -> bool:
        return any(action in WRITES for action, _, _ in self.changes)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for action, _, _ in self.changes:
            out[action] = out.get(action, 0) + 1
        out["unchanged"] = self.unchanged
        return out

    def text(self) -> str:
        head = "Dry run: nothing was changed." if self.dry_run else ""
        if self.stopped:
            return f"{head} Stopped, nothing was changed: {self.stopped}".strip()
        lines = [head] if head else []
        lines += [f"{a}: {who}" + (f" ({detail})" if detail else "") for a, who, detail in self.changes]
        lines.append(f"{self.unchanged} unchanged.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def _plain_http_ok(host: str, *, internal: bool) -> bool:
    """Plain HTTP only to this machine, or with ``internal`` also to a
    private address or a one-label name (a compose service, ``keycloak``)."""
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return internal and "." not in host and bool(host)
    return ip.is_loopback or (internal and ip.is_private)


def _url_problem(url: str, name: str, *, internal: bool) -> str | None:
    parts = urlsplit(url)
    try:
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 — raises on a bad port
    except ValueError:
        return f"{name} isn't a URL"
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        return f"{name} must be a plain https URL"
    if parts.scheme == "https" or (parts.scheme == "http" and _plain_http_ok(host, internal=internal)):
        return None
    where = "this machine, a private address or a compose service" if internal else "this machine"
    return f"{name} must be https (plain http only to {where})"


def _realm(issuer: str) -> str:
    path = urlsplit(issuer).path
    return path.split("/realms/", 1)[1].strip("/") if "/realms/" in path else ""


def config_problem(settings: Settings) -> str | None:
    """Why the settings can't be used, or None. Checked when the settings
    load, so a sync that can't be trusted never starts."""
    issuer = settings.directory_sync_issuer.strip()
    # Exactly as the web app has it: an ended session's ``iss`` must equal
    # its AUTH_OIDC_ISSUER character for character, so no trailing slash.
    if issuer.endswith("/") or settings.auth_oidc_issuer.strip().endswith("/"):
        return "DIRECTORY_SYNC_ISSUER and AUTH_OIDC_ISSUER must not end with a slash"
    if problem := _url_problem(issuer, "DIRECTORY_SYNC_ISSUER", internal=False):
        return problem
    realm = _realm(issuer)
    if not realm or "/" in realm:
        return "DIRECTORY_SYNC_ISSUER must be the realm's issuer URL (…/realms/<realm>)"
    if issuer != settings.auth_oidc_issuer.strip():
        return "DIRECTORY_SYNC_ISSUER must equal AUTH_OIDC_ISSUER, the provider people sign in with"
    if problem := _url_problem(
        settings.directory_sync_admin_url.strip().rstrip("/"), "DIRECTORY_SYNC_ADMIN_URL", internal=True
    ):
        return problem
    if not settings.directory_sync_client_id.strip():
        return "DIRECTORY_SYNC_CLIENT_ID isn't set"
    if not settings.directory_sync_client_secret:
        return "DIRECTORY_SYNC_CLIENT_SECRET isn't set"
    group = settings.directory_sync_group.strip()
    if not group.startswith("/") or group == "/" or "//" in group or group.endswith("/"):
        return "DIRECTORY_SYNC_GROUP must be a group path, e.g. /dynocortex"
    return None


# --------------------------------------------------------------------------- #
# Keycloak
# --------------------------------------------------------------------------- #

def _claims(token: str) -> dict[str, Any]:
    """A JWT's claims, unverified: the token came straight from the realm's
    token endpoint over the admin URL, and only its issuer is compared."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


class KeycloakReader:
    """One group's members, read through Keycloak's admin API."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.issuer = settings.directory_sync_issuer.strip().rstrip("/")
        self.admin_url = settings.directory_sync_admin_url.strip().rstrip("/")
        self.realm = _realm(self.issuer)
        self.client_id = settings.directory_sync_client_id.strip()
        self._secret = settings.directory_sync_client_secret
        self.group = settings.directory_sync_group.strip()
        self._client = httpx.Client(
            timeout=TIMEOUT_S, follow_redirects=False, transport=transport
        )
        self._fresh = False

    def _token(self, *, fresh: bool = False) -> str:
        key = (self.issuer, self.client_id)
        with _token_lock:
            if fresh:
                _tokens.pop(key, None)
            kept = _tokens.get(key)
            if kept and kept[1] > time.monotonic():
                return kept[0]
        url = f"{self.admin_url}/realms/{quote(self.realm)}/protocol/openid-connect/token"
        try:
            response = self._client.post(
                url, data={"grant_type": "client_credentials"}, auth=(self.client_id, self._secret)
            )
            body = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError) as error:
            raise DirectoryError(f"the token endpoint can't be reached: {type(error).__name__}") from None
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str):
            raise DirectoryError(f"the provider refused the directory client's token ({response.status_code})")
        if str(_claims(token).get("iss") or "").rstrip("/") != self.issuer:
            raise DirectoryError(f"the token isn't from {self.issuer}: the wrong realm or address")
        expires = int(body.get("expires_in") or 0) - TOKEN_MARGIN_S
        if expires > 0:
            with _token_lock:
                _tokens[key] = (token, time.monotonic() + expires)
        return token

    def _get(self, path: str, **params: Any) -> Any:
        """One admin API read. A kept token the provider no longer takes (a
        restart, a new secret) is dropped and a new one tried, once per run."""
        url = f"{self.admin_url}/admin/realms/{quote(self.realm)}/{path}"
        token = self._token()
        while True:
            try:
                response = self._client.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as error:
                raise DirectoryError(f"the admin API can't be reached: {type(error).__name__}") from None
            if response.status_code != 401 or self._fresh:
                break
            self._fresh = True
            token = self._token(fresh=True)
        if response.status_code == 404 and path.startswith("group-by-path/"):
            raise DirectoryError(f"the group {self.group} doesn't exist in the realm")
        if response.status_code != 200:
            raise DirectoryError(f"the admin API answered {response.status_code} for {path.split('/')[0]}")
        try:
            return response.json()
        except ValueError:
            raise DirectoryError(f"the admin API's answer for {path.split('/')[0]} isn't JSON") from None

    def _paged(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Every item of a paged list. Any failed page stops the run."""
        items: list[dict[str, Any]] = []
        while True:
            page = self._get(path, first=len(items), max=PAGE, **params)
            if not isinstance(page, list):
                raise DirectoryError(f"the admin API's answer for {path.split('/')[0]} isn't a list")
            items += page
            if len(page) < PAGE:
                return items

    def read_members(self) -> dict[str, Member]:
        """Every member of the group and the groups under it, by user id."""
        try:
            path = "/".join(quote(part, safe="") for part in self.group.strip("/").split("/"))
            groups, todo = [], [self._get(f"group-by-path/{path}")]
            while todo:
                group = todo.pop()
                groups.append(group)
                todo += self._paged(f"groups/{quote(group['id'])}/children", briefRepresentation="true")
            found: dict[str, dict[str, Any]] = {}
            for group in groups:
                for user in self._paged(f"groups/{quote(group['id'])}/members", briefRepresentation="false"):
                    found[str(user["id"])] = user
        finally:
            self._client.close()
        return {uid: _member(uid, user) for uid, user in found.items()}


def _member(uid: str, user: dict[str, Any]) -> Member:
    # A user id is the link, and Keycloak's are UUIDs: anything else is an
    # answer of the wrong shape, and the run stops.
    if not isinstance(user.get("id"), str) or not uid.strip() or uid != uid.strip():
        raise ValueError("a member without a usable id")
    email = str(user.get("email") or "").strip().lower()
    name = " ".join(
        part for part in (str(user.get("firstName") or "").strip(), str(user.get("lastName") or "").strip()) if part
    )
    return Member(
        id=uid,
        email=email,
        full_name=name or email.split("@", 1)[0],
        active=bool(user.get("enabled") is True and user.get("emailVerified") is True and email),
    )


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #

def _who(person: Person) -> str:
    return person.email or f"person #{person.id}"


def _audit(event_type: str, summary: str, details: dict[str, Any]) -> None:
    try:
        from openexecutive.audit import log_event

        log_event(event_type, summary, actor="directory", details=details, private=True)
    except Exception:
        logger.warning("directory_sync: audit failed", exc_info=True)


def _brake(settings: Settings, suspend: int, population: int) -> str | None:
    if suspend > settings.directory_sync_max_suspend:
        return (
            f"it would suspend {suspend} people, more than DIRECTORY_SYNC_MAX_SUSPEND "
            f"({settings.directory_sync_max_suspend}). Check the realm and the group, then run "
            "`openexecutive directory-sync --allow-mass-suspend`."
        )
    if suspend >= 2 and population and suspend / population > settings.directory_sync_max_share:
        return (
            f"it would suspend {suspend} of the {population} people it manages, more than "
            f"DIRECTORY_SYNC_MAX_SHARE ({settings.directory_sync_max_share}). Check the realm and "
            "the group, then run `openexecutive directory-sync --allow-mass-suspend`."
        )
    return None


def sync(
    *,
    reader: DirectoryReader | None = None,
    dry_run: bool = False,
    allow_mass_suspend: bool = False,
) -> Report:
    """One run, never two at once in this process. Raises nothing: a stop is
    in ``Report.stopped``. ``reader`` replaces Keycloak (tests)."""
    report = Report(dry_run=dry_run)
    if not _run_lock.acquire(blocking=False):
        report.stopped = "another run is in progress."
        return report
    try:
        _sync(report, reader, allow_mass_suspend)
    except Exception as error:
        # Never a traceback with the token or secret in its locals.
        logger.error("directory_sync: run failed (%s)", type(error).__name__)
        report.stopped = f"the run failed ({type(error).__name__})."
    finally:
        _run_lock.release()
    if not dry_run and get_settings().directory_sync_enabled:
        _record_status(report)
        if report.wrote or report.stopped:
            _audit(
                "directory_sync",
                f"directory sync: {'stopped' if report.stopped else 'ran'}",
                {"counts": report.counts(), "stopped": report.stopped},
            )
    return report


def _sync(report: Report, reader: DirectoryReader | None, allow_mass_suspend: bool) -> None:
    from openexecutive.people import store

    settings = get_settings()
    if reader is None:
        if not settings.directory_sync_enabled:
            report.stopped = "DIRECTORY_SYNC_ENABLED is off."
            return
        if problem := config_problem(settings):
            report.stopped = problem
            return
        reader = KeycloakReader(settings)
    try:
        members = reader.read_members()
    except DirectoryError as error:
        report.stopped = str(error)
        return
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        report.stopped = f"the provider's answer isn't the shape the directory contract says ({type(error).__name__})."
        return

    active = {uid: m for uid, m in members.items() if m.active}
    managed = store.list_directory_managed()
    by_subject = {p.directory_subject: p for p in managed if p.directory_subject}
    team = [p for p in managed if p.kind == "team" and not p.is_principal]
    gone = [p for p in team if p.directory_subject not in active and not p.suspended]
    for person in managed:
        if person.is_principal and person.directory_subject not in active:
            report.add("warning", _who(person), "the principal isn't an active member; the sync never changes them")

    if gone and not allow_mass_suspend:
        if not any(p.directory_subject in active for p in managed):
            report.stopped = (
                f"every managed person would be suspended: none came back as an active member of "
                f"{settings.directory_sync_group}. Check the realm and the group, then run "
                "`openexecutive directory-sync --allow-mass-suspend`."
            )
            return
        if why := _brake(settings, len(gone), sum(1 for p in team if not p.suspended)):
            report.stopped = why
            return

    for member in sorted(active.values(), key=lambda m: m.email):
        before = len(report.changes)
        try:
            linked = by_subject.get(member.id)
            if linked is None:
                _add(member, report)
            else:
                _update(linked, member, report)
        except Exception as error:
            # One person's failure leaves the others to run, and what it said
            # it would do for them didn't happen: only the failure stands.
            del report.changes[before:]
            report.add("failed", member.email, type(error).__name__)
        if len(report.changes) == before:
            report.unchanged += 1

    for person in gone:
        report.add("suspended", _who(person))
        if not report.dry_run:
            _suspend(person, settings)

    if report.wrote and not report.dry_run:
        from openexecutive.integrations.roster_intake import after_roster_write
        from openexecutive.people import registry

        registry.invalidate()
        # A pending roster request from someone just added is matched now.
        try:
            after_roster_write()
        except Exception:
            logger.warning("directory_sync: roster intake reconcile failed", exc_info=True)


def _add(member: Member, report: Report) -> None:
    from openexecutive.people import store

    if not member.email:
        return
    holder_id = store.address_holder(member.email)
    if holder_id is not None:
        holder = store.get_person(holder_id)
        if holder is None:
            return
        if holder.directory_managed:
            report.add("conflict", member.email, "the address is another directory identity's")
        elif holder.is_principal and store.normalize_email(holder.email or "") == member.email:
            # The principal is linked by their verified email, never changed.
            report.add("linked", member.email, "the principal")
            if not report.dry_run and holder.id is not None:
                store.link_directory(holder.id, member.id)
                _audit("directory_person_linked", f"person #{holder.id} linked to the directory",
                       {"person_id": holder.id})
        else:
            report.add("candidate", member.email, f"link with `openexecutive directory-link --email {member.email}`")
        return
    report.add("created", member.email)
    if report.dry_run:
        return
    pid = store.upsert_person(full_name=member.full_name, email=member.email, kind="team")
    try:
        store.link_directory(pid, member.id)
    except Exception:
        # Never leave an unlinked row behind: it would admit their email on
        # any sign-in, unchecked by the directory.
        store.archive_person(pid)
        raise
    _audit("directory_person_created", f"person #{pid} created from the directory", {"person_id": pid})


def _update(person: Person, member: Member, report: Report) -> None:
    from openexecutive.people import store

    assert person.id is not None
    if person.is_principal:
        return
    if person.kind != "team":
        report.add("skipped", _who(person), "a contact; the directory manages team members only")
        return
    fields: dict[str, str] = {}
    if member.full_name and member.full_name != person.full_name:
        fields["full_name"] = member.full_name
    if member.email and member.email != store.normalize_email(person.email or ""):
        if store.address_holder(member.email, exclude_person_id=person.id) is not None:
            report.add("conflict", member.email, f"their new address is already on someone else; kept {_who(person)}")
        else:
            fields["email"] = member.email
    if fields:
        report.add("updated", member.email, ", ".join(sorted(fields)))
        if not report.dry_run:
            store.update_person(person.id, **fields)  # type: ignore[arg-type]
            _audit("directory_person_updated", f"person #{person.id} updated from the directory",
                   {"person_id": person.id, "fields": sorted(fields)})
    if person.suspended:
        if person.suspended_by == "directory":
            report.add("restored", member.email)
            if not report.dry_run:
                store.restore_person(person.id, only_by="directory")
                _audit("directory_person_restored", f"person #{person.id} restored from the directory",
                       {"person_id": person.id})
        else:
            report.add("kept suspended", member.email, "suspended by the principal")


def _suspend(person: Person, settings: Settings) -> None:
    from openexecutive.memory import ended_sessions
    from openexecutive.people import store

    assert person.id is not None
    if not store.suspend_person(person.id, by="directory"):
        return
    # Every web session of theirs ends within seconds, whatever the provider
    # did: disabling or removing someone there signs them out of nothing.
    # The UI compares ``iss`` with AUTH_OIDC_ISSUER verbatim.
    if person.directory_subject:
        ended_sessions.record(
            iss=settings.auth_oidc_issuer.strip(),
            sid=None,
            sub=person.directory_subject,
            ended_at=time.time(),
            jti=f"directory-sync:{uuid.uuid4()}",
        )
    _audit("directory_person_suspended", f"person #{person.id} suspended by the directory",
           {"person_id": person.id})


# --------------------------------------------------------------------------- #
# Linking an existing Person
# --------------------------------------------------------------------------- #

def _holder(norm: str) -> Person | None:
    """The person whose primary address is ``norm``, a suspended one included."""
    from openexecutive.people import store

    holder_id = store.address_holder(norm)
    person = store.get_person(holder_id) if holder_id is not None else None
    if person is None or store.normalize_email(person.email or "") != norm:
        return None
    return person


def link(email: str, *, relink: bool = False, reader: DirectoryReader | None = None) -> str:
    """Link the team Person whose address is ``email`` to the active member
    with that email, so the sync manages them. ``relink`` re-points a
    managed row whose identity is gone from the group (the user, or the
    realm, was re-created) and restores it if the directory had suspended
    it. Returns what it did; raises ``DirectoryError`` when it can't."""
    from openexecutive.people import registry, store

    settings = get_settings()
    norm = store.normalize_email(email)
    person = _holder(norm)
    if person is None or person.id is None or person.kind != "team":
        raise DirectoryError(f"no team member has the address {norm}")
    if person.directory_managed and not relink:
        raise DirectoryError(
            f"{norm} is already managed by the directory (use --relink if their identity was re-created)"
        )
    if not person.directory_managed and not person.active:
        raise DirectoryError(f"{norm} is suspended; restore them first")
    if reader is None:
        if problem := config_problem(settings):
            raise DirectoryError(problem)
        reader = KeycloakReader(settings)
    members = reader.read_members()
    matches = [m for m in members.values() if m.active and m.email == norm]
    if len(matches) != 1:
        raise DirectoryError(f"no active member of {settings.directory_sync_group} has the address {norm}")
    if person.directory_managed:
        if person.directory_subject == matches[0].id:
            raise DirectoryError(f"{norm} is already linked to that identity")
        if person.directory_subject in members:
            raise DirectoryError(f"the identity {norm} is linked to is still in the group; nothing to re-link")
    try:
        store.link_directory(person.id, matches[0].id)
    except store.DirectoryLinkError as error:
        raise DirectoryError(str(error)) from None
    if person.suspended and person.suspended_by == "directory":
        store.restore_person(person.id, only_by="directory")
    registry.invalidate()
    _audit(
        "directory_person_linked",
        f"person #{person.id} linked to the directory",
        {"person_id": person.id, "relinked": person.directory_managed},
    )
    return f"Linked {person.full_name} ({norm}). The next run keeps their name and email in step."


def unlink(email: str) -> str:
    """Stop the directory managing the person whose address is ``email``.
    Their row stays as it is, editable here, and sign-in no longer checks
    their identity at the provider. Raises ``DirectoryError``."""
    from openexecutive.people import registry, store

    norm = store.normalize_email(email)
    person = _holder(norm)
    if person is None or person.id is None or not person.directory_managed:
        raise DirectoryError(f"nobody the directory manages has the address {norm}")
    store.unlink_directory(person.id)
    registry.invalidate()
    _audit("directory_person_unlinked", f"person #{person.id} unlinked from the directory", {"person_id": person.id})
    tail = " They stay suspended until you restore them in People." if person.suspended else ""
    return f"Unlinked {person.full_name} ({norm}).{tail}"


# --------------------------------------------------------------------------- #
# Status (Settings → Setup status)
# --------------------------------------------------------------------------- #

def _status_conn(db_path: Path | None = None) -> sqlite3.Connection:
    from openexecutive.memory.episodic import _resolve_db_path

    conn = sqlite3.connect(str(_resolve_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {STATUS_TABLE} ("
        " id INTEGER PRIMARY KEY CHECK (id = 1),"
        " last_run_at TEXT, last_ok_at TEXT, stopped TEXT, counts_json TEXT NOT NULL DEFAULT '{}')"
    )
    return conn


def _record_status(report: Report, db_path: Path | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    try:
        conn = _status_conn(db_path)
        try:
            with conn:
                conn.execute(
                    f"INSERT INTO {STATUS_TABLE} (id, last_run_at, last_ok_at, stopped, counts_json)"
                    " VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET"
                    " last_run_at = excluded.last_run_at,"
                    " last_ok_at = COALESCE(excluded.last_ok_at, last_ok_at),"
                    " stopped = excluded.stopped, counts_json = excluded.counts_json",
                    (now, None if report.stopped else now, report.stopped, json.dumps(report.counts())),
                )
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("directory_sync: recording the status failed", exc_info=True)


def status(db_path: Path | None = None) -> dict[str, Any] | None:
    """The last run: ``last_run_at``, ``last_ok_at`` (a run that read the
    whole directory and finished), ``stopped`` and ``counts``; None before
    the first."""
    conn = _status_conn(db_path)
    try:
        row = conn.execute(f"SELECT * FROM {STATUS_TABLE} WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        counts = json.loads(row["counts_json"] or "{}")
    except ValueError:
        counts = {}
    return {
        "last_run_at": row["last_run_at"],
        "last_ok_at": row["last_ok_at"],
        "stopped": row["stopped"],
        "counts": counts if isinstance(counts, dict) else {},
    }


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #

def _heartbeat_pending(db_path: Path | None = None) -> bool:
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path

    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return False
    with _get_conn(resolved) as conn:
        row = conn.execute(
            "SELECT 1 FROM scheduled_actions WHERE kind = ? AND status IN ('pending', 'running') LIMIT 1",
            (HEARTBEAT_KIND,),
        ).fetchone()
    return row is not None


def _enqueue(run_at: datetime, db_path: Path | None) -> int | None:
    from openexecutive.memory.episodic import insert_scheduled_action

    try:
        return insert_scheduled_action(
            run_at=run_at.isoformat(),
            channel=HEARTBEAT_CHANNEL,
            channel_ref=HEARTBEAT_CHANNEL_REF,
            intent_text=HEARTBEAT_INTENT,
            kind=HEARTBEAT_KIND,
            db_path=db_path,
        )
    except Exception:
        logger.exception("directory_sync: failed to enqueue the heartbeat")
        return None


def bootstrap_directory_sync_scan(db_path: Path | None = None) -> int | None:
    if _heartbeat_pending(db_path):
        return None
    return _enqueue(datetime.now(UTC) + timedelta(minutes=1), db_path)


def enqueue_next_directory_sync_scan(
    *, after: datetime | None = None, db_path: Path | None = None
) -> int | None:
    settings = get_settings()
    if not settings.directory_sync_enabled:
        # Turned off: the heartbeat ends here, and startup starts it again
        # once it is back on.
        return None
    base = (after or datetime.now(UTC)).astimezone(UTC)
    return _enqueue(base + timedelta(minutes=settings.directory_sync_interval_minutes), db_path)


async def run_scheduled(run: Callable[..., Report] = sync) -> Report | None:
    """One scheduled run, off the event loop (the reads and the store are
    blocking). Nothing while the sync is off."""
    if not get_settings().directory_sync_enabled:
        return None
    report = await asyncio.to_thread(run)
    if report.stopped:
        logger.warning("directory_sync: stopped, nothing was changed: %s", report.stopped)
    elif report.wrote:
        logger.info("directory_sync: %s", report.counts())
    return report
