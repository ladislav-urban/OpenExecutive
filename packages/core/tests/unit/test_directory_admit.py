"""Admitting one just-approved directory member at sign-in
(``integrations.directory_sync.admit_one`` and ``POST /auth/directory-admit``)
against a stubbed Keycloak admin API.

The invariant: an admit does only what the next sync run would do for that
one person (create a team or demo Person for an active member of the group)
or nothing, and believes nothing but the provider."""
from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import auth as auth_route
from openexecutive.integrations import directory_sync
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

ISSUER = "https://id.example.com/realms/dynocortex"
ADMIN = "http://keycloak:8180"
GROUP = "/dynocortex"
DEMO = "/dynocortex/demo"
DEMO_ROLE = "Demo user"


def uid(n: int) -> str:
    return f"0b6f9f0e-1c7a-4d1e-9a51-{n:012d}"


def _jwt(claims: dict[str, Any]) -> str:
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class Keycloak:
    """Just enough of the admin API for an admit: the token endpoint, a user,
    a user's groups (paged), group-by-path and a group's children. Groups
    are by path; ``member_of`` maps a user id to the group paths (or
    explicit ``(id, path)`` pairs) their groups endpoint answers."""

    def __init__(self) -> None:
        self.groups: dict[str, str] = {}   # path -> id
        self.children: dict[str, list[str]] = {}  # path -> child paths
        for path in (GROUP, "/dynocortex/team", DEMO, "/dynocortex/demo/sub", "/dynocortex-other", "/erpnext"):
            self.add_group(path)
        self.users: dict[str, dict[str, Any]] = {}
        self.member_of: dict[str, list[tuple[str, str]]] = {}
        self.requests: list[str] = []
        self.token_requests = 0
        self.status: dict[str, int] = {}     # a path prefix -> the status it answers
        self.bodies: dict[str, Any] = {}     # a path prefix -> the JSON it answers
        self.raise_for: dict[str, Exception] = {}
        self.hook: Any = None                # called with the path before answering

    def add_group(self, path: str, gid: str | None = None, *, root: bool = False) -> str:
        """A group; under the group its path names unless ``root`` (a root
        group whose name holds a slash)."""
        gid = gid or "g-" + path.strip("/").replace("/", "-")
        self.groups[path] = gid
        self.children.setdefault(path, [])
        if not root and path.count("/") > 1:
            self.children[path.rsplit("/", 1)[0]].append(path)
        return gid

    def user(self, n: int, *, email: str | None = None, groups: list[str] | None = None,
             enabled: bool = True, verified: bool = True, first: str = "Pat", last: str = "Member") -> str:
        subject = uid(n)
        self.users[subject] = {
            "id": subject, "username": f"user{n}", "email": email if email is not None else f"user{n}@example.com",
            "emailVerified": verified, "enabled": enabled, "firstName": first, "lastName": last,
        }
        self.member_of[subject] = [(self.groups[p], p) for p in (groups if groups is not None else [GROUP])]
        return subject

    def _answer(self, rest: str) -> httpx.Response | None:
        for prefix, error in self.raise_for.items():
            if rest.startswith(prefix):
                raise error
        for prefix, status in self.status.items():
            if rest.startswith(prefix):
                return httpx.Response(status, text="KEYCLOAK-INTERNAL-TEXT token=abc")
        for prefix, body in self.bodies.items():
            if rest.startswith(prefix):
                return httpx.Response(200, json=body)
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/realms/dynocortex/protocol/openid-connect/token":
            self.token_requests += 1
            if (answer := self._answer("token")) is not None:
                return answer
            return httpx.Response(200, json={"access_token": _jwt({"iss": ISSUER}), "expires_in": 300})
        prefix = "/admin/realms/dynocortex/"
        assert path.startswith(prefix), path
        rest = path[len(prefix):]
        self.requests.append(rest)
        if self.hook is not None:
            self.hook(rest)
        if (answer := self._answer(rest)) is not None:
            return answer
        first = int(request.url.params.get("first", 0))
        size = int(request.url.params.get("max", 100))
        if rest.startswith("group-by-path/"):
            gpath = "/" + rest[len("group-by-path/"):]
            if gpath not in self.groups:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": self.groups[gpath], "path": gpath})
        if rest.startswith("groups/"):
            gid = rest.split("/")[1]
            gpath = next(p for p, i in self.groups.items() if i == gid)
            items = [{"id": self.groups[c], "path": c} for c in self.children[gpath]]
            return httpx.Response(200, json=items[first:first + size])
        if rest.startswith("users/"):
            parts = rest.split("/")
            subject = parts[1]
            if subject not in self.users:
                return httpx.Response(404)
            if len(parts) == 2:
                return httpx.Response(200, json=self.users[subject])
            items = [{"id": gid, "path": gpath, "name": gpath.rsplit("/", 1)[-1]} for gid, gpath in self.member_of[subject]]
            return httpx.Response(200, json=items[first:first + size])
        return httpx.Response(400)


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    db = tmp_path / "admit.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    people_registry.invalidate()
    directory_sync._tokens.clear()
    directory_sync._trees.clear()
    directory_sync._admit_limits.reset()
    for name, value in {
        "DIRECTORY_SYNC_ENABLED": "true",
        "DIRECTORY_SYNC_ISSUER": ISSUER,
        "AUTH_OIDC_ISSUER": ISSUER,
        "DIRECTORY_SYNC_ADMIN_URL": ADMIN,
        "DIRECTORY_SYNC_CLIENT_ID": "openexecutive-directory",
        "DIRECTORY_SYNC_CLIENT_SECRET": "s3cret",  # gitleaks:allow
        "DIRECTORY_SYNC_GROUP": GROUP,
        "DIRECTORY_SYNC_DEMO_GROUP": DEMO,
        "DIRECTORY_SYNC_DEMO_ROLE": DEMO_ROLE,
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
    directory_sync._admit_limits.reset()
    directory_sync._admit_limits.clock = time.monotonic
    directory_sync._trees.clear()
    people_registry.invalidate()


@pytest.fixture
def kc() -> Keycloak:
    return Keycloak()


def admit(kc: Keycloak, subject: str, email: str) -> directory_sync.AdmitResult:
    return directory_sync.admit_one(subject, email, transport=httpx.MockTransport(kc.handle))


def everyone() -> list[Any]:
    return people_store.list_people(include_suspended=True, include_contacts=True)


def all_rows() -> list[tuple[Any, ...]]:
    """Every row of people, archived ones included, as written."""
    with people_store._get_conn() as conn:
        return [tuple(r) for r in conn.execute("SELECT * FROM people ORDER BY id").fetchall()]


def admits(audited: list[tuple[str, dict]]) -> list[dict]:
    return [d for t, d in audited if t == "directory_admit"]


# --------------------------------------------------------------------------- #
# Admitted
# --------------------------------------------------------------------------- #

def test_an_active_member_becomes_a_team_person(kc: Keycloak, env: list[tuple[str, dict]]) -> None:
    subject = kc.user(1, groups=["/dynocortex/team"], first="Ada", last="Approved")
    result = admit(kc, subject, "user1@example.com")
    assert result.admitted and result.code == "admitted" and result.person_id is not None
    person = people_store.get_person(result.person_id)
    assert person is not None
    assert (person.kind, person.email, person.full_name, person.role) == ("team", "user1@example.com", "Ada Approved", "")
    assert person.directory_managed and person.directory_subject == subject
    assert not person.demo and not person.is_principal and person.active
    kinds = [t for t, _ in env]
    assert "directory_person_created" in kinds and "directory_admit" in kinds


def test_a_direct_member_of_the_root_group_is_admitted(kc: Keycloak) -> None:
    # The root group's own id is in the tree it is checked against.
    subject = kc.user(2, groups=[GROUP])
    assert admit(kc, subject, "user2@example.com").admitted


def test_a_member_of_a_nested_subgroup_is_admitted(kc: Keycloak) -> None:
    kc.add_group("/dynocortex/team/emea")
    kc.add_group("/dynocortex/team/emea/sales")
    subject = kc.user(3, groups=["/dynocortex/team/emea/sales"])
    assert admit(kc, subject, "user3@example.com").admitted


def test_a_demo_member_becomes_a_demo_person_with_the_demo_role(kc: Keycloak) -> None:
    subject = kc.user(4, groups=[DEMO])
    result = admit(kc, subject, "user4@example.com")
    person = people_store.get_person(result.person_id)
    assert result.admitted and person.demo and person.role == DEMO_ROLE and person.kind == "team"


def test_a_member_of_a_demo_subgroup_is_a_demo_person(kc: Keycloak) -> None:
    subject = kc.user(5, groups=["/dynocortex/demo/sub"])
    result = admit(kc, subject, "user5@example.com")
    assert result.admitted and people_store.get_person(result.person_id).demo


def test_a_demo_subgroup_newer_than_the_kept_demo_tree_still_gives_a_demo_person(kc: Keycloak) -> None:
    # Someone in the team group reads both trees, which are then kept.
    assert admit(kc, kc.user(6, groups=["/dynocortex/team"]), "user6@example.com").admitted
    assert any(k[2] == DEMO for k in directory_sync._trees)
    # A demo subgroup made after that: the kept demo tree lacks its id, but
    # its path is under the demo group, and either signal makes them demo.
    kc.add_group("/dynocortex/demo/new")
    directory_sync._trees.pop((ISSUER, ADMIN, GROUP))  # the main tree's read is fresh
    result = admit(kc, kc.user(7, groups=["/dynocortex/demo/new"]), "user7@example.com")
    person = people_store.get_person(result.person_id)
    assert result.admitted and person.demo and person.role == DEMO_ROLE


def test_a_group_id_in_the_demo_tree_makes_them_demo_whatever_its_path_says(kc: Keycloak) -> None:
    subject = kc.user(8, groups=[])
    # Their group answers a path outside the demo group, but its id is the
    # demo subgroup's: the narrower role wins.
    kc.member_of[subject] = [(kc.groups["/dynocortex/demo/sub"], "/dynocortex/moved-sub")]
    result = admit(kc, subject, "user8@example.com")
    assert result.admitted and people_store.get_person(result.person_id).demo


def test_the_profile_attributes_and_name_come_from_the_provider_only(kc: Keycloak) -> None:
    subject = kc.user(9, first="", last="")
    result = admit(kc, subject, "user9@example.com")
    assert result.admitted and people_store.get_person(result.person_id).full_name == "user9"


# --------------------------------------------------------------------------- #
# Refused, nothing written
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("groups", "code"),
    [
        ([], "not_member"),
        (["/erpnext"], "not_member"),
        (["/dynocortex-other"], "not_member"),   # a sibling sharing the prefix
    ],
)
def test_someone_outside_the_group_is_refused(kc: Keycloak, groups: list[str], code: str) -> None:
    subject = kc.user(10, groups=groups)
    assert admit(kc, subject, "user10@example.com").code == code
    assert everyone() == []


