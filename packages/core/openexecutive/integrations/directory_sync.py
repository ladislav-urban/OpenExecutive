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
import dataclasses
import hashlib
import ipaddress
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
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
WRITES = ("created", "updated", "linked", "suspended", "restored", "profile updated")

_run_lock = threading.Lock()
_token_lock = threading.Lock()
_tokens: dict[tuple[str, str], tuple[str, float]] = {}


class DirectoryError(Exception):
    """Why a run stopped. It changed nothing."""


class _NotFound(DirectoryError):
    """The admin API answered 404 for something other than a group path."""


@dataclass(frozen=True)
class Member:
    id: str
    email: str
    full_name: str
    active: bool
    # Also a member of DIRECTORY_SYNC_DEMO_GROUP.
    demo: bool = False
    # The provider's user attributes, name -> first value, kept only for the
    # business profile (people.profile), which checks every value itself.
    attributes: tuple[tuple[str, str], ...] = ()


class DirectoryReader(Protocol):
    """Reads one group's members, each marked demo when also in the demo
    group. Keycloak today; Entra ID or Google Workspace can be added beside
    it."""

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
    if not _group_path_ok(group):
        return "DIRECTORY_SYNC_GROUP must be a group path, e.g. /dynocortex"
    demo = settings.directory_sync_demo_group.strip()
    if demo and not _group_path_ok(demo):
        return "DIRECTORY_SYNC_DEMO_GROUP must be a group path, e.g. /erpnext/demo, or empty"
    if demo and (demo == group or group.startswith(demo + "/")):
        return "DIRECTORY_SYNC_DEMO_GROUP must not be DIRECTORY_SYNC_GROUP or a group holding it"
    return None


def _group_path_ok(group: str) -> bool:
    return group.startswith("/") and group != "/" and "//" not in group and not group.endswith("/")


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

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = TIMEOUT_S,
        budget_s: float | None = None,
    ) -> None:
        self.issuer = settings.directory_sync_issuer.strip().rstrip("/")
        self.admin_url = settings.directory_sync_admin_url.strip().rstrip("/")
        self.realm = _realm(self.issuer)
        self.client_id = settings.directory_sync_client_id.strip()
        self._secret = settings.directory_sync_client_secret
        self.group = settings.directory_sync_group.strip()
        self.demo_group = settings.directory_sync_demo_group.strip()
        # The group being read, for a "doesn't exist" message.
        self._reading = self.group
        self._client = httpx.Client(
            timeout=timeout, follow_redirects=False, transport=transport
        )
        self._fresh = False
        # An admit's reads share one time budget (``budget_s``): each call is
        # capped at ``timeout`` and at what is left of it. A run has none.
        self._call_timeout = timeout
        self._deadline = time.monotonic() + budget_s if budget_s is not None else None

    def _timeout(self) -> dict[str, Any]:
        """The per-request timeout under the budget, or nothing (the client's)."""
        if self._deadline is None:
            return {}
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise DirectoryError("the time for the reads ran out")
        return {"timeout": min(self._call_timeout, left)}

    def close(self) -> None:
        self._client.close()

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
                url, data={"grant_type": "client_credentials"}, auth=(self.client_id, self._secret),
                **self._timeout(),
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
                    url, params=params, headers={"Authorization": f"Bearer {token}"}, **self._timeout()
                )
            except httpx.HTTPError as error:
                raise DirectoryError(f"the admin API can't be reached: {type(error).__name__}") from None
            if response.status_code != 401 or self._fresh:
                break
            self._fresh = True
            token = self._token(fresh=True)
        if response.status_code == 404 and path.startswith("group-by-path/"):
            raise DirectoryError(f"the group {self._reading} doesn't exist in the realm")
        if response.status_code == 404:
            raise _NotFound(f"the admin API answered 404 for {path.split('/')[0]}")
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
        """Every member of the group and the groups under it, by user id,
        marked demo when also in the demo group or one under it."""
        try:
            found = self._group_users(self.group)
            demo = set(self._group_users(self.demo_group)) if self.demo_group else set()
        finally:
            self._client.close()
        return {uid: _member(uid, user, demo=uid in demo) for uid, user in found.items()}

    def _group_tree(self, group_path: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """The group at ``group_path`` and every group under it. Past
        ``limit`` groups the read stops (an admit's bound)."""
        self._reading = group_path
        path = "/".join(quote(part, safe="") for part in group_path.strip("/").split("/"))
        groups, todo = [], [self._get(f"group-by-path/{path}")]
        while todo:
            group = todo.pop()
            groups.append(group)
            todo += self._paged(f"groups/{quote(group['id'])}/children", briefRepresentation="true")
            if limit is not None and len(groups) + len(todo) > limit:
                raise DirectoryError(f"the group {group_path} holds more than {limit} groups")
        return groups

    def _group_users(self, group_path: str) -> dict[str, dict[str, Any]]:
        groups = self._group_tree(group_path)
        found: dict[str, dict[str, Any]] = {}
        for group in groups:
            for user in self._paged(f"groups/{quote(group['id'])}/members", briefRepresentation="false"):
                found[str(user["id"])] = user
        return found


def _member(uid: str, user: dict[str, Any], *, demo: bool = False) -> Member:
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
        demo=demo,
        attributes=_attributes(user.get("attributes")),
    )


