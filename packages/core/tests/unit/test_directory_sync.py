"""The directory sync (integrations/directory_sync.py) against a stubbed
Keycloak admin API serving the directory contract's shared test vector,
tests/fixtures/directory/realm.json (the same file as oe_connector's
oe_connector/tests/fixtures/directory/realm.json; change both together)."""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from openexecutive.config import get_settings
from openexecutive.integrations import directory_sync
from openexecutive.memory import ended_sessions, episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "directory" / "realm.json"
ISSUER = "https://id.example.com/realms/dynocortex"
ADMIN = "http://keycloak:8180"


def _jwt(claims: dict[str, Any]) -> str:
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class Keycloak:
    """Just enough of the admin API: the token endpoint, group-by-path, a
    group's children and its members, paged. Edit ``users`` to change the
    realm between runs."""

    def __init__(self) -> None:
        realm = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.users: list[dict[str, Any]] = copy.deepcopy(realm["users"])
        self.groups = {g["path"]: g for g in self._walk(realm["groups"])}
        self.issuer = ISSUER
        self.fail_members = False
        self.token_requests = 0

    def _walk(self, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for group in groups:
            out.append(group)
            out += self._walk(group.get("subGroups") or [])
        return out

    def user(self, username: str) -> dict[str, Any]:
        return next(u for u in self.users if u["username"] == username)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/realms/dynocortex/protocol/openid-connect/token":
            self.token_requests += 1
            token = _jwt({"iss": self.issuer})
            return httpx.Response(200, json={"access_token": token, "expires_in": 300})
        assert request.headers["authorization"].startswith("Bearer ")
        prefix = "/admin/realms/dynocortex/"
        assert path.startswith(prefix), path
        rest = path[len(prefix):]
        if rest.startswith("group-by-path/"):
            group = self.groups.get("/" + rest[len("group-by-path/"):])
            return httpx.Response(200, json={"id": group["path"], "path": group["path"]}) if group else httpx.Response(404)
        _, gid_and_kind = rest.split("groups/", 1)
        gid, kind = gid_and_kind.rsplit("/", 1)
        gid = httpx.URL("http://x/" + gid).path[1:]  # unquote
        first, size = int(request.url.params["first"]), int(request.url.params["max"])
        if kind == "children":
            items = [{"id": g["path"], "path": g["path"]} for g in self.groups[gid].get("subGroups") or []]
        else:
            if self.fail_members:
                return httpx.Response(500)
            items = [
                {k: v for k, v in u.items() if k != "groups"} for u in self.users if gid in u["groups"]
            ]
        return httpx.Response(200, json=items[first:first + size])


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    db = tmp_path / "test.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    people_registry.invalidate()
    directory_sync._tokens.clear()
    for name, value in {
        "DIRECTORY_SYNC_ENABLED": "true",
        "DIRECTORY_SYNC_ISSUER": ISSUER,
        "AUTH_OIDC_ISSUER": ISSUER,
        "DIRECTORY_SYNC_ADMIN_URL": ADMIN,
        "DIRECTORY_SYNC_CLIENT_ID": "openexecutive-directory",
        "DIRECTORY_SYNC_CLIENT_SECRET": "s3cret",  # gitleaks:allow
        "DIRECTORY_SYNC_GROUP": "/dynocortex",
        "DIRECTORY_SYNC_MAX_SUSPEND": "3",
        "DIRECTORY_SYNC_MAX_SHARE": "0.2",
    }.items():
        monkeypatch.setenv(name, value)
    audited: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: audited.append((event_type, kw.get("details") or {})),
    )
    monkeypatch.setattr("openexecutive.integrations.roster_intake.after_roster_write", lambda: None)
    yield audited
    people_registry.invalidate()


@pytest.fixture
def kc() -> Keycloak:
    return Keycloak()


@pytest.fixture
def principal() -> int:
    return people_store.upsert_person(
        full_name="Olivia Owner", is_principal=True, email="dir-owner@example.com"
    )