def test_a_root_group_named_like_a_path_under_the_group_is_refused(kc: Keycloak) -> None:
    # One root group whose name holds a slash: its path reads "/dynocortex/x"
    # but its id isn't in the group's tree.
    kc.add_group("/dynocortex/x", "g-slash-named-root", root=True)
    assert "/dynocortex/x" not in kc.children[GROUP]
    subject = kc.user(11, groups=["/dynocortex/x"])
    assert admit(kc, subject, "user11@example.com").code == "not_member"
    assert everyone() == []


def test_a_group_moved_out_of_the_group_is_refused(kc: Keycloak) -> None:
    # Read once while it was inside, so the kept tree still holds its id.
    assert admit(kc, kc.user(12, groups=["/dynocortex/team"]), "user12@example.com").admitted
    team_id = kc.groups["/dynocortex/team"]
    subject = kc.user(13, groups=[])
    kc.member_of[subject] = [(team_id, "/elsewhere/team")]
    assert admit(kc, subject, "user13@example.com").code == "not_member"
    assert [p.email for p in everyone()] == ["user12@example.com"]


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"enabled": False}, "inactive"),
        ({"verified": False}, "inactive"),
        ({"email": ""}, "inactive"),
        ({"email": "someone.else@example.com"}, "email_mismatch"),
    ],
)
def test_an_inactive_or_mismatched_user_is_refused(kc: Keycloak, kwargs: dict, code: str) -> None:
    subject = kc.user(14, **kwargs)
    assert admit(kc, subject, "user14@example.com").code == code
    assert everyone() == []