# A profile value is a short code; anything longer is no code, and dropped.
_ATTRIBUTE_MAX = 40


def _attributes(raw: Any) -> tuple[tuple[str, str], ...]:
    """A Keycloak user's ``attributes`` (name -> list of values) as name ->
    first value, short strings only."""
    if not isinstance(raw, dict):
        return ()
    out: list[tuple[str, str]] = []
    for name, values in raw.items():
        first = values[0] if isinstance(values, list) and values else values
        if isinstance(name, str) and isinstance(first, str) and len(first) <= _ATTRIBUTE_MAX:
            out.append((name, first))
    return tuple(sorted(out))


def _keep_profile(pid: int, member: Member, report: Report) -> None:
    """Keep the member's business profile when profiles are on
    (DIRECTORY_PROFILES_ENABLED; people.profile checks every value)."""
    settings = get_settings()
    if not settings.directory_profiles_enabled or report.dry_run:
        return
    from openexecutive.people import profile

    names = {
        "country": settings.directory_profile_attr_country,
        "business_type": settings.directory_profile_attr_business_type,
        "industry": settings.directory_profile_attr_industry,
        "jurisdiction": settings.directory_profile_attr_jurisdiction,
    }
    # Its own failure never undoes what the run did for the person, nor stops
    # what follows for them (a restore): only the profile is left as it was.
    try:
        changed = profile.set_profile(pid, profile.from_attributes(dict(member.attributes), names))
    except Exception as error:
        logger.warning("directory sync: keeping a profile failed for person %s", pid, exc_info=True)
        report.add("warning", member.email, f"profile not kept ({type(error).__name__})")
        return
    if changed:
        report.add("profile updated", member.email)


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


def _brake(settings: Settings, suspend: int, population: int, demo_suspend: int = 0) -> str | None:
    """Why the run must stop before suspending ``suspend`` of the
    ``population`` team people it manages and ``demo_suspend`` demo people,
    or None. Demo accesses end in batches, so demo people have a brake of
    their own and don't count toward the team's."""
    if demo_suspend > settings.directory_sync_demo_max_suspend:
        return (
            f"it would suspend {demo_suspend} demo people, more than DIRECTORY_SYNC_DEMO_MAX_SUSPEND "
            f"({settings.directory_sync_demo_max_suspend}). Check the realm and the groups, then run "
            "`openexecutive directory-sync --allow-mass-suspend`."
        )
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
        gone_demo = sum(1 for p in gone if p.demo)
        population = sum(1 for p in team if not p.suspended and not p.demo)
        if why := _brake(settings, len(gone) - gone_demo, population, gone_demo):
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


