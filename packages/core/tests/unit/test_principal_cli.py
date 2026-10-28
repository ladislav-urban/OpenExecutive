"""``openexecutive principal``: naming who runs an install without onboarding.

It adds the principal or makes the team member with the address it, updates
them when they are it already, and changes nothing while someone else is the
principal or when the address can't be theirs (people/principal.py).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openexecutive.people import principal as pr
from openexecutive.people import store
from openexecutive.people.models import AuthorityScope


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda event, *a, **k: events.append(event))
    return events


@pytest.fixture
def db(tmp_path: Path, audited: list[str]) -> Path:
    path = tmp_path / "people.db"
    store.initialize_db(path)
    return path


def _mark(db: Path, person_id: int, **columns: int) -> None:
    with sqlite3.connect(str(db)) as conn:
        for column, value in columns.items():
            conn.execute(f"UPDATE people SET {column} = ? WHERE id = ?", (value, person_id))  # noqa: S608 — test columns


def test_adds_the_principal_when_nobody_has_the_address(db: Path, audited: list[str]) -> None:
    line = pr.set_principal("Pat@Example.com", name="Pat Owner", role="Founder", db_path=db)
    principal = store.find_principal_person(db)
    assert principal is not None
    assert (principal.full_name, principal.role, principal.email) == ("Pat Owner", "Founder", "pat@example.com")
    assert principal.kind == "team"
    assert principal.authority_scope == [AuthorityScope.WILDCARD]
    assert "Added Pat Owner" in line
    assert audited == ["principal_set"]


def test_makes_the_team_member_with_the_address_the_principal(db: Path) -> None:
    pid = store.upsert_person(full_name="Pat Owner", role="CEO", email="pat@example.com", db_path=db)
    line = pr.set_principal("pat@example.com", db_path=db)
    principal = store.find_principal_person(db)
    assert principal is not None and principal.id == pid and principal.role == "CEO"
    assert principal.authority_scope == [AuthorityScope.WILDCARD]
    assert "is now the principal" in line


def test_updates_the_principal_and_says_so_when_nothing_changes(db: Path) -> None:
    pr.set_principal("pat@example.com", name="Pat", db_path=db)
    assert "Nothing changed" in pr.set_principal("pat@example.com", db_path=db)
    pr.set_principal("pat@example.com", role="Founder", db_path=db)
    principal = store.find_principal_person(db)
    assert principal is not None and principal.role == "Founder"


def test_a_directory_managed_person_keeps_the_directorys_name(db: Path) -> None:
    pid = store.upsert_person(full_name="Pat From Directory", email="pat@example.com", db_path=db)
    _mark(db, pid, directory_managed=1)
    pr.set_principal("pat@example.com", name="Someone Else", role="Founder", db_path=db)
    principal = store.find_principal_person(db)
    assert principal is not None
    assert (principal.full_name, principal.role) == ("Pat From Directory", "Founder")


def test_never_replaces_another_principal(db: Path) -> None:
    pr.set_principal("pat@example.com", name="Pat", db_path=db)
    store.upsert_person(full_name="Sam", email="sam@example.com", db_path=db)
    with pytest.raises(pr.PrincipalError, match="Pat is the principal already"):
        pr.set_principal("sam@example.com", db_path=db)
    with pytest.raises(pr.PrincipalError, match="Pat is the principal already"):
        pr.set_principal("new@example.com", name="New", db_path=db)
    principal = store.find_principal_person(db)
    assert principal is not None and principal.email == "pat@example.com"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("contact", "A contact can't be the principal"),
        ("suspended", "is suspended"),
        ("demo", "is a demo person"),
        ("alias", "is another address of"),
    ],
)
def test_an_address_that_cant_be_the_principals_changes_nothing(
    db: Path, audited: list[str], setup: str, reason: str
) -> None:
    if setup == "contact":
        store.upsert_person(full_name="Vendor", email="pat@example.com", kind="contact", db_path=db)
    elif setup == "alias":
        pid = store.upsert_person(full_name="Sam", email="sam@example.com", db_path=db)
        store.add_person_email(pid, "pat@example.com", db_path=db)
    else:
        pid = store.upsert_person(full_name="Pat", email="pat@example.com", db_path=db)
        _mark(db, pid, **({"suspended": 1} if setup == "suspended" else {"directory_managed": 1, "directory_demo": 1}))
    with pytest.raises(pr.PrincipalError, match=reason):
        pr.set_principal("pat@example.com", name="Pat", db_path=db)
    assert store.find_principal_person(db) is None
    assert audited == []


@pytest.mark.parametrize(
    ("email", "name", "reason"),
    [
        ("not-an-email", "Pat", "is not an email address"),
        ("pat@example.com", None, "Give their --name"),
        ("pat@example.com", "x" * 121, "the name must be"),
    ],
)
def test_bad_input_changes_nothing(db: Path, email: str, name: str | None, reason: str) -> None:
    with pytest.raises(pr.PrincipalError, match=reason):
        pr.set_principal(email, name=name, db_path=db)
    assert store.find_principal_person(db) is None


def test_claim_principal_wins_only_while_there_is_none(db: Path) -> None:
    a = store.upsert_person(full_name="A", email="a@example.com", db_path=db)
    b = store.upsert_person(full_name="B", email="b@example.com", db_path=db)
    assert store.claim_principal(a, db) is True
    assert store.claim_principal(b, db) is False
    assert store.claim_principal(a, db) is False


def test_the_cli_exits_non_zero_on_a_refusal(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from openexecutive.cli import cli

    real = pr.set_principal
    monkeypatch.setattr(pr, "set_principal", lambda email, **kw: real(email, db_path=db, **kw))
    result = CliRunner().invoke(cli, ["principal", "--email", "pat@example.com"])
    assert result.exit_code == 1
    assert "Not set: nobody has pat@example.com yet" in result.output
    result = CliRunner().invoke(cli, ["principal", "--email", "pat@example.com", "--name", "Pat Owner"])
    assert result.exit_code == 0, result.output
    assert "Added Pat Owner <pat@example.com> as the principal." in result.output