@pytest.mark.parametrize("theirs", ["  User15@Example.COM ", "USER15@EXAMPLE.COM", "user15@example.com"])
@pytest.mark.parametrize("claimed", ["user15@example.com", " User15@example.com "])
def test_case_and_space_variants_are_normalised_before_comparing(kc: Keycloak, theirs: str, claimed: str) -> None:
    subject = kc.user(15, email=theirs)
    result = admit(kc, subject, claimed)
    assert result.admitted
    assert people_store.get_person(result.person_id).email == "user15@example.com"


def test_an_unknown_subject_is_refused(kc: Keycloak) -> None:
    assert admit(kc, uid(99), "user99@example.com").code == "unknown"
    assert everyone() == []


@pytest.mark.parametrize(
    "subject",
    [
        "", "not-a-uuid", "0B6F9F0E-1C7A-4D1E-9A51-000000000001", uid(1) + "0", " " + uid(1),
        "f:provider:" + uid(1), "../" + uid(1)[3:], "x" * 65, None, 42,
    ],
)
def test_a_malformed_subject_is_refused_before_any_read(kc: Keycloak, subject: Any) -> None:
    kc.user(1)
    assert directory_sync.admit_one(subject, "user1@example.com", transport=httpx.MockTransport(kc.handle)).code == "invalid"
    assert kc.requests == [] and kc.token_requests == 0


@pytest.mark.parametrize("email", ["", "no-at-sign", "a@b@c", "a b@example.com", "@example.com",
                                   "x" * 250 + "@example.com", None])
def test_a_malformed_email_is_refused_before_any_read(kc: Keycloak, email: Any) -> None:
    subject = kc.user(1)
    assert directory_sync.admit_one(subject, email, transport=httpx.MockTransport(kc.handle)).code == "invalid"
    assert kc.requests == [] and kc.token_requests == 0


@pytest.mark.parametrize(
    ("prefix", "body"),
    [
        ("users/" + uid(16) + "/groups", {"not": "a list"}),
        ("users/" + uid(16) + "/groups", [{"id": 5, "path": GROUP}]),
        ("users/" + uid(16) + "/groups", [{"path": GROUP}]),
        ("group-by-path/", {"path": GROUP}),
    ],
)
def test_an_answer_of_the_wrong_shape_is_refused(kc: Keycloak, prefix: str, body: Any) -> None:
    subject = kc.user(16)
    kc.bodies[prefix] = body
    assert admit(kc, subject, "user16@example.com").code == "error"
    assert everyone() == []