def _add(member: Member, report: Report, *, admit: bool = False) -> int | None:
    """Create a Person for ``member``, or report why not. Returns the new
    Person's id when one was created. With ``admit`` (``admit_one``) it never
    links anyone: an address someone holds is left to the run."""
    from openexecutive.people import store

    if not member.email:
        return None
    holder_id = store.address_holder(member.email)
    if holder_id is not None and admit:
        report.add("conflict", member.email, "the address is held; left to the run")
        return None
    if holder_id is not None:
        holder = store.get_person(holder_id)
        if holder is None:
            return None
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
        return None
    report.add("created", member.email, "demo" if member.demo else "")
    if report.dry_run:
        return None
    # A demo person starts with the demo role and nothing else: no
    # departments, scopes or manager. Set on create only, so the principal
    # may change it.
    role = get_settings().directory_sync_demo_role.strip() if member.demo else ""
    pid = store.upsert_person(full_name=member.full_name, role=role, email=member.email, kind="team")
    try:
        store.link_directory(pid, member.id)
        if member.demo:
            store.set_directory_demo(pid, True)
    except Exception:
        # Never leave an unlinked row behind: it would admit their email on
        # any sign-in, unchecked by the directory.
        store.archive_person(pid)
        raise
    _audit("directory_person_created", f"person #{pid} created from the directory", {"person_id": pid})
    _keep_profile(pid, member, report)
    return pid


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
    # The demo mark is set when someone is created and changes only by hand
    # (`openexecutive directory-demo`), never by the run: marking an existing
    # team member demo would take their approvals and mail away and move
    # their later suspension onto the demo brake, and unmarking a demo person
    # would hand a stranger the team's tools, uncapped. Either way one change
    # to a group's members (or a short answer for it) would do it.
    _keep_profile(person.id, member, report)
    if person.demo and not member.demo:
        report.add("warning", member.email, "left the demo group; still a demo person (openexecutive directory-demo --off)")
    elif member.demo and not person.demo:
        report.add("warning", member.email, "in the demo group but already on the team; kept as a team member")
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
# Admitting one person at sign-in
# --------------------------------------------------------------------------- #
#
# The web app's sign-in refuses someone the roster doesn't hold yet: an
# approved member waits for the next run. ``admit_one`` does for that one
# person what the next run would do, and nothing else: it creates a Person
# for an active member of the group, or does nothing. It never links,
# restores, suspends, changes or reads anyone else, and it believes nothing
# the web app sends beyond who to look up: every fact comes from the admin
# API. The reads happen before ``_run_lock``; the lock is held only for the
# store's re-checks and the write, so a heartbeat run is never kept waiting
# on the network.

ADMIT_CALL_TIMEOUT_S = 3.0
# The reads' budget plus the lock wait stay under the web app's 4 s wait for
# the answer, so the API never admits someone after the web app gave up.
ADMIT_BUDGET_S = 3.0
ADMIT_LOCK_WAIT_S = 0.5
ADMIT_SUBJECT_WINDOW_S = 30.0
ADMIT_SUBJECT_CAP = 10_000
ADMIT_GLOBAL_PER_MINUTE = 20
# A group tree an admit reads is kept this long, and holds at most this many
# groups (more answers ``error``).
ADMIT_TREE_TTL_S = 60.0
ADMIT_TREE_MAX_GROUPS = 50

_SUBJECT_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+")
_SUBJECT_MAX = 64
_EMAIL_MAX = 254

# What an admit answers. Only ``admitted`` lets anyone in.
ADMIT_CODES = (
    "admitted",       # a Person was created for them
    "exists",         # a Person is already linked to them (left as it is)
    "address_held",   # someone already holds the address (left to the run)
    "unknown",        # no such user at the provider
    "inactive",       # disabled, email not verified, or no email
    "email_mismatch", # the provider has another email for them
    "not_member",     # not in DIRECTORY_SYNC_GROUP
    "invalid",        # the subject or email isn't the right shape
    "disabled",       # the sync is off, or its settings can't be used
    "busy",           # another admit or a run holds the way
    "rate_limited",   # too many admits lately
    "error",          # a read failed or answered the wrong shape
)


@dataclass(frozen=True)
class AdmitResult:
    code: str
    person_id: int | None = None

    @property
    def admitted(self) -> bool:
        return self.code == "admitted"