def run(kc: Keycloak, **kwargs: Any) -> directory_sync.Report:
    reader = directory_sync.KeycloakReader(get_settings(), transport=httpx.MockTransport(kc.handle))
    return directory_sync.sync(reader=reader, **kwargs)


def by_email(email: str) -> Any:
    return next(
        (p for p in people_store.list_people(include_suspended=True, include_contacts=True) if p.email == email),
        None,
    )


# --------------------------------------------------------------------------- #
# Reading the realm
# --------------------------------------------------------------------------- #

def test_it_reads_the_groups_members_and_who_is_active(kc: Keycloak) -> None:
    reader = directory_sync.KeycloakReader(get_settings(), transport=httpx.MockTransport(kc.handle))
    members = reader.read_members()
    assert {m.email for m in members.values()} == {
        "dir-owner@example.com", "dir-accountant@example.com", "dir-adviser@example.com",
        "dir-disabled@example.com", "dir-unverified@example.com",
    }
    assert {m.email for m in members.values() if m.active} == {
        "dir-owner@example.com", "dir-accountant@example.com", "dir-adviser@example.com",
    }
    owner = next(m for m in members.values() if m.email == "dir-owner@example.com")
    assert owner.id == "0b6f9f0e-1c7a-4d1e-9a51-000000000001" and owner.full_name == "Olivia Owner"


# --------------------------------------------------------------------------- #
# Create, link, update
# --------------------------------------------------------------------------- #

def test_the_first_run_links_the_principal_and_creates_the_team(
    kc: Keycloak, principal: int, env: list[tuple[str, dict]]
) -> None:
    report = run(kc)
    assert report.stopped is None
    assert sorted((a, w) for a, w, _ in report.changes) == [
        ("created", "dir-accountant@example.com"),
        ("created", "dir-adviser@example.com"),
        ("linked", "dir-owner@example.com"),
    ]
    owner = people_store.get_person(principal)
    assert owner is not None and owner.directory_managed and owner.full_name == "Olivia Owner"
    accountant = by_email("dir-accountant@example.com")
    assert accountant.kind == "team" and accountant.directory_managed and accountant.role == ""
    assert by_email("dir-disabled@example.com") is None and by_email("dir-unverified@example.com") is None
    kinds = [t for t, _ in env]
    assert kinds.count("directory_person_created") == 2 and "directory_sync" in kinds

    second = run(kc)
    assert second.changes == [] and second.unchanged == 3


def test_name_and_email_follow_the_directory(kc: Keycloak, principal: int) -> None:
    run(kc)
    pid = by_email("dir-accountant@example.com").id
    kc.user("accountant").update(firstName="Anna", email="anna@example.com")
    report = run(kc)
    assert [(a, d) for a, _, d in report.changes] == [("updated", "email, full_name")]
    person = people_store.get_person(pid)
    assert person.email == "anna@example.com" and person.full_name.startswith("Anna")


def test_an_email_already_someone_elses_is_reported_not_taken(kc: Keycloak, principal: int) -> None:
    run(kc)
    pid = by_email("dir-accountant@example.com").id
    people_store.upsert_person(full_name="Someone", email="taken@example.com")
    kc.user("accountant")["email"] = "taken@example.com"
    report = run(kc)
    assert [a for a, _, _ in report.changes] == ["conflict"]
    assert people_store.get_person(pid).email == "dir-accountant@example.com"


def test_an_unmanaged_person_with_a_members_email_is_a_link_candidate(kc: Keycloak, principal: int) -> None:
    pid = people_store.upsert_person(full_name="Adam Adviser", email="dir-adviser@example.com", role="Adviser")
    report = run(kc)
    assert ("candidate", "dir-adviser@example.com") in [(a, w) for a, w, _ in report.changes]
    assert not people_store.get_person(pid).directory_managed

    reader = directory_sync.KeycloakReader(get_settings(), transport=httpx.MockTransport(kc.handle))
    assert "Linked" in directory_sync.link("dir-adviser@example.com", reader=reader)
    person = people_store.get_person(pid)
    assert person.directory_managed and person.role == "Adviser"
    assert ("candidate", "dir-adviser@example.com") not in [(a, w) for a, w, _ in run(kc).changes]