@pytest.mark.parametrize("body", [["a", "list"], "text", {"id": uid(17) + "x"}, {"username": "no id"}])
def test_a_user_answer_of_the_wrong_shape_or_another_id_is_refused(kc: Keycloak, body: Any) -> None:
    subject = kc.user(16)
    kc.bodies["users/" + subject] = body
    # Only the bare user read: the groups path is longer and matched first otherwise.
    kc.bodies.pop("users/" + subject + "/groups", None)
    assert admit(kc, subject, "user16@example.com").code == "error"
    assert everyone() == []


def test_a_tree_of_more_than_fifty_groups_is_refused_and_not_kept(kc: Keycloak) -> None:
    for n in range(60):
        kc.add_group(f"/dynocortex/team/g{n}")
    subject = kc.user(18, groups=["/dynocortex/team"])
    assert admit(kc, subject, "user18@example.com").code == "error"
    assert directory_sync._trees == {}
    assert everyone() == []


# --------------------------------------------------------------------------- #
# Left alone
# --------------------------------------------------------------------------- #

def test_a_suspended_linked_person_gets_exists_and_stays_suspended(kc: Keycloak) -> None:
    subject = kc.user(20)
    pid = people_store.upsert_person(full_name="Sue", email="user20@example.com", kind="team")
    people_store.link_directory(pid, subject)
    people_store.suspend_person(pid, by="principal")
    before = all_rows()
    result = admit(kc, subject, "user20@example.com")
    assert (result.code, result.person_id) == ("exists", pid)
    assert all_rows() == before and kc.requests == []
    assert people_store.get_person(pid).suspended


def test_an_archived_managed_person_still_in_the_group_gets_a_new_person(kc: Keycloak) -> None:
    # As the run does: an archived row's link no longer counts.
    subject = kc.user(21)
    old = people_store.upsert_person(full_name="Old", email="user21@example.com", kind="team")
    people_store.link_directory(old, subject)
    people_store.archive_person(old)
    result = admit(kc, subject, "user21@example.com")
    assert result.admitted and result.person_id != old
    assert people_store.get_person(result.person_id).directory_subject == subject
    archived = people_store.get_person(old)
    assert archived is None or archived.archived


def test_an_unmanaged_person_with_the_email_is_left_to_the_run(kc: Keycloak) -> None:
    subject = kc.user(22)
    people_store.upsert_person(full_name="Hand Made", email="user22@example.com", kind="team")
    before = all_rows()
    assert admit(kc, subject, "user22@example.com").code == "address_held"
    assert all_rows() == before and kc.requests == []


def test_the_principals_email_is_never_linked_by_an_admit(kc: Keycloak) -> None:
    subject = kc.user(23, email="owner@example.com")
    people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="owner@example.com")
    before = all_rows()
    assert admit(kc, subject, "owner@example.com").code == "address_held"
    assert all_rows() == before and kc.requests == []


def test_another_directory_identitys_email_is_left_alone(kc: Keycloak) -> None:
    other = kc.user(24, email="shared@example.com")
    pid = people_store.upsert_person(full_name="First", email="shared@example.com", kind="team")
    people_store.link_directory(pid, other)
    subject = kc.user(25, email="shared@example.com")
    before = all_rows()
    assert admit(kc, subject, "shared@example.com").code == "address_held"
    assert all_rows() == before


def test_an_alias_held_by_someone_is_left_alone(kc: Keycloak) -> None:
    pid = people_store.upsert_person(full_name="Has Alias", email="main@example.com", kind="team")
    people_store.add_person_email(pid, "user26@example.com")
    subject = kc.user(26)
    assert admit(kc, subject, "user26@example.com").code == "address_held"


def test_an_address_taken_while_reading_is_rechecked_under_the_lock(kc: Keycloak) -> None:
    subject = kc.user(27)

    def take_it(rest: str) -> None:
        if rest == "users/" + subject and not everyone():
            people_store.upsert_person(full_name="Quick", email="user27@example.com", kind="team")

    kc.hook = take_it
    assert admit(kc, subject, "user27@example.com").code == "address_held"
    assert [p.full_name for p in everyone()] == ["Quick"]
    assert not everyone()[0].directory_managed


def test_add_in_admit_mode_never_links_the_principal() -> None:
    pid = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="owner@example.com")
    report = directory_sync.Report(dry_run=False)
    member = directory_sync.Member(id=uid(28), email="owner@example.com", full_name="Olivia", active=True)
    assert directory_sync._add(member, report, admit=True) is None
    assert [a for a, _, _ in report.changes] == ["conflict"]
    assert not people_store.get_person(pid).directory_managed


# --------------------------------------------------------------------------- #
# Off, misconfigured, the provider failing
# --------------------------------------------------------------------------- #