class _AdmitLimits:
    """In-process limits: one admit per subject per window, and a global cap
    per minute counting only admits that reached the provider."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subjects: dict[str, float] = {}
        self._recent: deque[float] = deque()
        self.refused = {"busy": 0, "rate_limited": 0}
        self.clock: Callable[[], float] = time.monotonic

    def reset(self) -> None:
        with self._lock:
            self._subjects.clear()
            self._recent.clear()
            self.refused = {"busy": 0, "rate_limited": 0}

    def _prune(self, now: float) -> None:
        old = [s for s, at in self._subjects.items() if not 0 <= now - at < ADMIT_SUBJECT_WINDOW_S]
        for subject in old:
            del self._subjects[subject]
        while self._recent and not 0 <= now - self._recent[0] < 60.0:
            self._recent.popleft()

    def subject_ok(self, subject: str) -> bool:
        """Whether this subject may be looked up now. At the cap, a new
        subject is refused rather than a live entry evicted."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            if subject in self._subjects:
                return False
            return len(self._subjects) < ADMIT_SUBJECT_CAP

    def take(self, subject: str) -> bool:
        """Count an admit about to reach the provider; False over a limit."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            if subject in self._subjects or len(self._subjects) >= ADMIT_SUBJECT_CAP:
                return False
            if len(self._recent) >= ADMIT_GLOBAL_PER_MINUTE:
                return False
            self._subjects[subject] = now
            self._recent.append(now)
            return True

    def note_refused(self, code: str, digest: str) -> None:
        with self._lock:
            self.refused[code] = self.refused.get(code, 0) + 1
            count = self.refused[code]
        logger.warning("directory admit: %s for subject %s (%d so far)", code, digest, count)


_admit_limits = _AdmitLimits()
# One admit in flight in this process; a second answers ``busy`` at once.
_admit_slot = threading.Lock()
_tree_lock = threading.Lock()
_trees: dict[tuple[str, str, str], tuple[frozenset[str], float]] = {}


def _subject_digest(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:12]


def _under(path: str, group: str) -> bool:
    return path == group or path.startswith(group + "/")


def _tree_ids(reader: KeycloakReader, group_path: str) -> frozenset[str]:
    """The ids of the group at ``group_path`` and every group under it, as
    the run's tree walk reads them. Kept ``ADMIT_TREE_TTL_S``; a failed read
    is never kept."""
    key = (reader.issuer, reader.admin_url, group_path)
    now = time.monotonic()
    with _tree_lock:
        kept = _trees.get(key)
        if kept and 0 < kept[1] - now <= ADMIT_TREE_TTL_S:
            return kept[0]
    groups = reader._group_tree(group_path, limit=ADMIT_TREE_MAX_GROUPS)
    ids = frozenset(str(g["id"]) for g in groups if isinstance(g.get("id"), str))
    if len(ids) != len(groups):
        raise ValueError("a group without a usable id")
    with _tree_lock:
        _trees[key] = (ids, time.monotonic() + ADMIT_TREE_TTL_S)
    return ids


def _user_groups(reader: KeycloakReader, subject: str) -> list[tuple[str, str]]:
    """(id, path) of every group the user is directly in."""
    groups = reader._paged(f"users/{quote(subject, safe='')}/groups", briefRepresentation="false")
    out = []
    for group in groups:
        gid, path = group.get("id"), group.get("path")
        if not isinstance(gid, str) or not isinstance(path, str):
            raise ValueError("a group without an id or path")
        out.append((gid, path))
    return out


def _read_member(settings: Settings, subject: str, email: str,
                 transport: httpx.BaseTransport | None) -> Member | AdmitResult:
    """The member ``subject`` is, read from the provider, or why not. Holds
    no lock: every call here is network."""
    reader = KeycloakReader(
        settings, transport=transport, timeout=ADMIT_CALL_TIMEOUT_S, budget_s=ADMIT_BUDGET_S
    )
    try:
        try:
            user = reader._get(f"users/{quote(subject, safe='')}")
        except _NotFound:
            return AdmitResult("unknown")
        if not isinstance(user, dict) or user.get("id") != subject:
            return AdmitResult("error")
        member = _member(subject, user)
        if not member.active:
            return AdmitResult("inactive")
        if member.email != email:
            return AdmitResult("email_mismatch")
        groups = _user_groups(reader, subject)
        # A member by both signals: a path at or under the group, and that
        # group's id in the group's tree. A moved-out group fails the path, a
        # root group named like a path ("/dynocortex/x") fails the id.
        in_path = [gid for gid, path in groups if _under(path, reader.group)]
        if not in_path:
            return AdmitResult("not_member")
        tree = _tree_ids(reader, reader.group)
        if not any(gid in tree for gid in in_path):
            return AdmitResult("not_member")
        # Demo by either signal: the narrower role wins a disagreement.
        demo = False
        if reader.demo_group:
            demo = any(_under(path, reader.demo_group) for _, path in groups) or bool(
                {gid for gid, _ in groups} & _tree_ids(reader, reader.demo_group)
            )
        return dataclasses.replace(member, demo=demo)
    finally:
        reader.close()


def _admit_write(member: Member) -> AdmitResult:
    """Under ``_run_lock`` (a short wait, else ``busy``): the store's
    re-checks and the write, as the run's ``_add`` does it."""
    from openexecutive.people import registry, store

    if not _run_lock.acquire(timeout=ADMIT_LOCK_WAIT_S):
        return AdmitResult("busy")
    try:
        linked = store.find_person_by_directory_subject(member.id)
        if linked is not None:
            return AdmitResult("exists", linked.id)
        if store.address_holder(member.email) is not None:
            return AdmitResult("address_held")
        report = Report(dry_run=False)
        pid = _add(member, report, admit=True)
        if pid is None or report.count("created") != 1:
            return AdmitResult("error")
        registry.invalidate()
    finally:
        _run_lock.release()
    from openexecutive.integrations.roster_intake import after_roster_write

    # A pending roster request from them is matched now, as after a run.
    try:
        after_roster_write()
    except Exception:
        logger.warning("directory admit: roster intake reconcile failed", exc_info=True)
    return AdmitResult("admitted", pid)