def test_contacts_are_never_managed(kc: Keycloak, principal: int) -> None:
    contact = people_store.upsert_person(full_name="Adam", email="dir-adviser@example.com", kind="contact")
    report = run(kc)
    assert ("candidate", "dir-adviser@example.com") in [(a, w) for a, w, _ in report.changes]
    assert people_store.get_person(contact).kind == "contact"


# --------------------------------------------------------------------------- #
# Suspend and restore
# --------------------------------------------------------------------------- #

def test_leaving_the_group_suspends_and_signs_them_out(kc: Keycloak, principal: int) -> None:
    run(kc)
    accountant = by_email("dir-accountant@example.com")
    kc.user("accountant")["groups"] = ["/erpnext/accounts"]
    report = run(kc)
    assert [(a, w) for a, w, _ in report.changes] == [("suspended", "dir-accountant@example.com")]
    person = people_store.get_person(accountant.id)
    assert person.suspended and person.suspended_by == "directory"
    ended = ended_sessions.recent()
    assert [(e["iss"], e["sub"]) for e in ended] == [(ISSUER, accountant.directory_subject)]

    kc.user("accountant")["groups"] = ["/dynocortex"]
    report = run(kc)
    assert [a for a, _, _ in report.changes] == ["restored"]
    assert people_store.get_person(accountant.id).active


def test_disabled_in_the_provider_counts_as_gone(kc: Keycloak, principal: int) -> None:
    run(kc)
    kc.user("adviser")["enabled"] = False
    assert [a for a, _, _ in run(kc).changes] == ["suspended"]


def test_the_principals_suspension_is_never_lifted(kc: Keycloak, principal: int) -> None:
    run(kc)
    pid = by_email("dir-adviser@example.com").id
    people_store.suspend_person(pid, by="principal")
    report = run(kc)
    assert ("kept suspended", "dir-adviser@example.com") in [(a, w) for a, w, _ in report.changes]
    assert people_store.get_person(pid).suspended_by == "principal"


def test_the_principal_is_never_changed(kc: Keycloak, principal: int) -> None:
    run(kc)
    kc.user("owner").update(enabled=False, firstName="Changed")
    report = run(kc)
    assert [a for a, _, _ in report.changes] == ["warning"]
    owner = people_store.get_person(principal)
    assert owner.active and owner.full_name == "Olivia Owner"


# --------------------------------------------------------------------------- #
# Stops: nothing changes
# --------------------------------------------------------------------------- #

def test_the_brake_stops_a_mass_suspension(kc: Keycloak, principal: int, monkeypatch: pytest.MonkeyPatch) -> None:
    run(kc)
    for n in range(4):
        pid = people_store.upsert_person(full_name=f"Extra {n}", email=f"x{n}@example.com")
        people_store.link_directory(pid, f"kc-extra-{n}")
    report = run(kc)
    assert report.stopped and "DIRECTORY_SYNC_MAX_SUSPEND" in report.stopped
    assert all(by_email(f"x{n}@example.com").active for n in range(4))
    # The share rule: 2 of 4 is over 20%.
    monkeypatch.setenv("DIRECTORY_SYNC_MAX_SUSPEND", "10")
    report = run(kc)
    assert report.stopped and "DIRECTORY_SYNC_MAX_SHARE" in report.stopped
    report = run(kc, allow_mass_suspend=True)
    assert report.stopped is None and report.count("suspended") == 4


def test_no_active_member_at_all_changes_nothing(kc: Keycloak, principal: int) -> None:
    run(kc)
    for user in kc.users:
        user["groups"] = []
    report = run(kc, allow_mass_suspend=False)
    assert report.stopped and "none came back" in report.stopped
    assert by_email("dir-accountant@example.com").active


def test_a_failed_page_changes_nothing(kc: Keycloak, principal: int) -> None:
    kc.fail_members = True
    report = run(kc)
    assert report.stopped and "500" in report.stopped
    assert people_store.list_people() == [people_store.get_person(principal)]