def test_nothing_happens_while_the_sync_is_off(kc: Keycloak, monkeypatch: pytest.MonkeyPatch,
                                               env: list[tuple[str, dict]]) -> None:
    monkeypatch.setenv("DIRECTORY_SYNC_ENABLED", "false")
    subject = kc.user(30)
    assert admit(kc, subject, "user30@example.com").code == "disabled"
    assert kc.requests == [] and kc.token_requests == 0 and everyone() == [] and admits(env) == []


def test_nothing_happens_when_the_settings_cant_be_used(kc: Keycloak, monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings that fail config_problem don't load at all while the sync is
    # on; the admit checks again anyway, as the run does.
    from openexecutive.config import get_settings

    settings = get_settings()
    seen: list[Any] = []
    monkeypatch.setattr(directory_sync, "get_settings", lambda: settings)
    monkeypatch.setattr(directory_sync, "config_problem", lambda s: seen.append(s) or "bad")
    subject = kc.user(31)
    assert admit(kc, subject, "user31@example.com").code == "disabled"
    assert seen and kc.requests == [] and kc.token_requests == 0 and everyone() == []


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")]
)
def test_the_provider_down_or_timing_out_writes_nothing(kc: Keycloak, error: Exception) -> None:
    subject = kc.user(32)
    kc.raise_for["users/"] = error
    assert admit(kc, subject, "user32@example.com").code == "error"
    assert everyone() == []


def test_the_token_endpoint_down_writes_nothing(kc: Keycloak) -> None:
    subject = kc.user(33)
    kc.status["token"] = 500
    assert admit(kc, subject, "user33@example.com").code == "error"
    assert everyone() == []


@pytest.mark.parametrize("status", [500, 403, 400, 502])
def test_an_error_status_writes_nothing(kc: Keycloak, status: int) -> None:
    subject = kc.user(34)
    kc.status["users/"] = status
    assert admit(kc, subject, "user34@example.com").code == "error"
    assert everyone() == []


def test_a_404_for_the_groups_is_an_error_not_unknown(kc: Keycloak) -> None:
    subject = kc.user(35)
    kc.status[f"users/{subject}/groups"] = 404
    assert admit(kc, subject, "user35@example.com").code == "error"


def test_a_missing_group_in_the_realm_is_an_error(kc: Keycloak) -> None:
    subject = kc.user(36)
    kc.status["group-by-path/"] = 404
    assert admit(kc, subject, "user36@example.com").code == "error"
    assert directory_sync._trees == {}


def test_a_401_retries_the_token_once_then_refuses(kc: Keycloak) -> None:
    subject = kc.user(37)
    kc.status["users/"] = 401
    assert admit(kc, subject, "user37@example.com").code == "error"
    assert kc.token_requests == 2
    assert everyone() == []