def admit_one(
    subject: str, email: str, *, transport: httpx.BaseTransport | None = None
) -> AdmitResult:
    """Admit the one person signing in as ``subject`` with ``email`` if the
    provider says they are an active member of the group: what the next run
    would do for them. Raises nothing; writes nothing unless it answers
    ``admitted``. ``transport`` replaces the network (tests)."""
    from openexecutive.people import store

    settings = get_settings()
    if not settings.directory_sync_enabled or config_problem(settings):
        return AdmitResult("disabled")
    if not isinstance(subject, str) or len(subject) > _SUBJECT_MAX or not _SUBJECT_RE.fullmatch(subject):
        return AdmitResult("invalid")
    norm = store.normalize_email(email) if isinstance(email, str) else ""
    if len(norm) > _EMAIL_MAX or not _EMAIL_RE.fullmatch(norm):
        return AdmitResult("invalid")
    try:
        linked = store.find_person_by_directory_subject(subject)
        if linked is not None:
            # A suspended one stays so; an archived one doesn't count.
            return AdmitResult("exists", linked.id)
        if store.address_holder(norm) is not None:
            # Read again: a sign-in of the same person at the same moment may
            # have admitted them between the two reads (the run's _add writes
            # the Person and its subject together), and they then exist.
            again = store.find_person_by_directory_subject(subject)
            if again is not None:
                return AdmitResult("exists", again.id)
            return AdmitResult("address_held")
    except Exception as error:
        logger.error("directory admit: the store check failed (%s)", type(error).__name__)
        return AdmitResult("error")

    digest = _subject_digest(subject)
    if not _admit_limits.subject_ok(subject):
        _admit_limits.note_refused("rate_limited", digest)
        return AdmitResult("rate_limited")
    if not _admit_slot.acquire(blocking=False):
        _admit_limits.note_refused("busy", digest)
        return AdmitResult("busy")
    try:
        if not _admit_limits.take(subject):
            _admit_limits.note_refused("rate_limited", digest)
            return AdmitResult("rate_limited")
        try:
            found = _read_member(settings, subject, norm, transport)
            result = found if isinstance(found, AdmitResult) else _admit_write(found)
        except DirectoryError:
            result = AdmitResult("error")
        except Exception as error:
            # Never a traceback with the token or secret in its locals.
            logger.error("directory admit: failed (%s)", type(error).__name__)
            result = AdmitResult("error")
    finally:
        _admit_slot.release()
    if result.code in ("busy", "rate_limited"):
        _admit_limits.note_refused(result.code, digest)
    else:
        _audit(
            "directory_admit",
            f"directory admit at sign-in: {result.code}",
            {
                "code": result.code,
                "subject_sha256": digest,
                "email_domain": norm.rsplit("@", 1)[-1],
                "person_id": result.person_id,
            },
        )
    return result


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
    try:
        members = reader.read_members()
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise DirectoryError(
            f"the provider's answer isn't the shape the directory contract says ({type(error).__name__})"
        ) from None
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
    # A member of the demo group is linked as what they are: a demo person.
    demo = matches[0].demo
    if demo:
        store.set_directory_demo(person.id, True)
    if person.suspended and person.suspended_by == "directory":
        store.restore_person(person.id, only_by="directory")
    registry.invalidate()
    _audit(
        "directory_person_linked",
        f"person #{person.id} linked to the directory",
        {"person_id": person.id, "relinked": person.directory_managed, "demo": demo},
    )
    tail = " They are in the demo group, so they are a demo person." if demo else ""
    return f"Linked {person.full_name} ({norm}). The next run keeps their name and email in step.{tail}"