def test_a_token_from_another_realm_stops_the_run(kc: Keycloak, principal: int) -> None:
    kc.issuer = "https://id.example.com/realms/other"
    report = run(kc)
    assert report.stopped and "isn't from" in report.stopped
    assert by_email("dir-accountant@example.com") is None


def test_a_missing_group_stops_the_run(kc: Keycloak, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTORY_SYNC_GROUP", "/nope")
    report = run(kc)
    assert report.stopped and "/nope" in report.stopped


def test_a_dry_run_writes_nothing(kc: Keycloak, principal: int, env: list[tuple[str, dict]]) -> None:
    report = run(kc, dry_run=True)
    assert report.count("created") == 2 and report.count("linked") == 1
    assert by_email("dir-accountant@example.com") is None
    assert not people_store.get_person(principal).directory_managed
    assert directory_sync.status() is None and env == []


def test_the_status_records_each_run(kc: Keycloak, principal: int) -> None:
    run(kc)
    first = directory_sync.status()
    assert first and first["stopped"] is None and first["last_ok_at"] and first["counts"]["created"] == 2
    kc.fail_members = True
    run(kc)
    second = directory_sync.status()
    assert second["stopped"] and second["last_ok_at"] == first["last_ok_at"]


def test_the_token_and_secret_never_reach_the_audit(kc: Keycloak, principal: int, env: list[tuple[str, dict]]) -> None:
    run(kc)
    text = json.dumps(env)
    assert "s3cret" not in text and "access_token" not in text and "Bearer" not in text


# --------------------------------------------------------------------------- #
# Settings and sign-in
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("name", "value", "says"),
    [
        ("auth_oidc_issuer", "https://other.example.com/realms/dynocortex", "AUTH_OIDC_ISSUER"),
        ("auth_oidc_issuer", ISSUER + "/", "slash"),
        ("directory_sync_issuer", ISSUER + "/", "slash"),
        ("directory_sync_issuer", "http://id.example.com/realms/dynocortex", "DIRECTORY_SYNC_ISSUER"),
        ("directory_sync_issuer", "https://id.example.com", "realms"),
        ("directory_sync_admin_url", "http://keycloak.example.com:8180", "DIRECTORY_SYNC_ADMIN_URL"),
        ("directory_sync_admin_url", "https://user:pw@keycloak", "DIRECTORY_SYNC_ADMIN_URL"),
        ("directory_sync_client_secret", "", "DIRECTORY_SYNC_CLIENT_SECRET"),
        ("directory_sync_group", "dynocortex", "DIRECTORY_SYNC_GROUP"),
    ],
)
def test_settings_that_cant_be_trusted_are_refused(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, says: str
) -> None:
    settings = get_settings()
    assert directory_sync.config_problem(settings) is None
    monkeypatch.setattr(settings, name, value)
    problem = directory_sync.config_problem(settings)
    assert problem is not None and says in problem


def test_plain_http_admin_urls_only_on_internal_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    for url in ("http://keycloak:8180", "http://127.0.0.1:8180", "http://10.0.0.5:8180", "https://id.example.com"):
        monkeypatch.setattr(settings, "directory_sync_admin_url", url)
        assert directory_sync.config_problem(settings) is None, url