def test_a_kept_token_the_provider_no_longer_takes_is_replaced(kc: Keycloak) -> None:
    subject = kc.user(38)
    calls = {"n": 0}

    def once(rest: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            kc.status["users/"] = 401
        else:
            kc.status.pop("users/", None)

    kc.hook = once
    assert admit(kc, subject, "user38@example.com").admitted
    assert kc.token_requests == 2


def test_the_reads_share_one_time_budget(kc: Keycloak, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(directory_sync, "ADMIT_BUDGET_S", 0.2)
    subject = kc.user(39)

    def slow(rest: str) -> None:
        if rest == "users/" + subject:
            time.sleep(0.3)

    kc.hook = slow
    assert admit(kc, subject, "user39@example.com").code == "error"
    # The groups read never started: the budget was spent.
    assert kc.requests == ["users/" + subject]
    assert everyone() == []


def test_each_call_carries_the_admit_timeout_under_the_budget(kc: Keycloak) -> None:
    seen: list[float] = []
    subject = kc.user(40)
    inner = kc.handle

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return inner(request)

    assert directory_sync.admit_one(subject, "user40@example.com", transport=httpx.MockTransport(handle)).admitted
    assert seen and all(0 < t <= directory_sync.ADMIT_CALL_TIMEOUT_S for t in seen)
    assert directory_sync.ADMIT_BUDGET_S < 4.0


def test_the_readers_client_is_closed_whatever_happens(kc: Keycloak, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    original = directory_sync.KeycloakReader.close
    monkeypatch.setattr(directory_sync.KeycloakReader, "close", lambda self: (closed.append(True), original(self)))
    subject = kc.user(41)
    kc.raise_for["users/"] = httpx.ConnectError("down")
    admit(kc, subject, "user41@example.com")
    kc.raise_for.clear()
    directory_sync._admit_limits.reset()
    admit(kc, subject, "user41@example.com")
    assert closed == [True, True]


def test_an_unexpected_failure_writes_nothing_and_says_error(kc: Keycloak, monkeypatch: pytest.MonkeyPatch) -> None:
    subject = kc.user(42)

    def boom(*a: Any, **k: Any) -> int:
        raise RuntimeError("store down")

    monkeypatch.setattr(people_store, "upsert_person", boom)
    assert admit(kc, subject, "user42@example.com").code == "error"
    assert everyone() == []
    # The lock and the slot were let go.
    assert not directory_sync._run_lock.locked() and not directory_sync._admit_slot.locked()


# --------------------------------------------------------------------------- #
# Once only, and never alongside a run
# --------------------------------------------------------------------------- #

def test_a_second_admit_says_exists_and_one_person_is_made(kc: Keycloak) -> None:
    subject = kc.user(50)
    first = admit(kc, subject, "user50@example.com")
    second = admit(kc, subject, "user50@example.com")
    assert first.admitted and (second.code, second.person_id) == ("exists", first.person_id)
    assert len(everyone()) == 1


def test_the_next_run_finds_the_admitted_person_unchanged(kc: Keycloak) -> None:
    subject = kc.user(51, first="Ada", last="Approved")
    assert admit(kc, subject, "user51@example.com").admitted
    member = directory_sync._member(subject, kc.users[subject])

    class Reader:
        def read_members(self) -> dict[str, directory_sync.Member]:
            return {subject: member}

    report = directory_sync.sync(reader=Reader())
    assert report.stopped is None and report.changes == [] and report.unchanged == 1


class _Members:
    def __init__(self, members: dict[str, directory_sync.Member]) -> None:
        self.members = members

    def read_members(self) -> dict[str, directory_sync.Member]:
        return self.members


def test_the_lock_is_never_held_while_reading_and_a_run_goes_ahead(kc: Keycloak) -> None:
    subject = kc.user(52)
    entered, go = threading.Event(), threading.Event()
    held_during_io: list[bool] = []

    def hang(rest: str) -> None:
        held_during_io.append(directory_sync._run_lock.locked())
        if rest == "users/" + subject:
            entered.set()
            assert go.wait(5)

    kc.hook = hang
    out: list[directory_sync.AdmitResult] = []
    worker = threading.Thread(target=lambda: out.append(admit(kc, subject, "user52@example.com")))
    worker.start()
    assert entered.wait(5)
    # A heartbeat run started mid-admit gets the lock and runs.
    member = directory_sync._member(subject, kc.users[subject])
    report = directory_sync.sync(reader=_Members({subject: member}))
    assert report.stopped is None and report.count("created") == 1
    go.set()
    worker.join(5)
    assert not worker.is_alive()
    assert out[0].code == "exists"
    assert len(everyone()) == 1
    assert held_during_io and not any(held_during_io)


def test_a_run_holding_the_lock_makes_an_admit_busy_without_writing(
    kc: Keycloak, monkeypatch: pytest.MonkeyPatch, env: list[tuple[str, dict]]
) -> None:
    monkeypatch.setattr(directory_sync, "ADMIT_LOCK_WAIT_S", 0.05)
    subject = kc.user(53)
    assert directory_sync._run_lock.acquire(blocking=False)
    try:
        start = time.monotonic()
        assert admit(kc, subject, "user53@example.com").code == "busy"
        assert time.monotonic() - start < 2
    finally:
        directory_sync._run_lock.release()
    assert everyone() == []
    assert admits(env) == []


def test_an_admit_waits_briefly_for_a_short_run(kc: Keycloak) -> None:
    subject = kc.user(54)
    assert directory_sync._run_lock.acquire(blocking=False)
    threading.Timer(0.1, directory_sync._run_lock.release).start()
    assert admit(kc, subject, "user54@example.com").admitted


def test_a_second_concurrent_admit_is_busy_at_once(kc: Keycloak, env: list[tuple[str, dict]]) -> None:
    first, second = kc.user(55), kc.user(56)
    entered, go = threading.Event(), threading.Event()

    def hang(rest: str) -> None:
        if rest == "users/" + first:
            entered.set()
            assert go.wait(5)

    kc.hook = hang
    out: list[directory_sync.AdmitResult] = []
    worker = threading.Thread(target=lambda: out.append(admit(kc, first, "user55@example.com")))
    worker.start()
    assert entered.wait(5)
    start = time.monotonic()
    assert admit(kc, second, "user56@example.com").code == "busy"
    assert time.monotonic() - start < 1
    go.set()
    worker.join(5)
    assert out[0].admitted
    assert [p.email for p in everyone()] == ["user55@example.com"]
    # No audit row for busy; the admit that ran has one.
    assert [d["code"] for d in admits(env)] == ["admitted"]
    # A busy answer didn't use the subject's turn: they may try again.
    assert admit(kc, second, "user56@example.com").admitted


def test_concurrent_admits_of_the_same_person_make_one_person(kc: Keycloak) -> None:
    subject = kc.user(57)
    barrier = threading.Barrier(8)
    out: list[directory_sync.AdmitResult] = []
    lock = threading.Lock()

    def one() -> None:
        barrier.wait()
        result = admit(kc, subject, "user57@example.com")
        with lock:
            out.append(result)

    workers = [threading.Thread(target=one) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(10)
    codes = sorted(r.code for r in out)
    assert codes.count("admitted") == 1
    assert set(codes) <= {"admitted", "busy", "rate_limited", "exists"}
    assert len(everyone()) == 1


def test_concurrent_admits_of_different_people_never_interleave(kc: Keycloak) -> None:
    subjects = [kc.user(60 + n) for n in range(6)]
    out: list[directory_sync.AdmitResult] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(subjects))

    def one(n: int) -> None:
        barrier.wait()
        result = admit(kc, subjects[n], f"user{60 + n}@example.com")
        with lock:
            out.append(result)

    workers = [threading.Thread(target=one, args=(n,)) for n in range(len(subjects))]
    for w in workers:
        w.start()
    for w in workers:
        w.join(10)
    admitted = [r for r in out if r.admitted]
    assert len(admitted) >= 1 and all(r.code in ("admitted", "busy") for r in out)
    assert len(everyone()) == len(admitted)
    assert len({r.person_id for r in admitted}) == len(admitted)


# --------------------------------------------------------------------------- #
# Rate limits
# --------------------------------------------------------------------------- #

class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    tick = Clock()
    directory_sync._admit_limits.clock = tick
    return tick


def test_one_admit_per_subject_per_thirty_seconds(kc: Keycloak, clock: Clock, env: list[tuple[str, dict]]) -> None:
    subject = kc.user(70, groups=[])
    assert admit(kc, subject, "user70@example.com").code == "not_member"
    reads = len(kc.requests)
    clock.now += 29
    assert admit(kc, subject, "user70@example.com").code == "rate_limited"
    assert len(kc.requests) == reads, "refused before any read"
    assert [d["code"] for d in admits(env)] == ["not_member"], "no audit row for rate_limited"
    assert directory_sync._admit_limits.refused["rate_limited"] == 1
    clock.now += 2
    kc.member_of[subject] = [(kc.groups[GROUP], GROUP)]
    assert admit(kc, subject, "user70@example.com").admitted


def test_twenty_admits_a_minute_in_all(kc: Keycloak, clock: Clock) -> None:
    for n in range(20):
        assert admit(kc, kc.user(100 + n, groups=[]), f"user{100 + n}@example.com").code == "not_member"
        clock.now += 1
    late = kc.user(200)
    reads = len(kc.requests)
    assert admit(kc, late, "user200@example.com").code == "rate_limited"
    assert len(kc.requests) == reads
    clock.now += 41  # the first one is a minute old now
    assert admit(kc, late, "user200@example.com").admitted


def test_only_admits_reaching_the_provider_count_toward_the_cap(kc: Keycloak, clock: Clock,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    for n in range(30):
        assert directory_sync.admit_one("bad", f"x{n}@example.com").code == "invalid"
    held = people_store.upsert_person(full_name="Held", email="held@example.com", kind="team")
    assert held
    for n in range(30):
        assert admit(kc, kc.user(300 + n), "held@example.com").code == "address_held"
    monkeypatch.setenv("DIRECTORY_SYNC_ENABLED", "false")
    for n in range(30):
        assert admit(kc, kc.user(400 + n), f"user{400 + n}@example.com").code == "disabled"
    monkeypatch.setenv("DIRECTORY_SYNC_ENABLED", "true")
    assert admit(kc, kc.user(500), "user500@example.com").admitted


def test_the_subject_map_is_pruned_and_capped(kc: Keycloak, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(directory_sync, "ADMIT_SUBJECT_CAP", 3)
    for n in range(3):
        assert admit(kc, kc.user(600 + n, groups=[]), f"user{600 + n}@example.com").code == "not_member"
        clock.now += 1
    # Full of live entries: a new subject is refused, nobody is evicted.
    newcomer = kc.user(700)
    assert admit(kc, newcomer, "user700@example.com").code == "rate_limited"
    assert admit(kc, uid(600), "user600@example.com").code == "rate_limited"
    assert len(directory_sync._admit_limits._subjects) == 3
    clock.now += 31
    assert admit(kc, newcomer, "user700@example.com").admitted
    assert len(directory_sync._admit_limits._subjects) == 1


def test_a_backwards_clock_never_keeps_an_entry_forever(kc: Keycloak, clock: Clock) -> None:
    subject = kc.user(800, groups=[])
    assert admit(kc, subject, "user800@example.com").code == "not_member"
    clock.now -= 100
    kc.member_of[subject] = [(kc.groups[GROUP], GROUP)]
    assert admit(kc, subject, "user800@example.com").admitted


# --------------------------------------------------------------------------- #
# Afterwards: the registry, the roster intake and the audit row
# --------------------------------------------------------------------------- #

def test_the_registry_is_refreshed_and_the_roster_intake_runs(kc: Keycloak, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(people_registry, "invalidate", lambda: calls.append("invalidate"))
    monkeypatch.setattr("openexecutive.integrations.roster_intake.after_roster_write",
                        lambda: calls.append("after_roster_write"))
    assert admit(kc, kc.user(900), "user900@example.com").admitted
    assert calls == ["invalidate", "after_roster_write"]


def test_nothing_is_refreshed_when_nothing_was_written(kc: Keycloak, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(people_registry, "invalidate", lambda: calls.append("invalidate"))
    monkeypatch.setattr("openexecutive.integrations.roster_intake.after_roster_write",
                        lambda: calls.append("after_roster_write"))
    assert admit(kc, kc.user(901, groups=[]), "user901@example.com").code == "not_member"
    assert calls == []


def test_a_failing_roster_intake_doesnt_undo_the_admit(kc: Keycloak, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("intake down")

    monkeypatch.setattr("openexecutive.integrations.roster_intake.after_roster_write", boom)
    assert admit(kc, kc.user(902), "user902@example.com").admitted
    assert len(everyone()) == 1


def test_the_audit_row_holds_no_email_token_or_name(kc: Keycloak, env: list[tuple[str, dict]]) -> None:
    subject = kc.user(903, first="Secretive", last="Person")
    result = admit(kc, subject, "user903@example.com")
    rows = admits(env)
    assert len(rows) == 1
    row = rows[0]
    assert row == {
        "code": "admitted",
        "subject_sha256": directory_sync._subject_digest(subject),
        "email_domain": "example.com",
        "person_id": result.person_id,
    }
    text = json.dumps(env)
    for leak in ("user903", "Secretive", subject, "s3cret", "access_token", "Bearer"):
        assert leak not in text


def test_a_refusal_that_reached_the_provider_is_audited_once(kc: Keycloak, env: list[tuple[str, dict]]) -> None:
    subject = kc.user(904, email="other@example.com")
    assert admit(kc, subject, "user904@example.com").code == "email_mismatch"
    assert [(d["code"], d["person_id"]) for d in admits(env)] == [("email_mismatch", None)]
    assert "other@example.com" not in json.dumps(env)


def test_stops_before_the_provider_leave_no_audit_row(kc: Keycloak, env: list[tuple[str, dict]]) -> None:
    people_store.upsert_person(full_name="Held", email="held@example.com", kind="team")
    admit(kc, kc.user(905), "held@example.com")
    admit(kc, "nope", "user905@example.com")
    assert admits(env) == []


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(kc: Keycloak, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    real = directory_sync.KeycloakReader
    transport = httpx.MockTransport(kc.handle)
    monkeypatch.setattr(
        directory_sync, "KeycloakReader", lambda settings, **kw: real(settings, **{**kw, "transport": transport})
    )
    app = FastAPI()
    app.include_router(auth_route.router)
    return TestClient(app)


def test_the_route_admits_and_answers_only_admitted_and_code(kc: Keycloak, client: TestClient) -> None:
    subject = kc.user(1000)
    res = client.post("/auth/directory-admit", json={"subject": subject, "email": "user1000@example.com"})
    assert res.status_code == 200 and res.json() == {"admitted": True, "code": "admitted"}
    again = client.post("/auth/directory-admit", json={"subject": subject, "email": "user1000@example.com"})
    assert again.json() == {"admitted": False, "code": "exists"}


def test_the_route_never_echoes_the_providers_text(kc: Keycloak, client: TestClient) -> None:
    subject = kc.user(1001)
    kc.status["users/"] = 500
    res = client.post("/auth/directory-admit", json={"subject": subject, "email": "user1001@example.com"})
    assert res.status_code == 200 and res.json() == {"admitted": False, "code": "error"}
    assert "KEYCLOAK" not in res.text and "token" not in res.text


def test_the_route_refuses_a_call_made_as_someone(kc: Keycloak, client: TestClient) -> None:
    subject = kc.user(1002)
    res = client.post(
        "/auth/directory-admit",
        json={"subject": subject, "email": "user1002@example.com"},
        headers={"x-caller-email": "user1002@example.com"},
    )
    assert res.status_code == 404
    assert everyone() == [] and kc.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {"subject": uid(1), "email": "a@example.com", "admin": True},
        {"subject": uid(1), "email": "a@example.com", "kind": "operator"},
        {"subject": "x" * 65, "email": "a@example.com"},
        {"subject": uid(1), "email": "x" * 250 + "@example.com"},
        {"subject": uid(1)},
        {"email": "a@example.com"},
        {"subject": 5, "email": "a@example.com"},
    ],
)
def test_the_route_takes_exactly_a_subject_and_an_email(kc: Keycloak, client: TestClient, body: dict) -> None:
    assert client.post("/auth/directory-admit", json=body).status_code == 422
    assert kc.requests == [] and kc.token_requests == 0


def test_the_route_is_behind_the_shared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api import main as api_main

    assert "/auth/directory-admit" not in api_main._UNAUTHENTICATED_PATHS
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "k" * 32)
    called: list[Any] = []
    monkeypatch.setattr(directory_sync, "admit_one", lambda *a, **k: called.append(a))
    app_client = TestClient(api_main.create_app())
    body = {"subject": uid(1), "email": "a@example.com"}
    assert app_client.post("/auth/directory-admit", json=body).status_code == 401
    assert app_client.post("/auth/directory-admit", json=body, headers={"x-api-key": "wrong"}).status_code == 401
    assert called == []