def set_demo(email: str, demo: bool) -> str:
    """Mark the person the directory manages at ``email`` a demo person, or
    a team member (``demo`` False). The run never changes it, so this is the
    one way, besides creation. Raises ``DirectoryError``."""
    from openexecutive.people import registry, store

    norm = store.normalize_email(email)
    person = _holder(norm)
    if person is None or person.id is None or not person.directory_managed or person.kind != "team":
        raise DirectoryError(f"no team member the directory manages has the address {norm}")
    if person.is_principal:
        raise DirectoryError("the principal is never a demo person")
    if not store.set_directory_demo(person.id, demo):
        return f"{person.full_name} ({norm}) is already {'a demo person' if demo else 'a team member'}."
    registry.invalidate()
    _audit("directory_person_updated", f"person #{person.id} marked {'demo' if demo else 'team'} by hand",
           {"person_id": person.id, "fields": ["demo"], "demo": demo, "by": "operator"})
    return f"{person.full_name} ({norm}) is now {'a demo person' if demo else 'a team member'}."


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
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({STATUS_TABLE})")}
    if "candidates_json" not in cols:
        try:
            conn.execute(f"ALTER TABLE {STATUS_TABLE} ADD COLUMN candidates_json TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError as exc:
            # Another connection added it first.
            if "duplicate column" not in str(exc).lower():
                raise
    return conn


def _record_status(report: Report, db_path: Path | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    try:
        conn = _status_conn(db_path)
        try:
            with conn:
                # The link candidates of a finished run; a stopped run read
                # nothing reliable, so the last ones are kept.
                candidates = (
                    None if report.stopped
                    else json.dumps(sorted({who for a, who, _ in report.changes if a == "candidate"})[:100])
                )
                conn.execute(
                    f"INSERT INTO {STATUS_TABLE}"
                    " (id, last_run_at, last_ok_at, stopped, counts_json, candidates_json)"
                    " VALUES (1, ?, ?, ?, ?, COALESCE(?, '[]')) ON CONFLICT(id) DO UPDATE SET"
                    " last_run_at = excluded.last_run_at,"
                    " last_ok_at = COALESCE(excluded.last_ok_at, last_ok_at),"
                    " stopped = excluded.stopped, counts_json = excluded.counts_json,"
                    " candidates_json = COALESCE(?, candidates_json)",
                    (now, None if report.stopped else now, report.stopped, json.dumps(report.counts()),
                     candidates, candidates),
                )
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("directory_sync: recording the status failed", exc_info=True)


def status(db_path: Path | None = None) -> dict[str, Any] | None:
    """The last run: ``last_run_at``, ``last_ok_at`` (a run that read the
    whole directory and finished), ``stopped``, ``counts`` and
    ``candidates`` (the addresses of unmanaged People who are active
    members, from the last finished run); None before the first."""
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
    try:
        candidates = json.loads(row["candidates_json"] or "[]")
    except ValueError:
        candidates = []
    return {
        "candidates": [c for c in candidates if isinstance(c, str)] if isinstance(candidates, list) else [],
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