def test_the_sign_in_list_names_the_subject_only_while_the_sync_is_on(
    kc: Keycloak, principal: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes.auth import allowed_emails

    run(kc)
    rows = {r.email: r.directory_subject for r in allowed_emails()}
    assert rows["dir-accountant@example.com"] == by_email("dir-accountant@example.com").directory_subject
    monkeypatch.setenv("DIRECTORY_SYNC_ENABLED", "false")
    assert all(r.directory_subject is None for r in allowed_emails())


# --------------------------------------------------------------------------- #
# Security review (Oct 25, 2026)
# --------------------------------------------------------------------------- #

def test_the_principals_identity_is_never_on_the_sign_in_list(kc: Keycloak, principal: int) -> None:
    """The principal is never from the directory: their Google or
    ALLOWED_EMAILS sign-in stays the way in when the provider is wrong."""
    from openexecutive.api.routes.auth import allowed_emails

    run(kc)
    assert people_store.get_person(principal).directory_managed
    rows = {r.email: r.directory_subject for r in allowed_emails()}
    assert rows["dir-owner@example.com"] is None
    assert rows["dir-adviser@example.com"] is not None


def test_a_re_created_identity_is_re_linked_and_restored(kc: Keycloak, principal: int) -> None:
    run(kc)
    adviser = by_email("dir-adviser@example.com")
    # Deleted and re-created at the provider: a new id, the same email.
    kc.user("adviser")["id"] = "0b6f9f0e-1c7a-4d1e-9a51-0000000000aa"
    report = run(kc)
    assert ("conflict", "dir-adviser@example.com") in [(a, w) for a, w, _ in report.changes]
    assert people_store.get_person(adviser.id).suspended

    reader = directory_sync.KeycloakReader(get_settings(), transport=httpx.MockTransport(kc.handle))
    with pytest.raises(directory_sync.DirectoryError, match="--relink"):
        directory_sync.link("dir-adviser@example.com", reader=reader)
    reader = directory_sync.KeycloakReader(get_settings(), transport=httpx.MockTransport(kc.handle))
    directory_sync.link("dir-adviser@example.com", relink=True, reader=reader)
    person = people_store.get_person(adviser.id)
    assert person.active and person.directory_subject == "0b6f9f0e-1c7a-4d1e-9a51-0000000000aa"
    assert run(kc).changes == []


def test_relinking_someone_whose_identity_is_still_there_is_refused(kc: Keycloak, principal: int) -> None:
    run(kc)
    reader = directory_sync.KeycloakReader(get_settings(), transport=httpx.MockTransport(kc.handle))
    with pytest.raises(directory_sync.DirectoryError, match="already linked"):
        directory_sync.link("dir-adviser@example.com", relink=True, reader=reader)


def test_unlink_hands_the_row_back(kc: Keycloak, principal: int) -> None:
    run(kc)
    pid = by_email("dir-adviser@example.com").id
    assert "Unlinked" in directory_sync.unlink("dir-adviser@example.com")
    person = people_store.get_person(pid)
    assert not person.directory_managed and person.directory_subject is None
    # The next run sees an unmanaged person with a member's email: a candidate.
    assert ("candidate", "dir-adviser@example.com") in [(a, w) for a, w, _ in run(kc).changes]
    with pytest.raises(directory_sync.DirectoryError):
        directory_sync.unlink("nobody@example.com")


@pytest.mark.parametrize("bad_id", ["", " ", None, 7])
def test_a_member_without_a_usable_id_stops_the_run(kc: Keycloak, principal: int, bad_id: object) -> None:
    kc.user("adviser")["id"] = bad_id
    report = run(kc)
    assert report.stopped and "shape" in report.stopped
    assert by_email("dir-accountant@example.com") is None


def test_a_failed_link_leaves_no_unmanaged_row(
    kc: Keycloak, principal: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise people_store.DirectoryLinkError("taken")

    monkeypatch.setattr(people_store, "link_directory", refuse)
    report = run(kc)
    assert report.count("failed") == 3
    # Archived, so nobody can sign in with the address unchecked.
    assert people_store.find_person_by_email("dir-accountant@example.com") is None
    # The principal's own row is only ever linked, never made or archived.
    owner = people_store.get_person(principal)
    assert owner is not None and owner.active and not owner.directory_managed


def test_turned_off_the_heartbeat_ends_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, env: list[tuple[str, dict]]
) -> None:
    import asyncio

    monkeypatch.setenv("DIRECTORY_SYNC_ENABLED", "false")
    assert asyncio.run(directory_sync.run_scheduled()) is None
    assert directory_sync.enqueue_next_directory_sync_scan() is None
    assert directory_sync.sync().stopped == "DIRECTORY_SYNC_ENABLED is off."
    assert directory_sync.status() is None and env == []
