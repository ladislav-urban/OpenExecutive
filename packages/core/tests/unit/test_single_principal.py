"""There is one principal: the store refuses a second in the write itself.

``upsert_person(is_principal=True)`` raises ``PrincipalExistsError`` while
someone else is the principal (a suspended one counts), atomically, and
``transfer_principal`` moves the flag in one UPDATE (people/store.py). The
People API answers 409 (api/routes/people.py), and onboarding's re-run with
another owner moves it with a transfer (onboarding/commit.py).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openexecutive.people import store
from openexecutive.people.models import AuthorityScope


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "people.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    store.initialize_db(path)
    return path


def _principals(db: Path) -> list[int]:
    with sqlite3.connect(str(db)) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM people WHERE is_principal = 1 ORDER BY id")]


def test_a_second_principal_is_refused_on_insert_and_update(db: Path) -> None:
    first = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    with pytest.raises(store.PrincipalExistsError):
        store.upsert_person(full_name="Sam", is_principal=True, db_path=db)
    sam = store.upsert_person(full_name="Sam", db_path=db)
    with pytest.raises(store.PrincipalExistsError):
        store.upsert_person(person_id=sam, full_name="Sam", is_principal=True, db_path=db)
    assert _principals(db) == [first]


def test_the_principal_may_rewrite_their_own_row(db: Path) -> None:
    pat = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    store.upsert_person(person_id=pat, full_name="Pat Owner", role="Founder", is_principal=True, db_path=db)
    principal = store.find_principal_person(db)
    assert principal is not None and (principal.id, principal.full_name) == (pat, "Pat Owner")


def test_a_suspended_or_archived_principal(db: Path) -> None:
    pat = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE people SET suspended = 1 WHERE id = ?", (pat,))
    # Suspended still counts: restored, they would be a second one.
    with pytest.raises(store.PrincipalExistsError):
        store.upsert_person(full_name="Sam", is_principal=True, db_path=db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE people SET archived = 1 WHERE id = ?", (pat,))
    # Archived doesn't: the seat is free.
    store.upsert_person(full_name="Sam", is_principal=True, db_path=db)


def test_transfer_moves_the_flag_in_one_step(db: Path) -> None:
    pat = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    sam = store.upsert_person(full_name="Sam", db_path=db)
    assert store.transfer_principal(sam, db) == [pat]
    assert _principals(db) == [sam]
    assert store.transfer_principal(sam, db) == []
    assert _principals(db) == [sam]


@pytest.mark.parametrize("column", ["suspended", "directory_demo", "archived"])
def test_transfer_refuses_someone_who_cant_be_the_principal(db: Path, column: str) -> None:
    pat = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    sam = store.upsert_person(full_name="Sam", db_path=db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(f"UPDATE people SET {column} = 1 WHERE id = ?", (sam,))  # noqa: S608 — test columns
    with pytest.raises(store.PrincipalExistsError):
        store.transfer_principal(sam, db)
    assert _principals(db) == [pat]


def test_transfer_refuses_a_contact(db: Path) -> None:
    pat = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    vendor = store.upsert_person(full_name="Vendor", kind="contact", db_path=db)
    with pytest.raises(store.PrincipalExistsError):
        store.transfer_principal(vendor, db)
    assert _principals(db) == [pat]


def test_onboarding_rerun_with_another_owner_moves_the_principal(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.onboarding.commit import save_onboarding_people
    from openexecutive.onboarding.interview import PersonDraft

    pat = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    store.set_authority_scope(pat, [AuthorityScope.WILDCARD, AuthorityScope("spend_lt_2k")], db_path=db)
    ids = save_onboarding_people(
        [PersonDraft(full_name="Pat", role="Advisor"), PersonDraft(full_name="Sam", role="CEO", is_principal=True)]
    )
    assert ids and _principals(db) == [ids["Sam"]]
    sam = store.get_person(ids["Sam"], db)
    old = store.get_person(pat, db)
    assert sam is not None and sam.authority_scope == [AuthorityScope.WILDCARD]
    # Only WILDCARD goes: the other scope they were granted stays.
    assert old is not None and old.authority_scope == [AuthorityScope("spend_lt_2k")]


def test_transfer_moves_wildcard_with_the_flag(db: Path) -> None:
    pat = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    store.set_authority_scope(pat, [AuthorityScope.WILDCARD, AuthorityScope("spend_lt_2k")], db_path=db)
    sam = store.upsert_person(full_name="Sam", db_path=db)
    store.transfer_principal(sam, db)
    old, new = store.get_person(pat, db), store.get_person(sam, db)
    assert old is not None and old.authority_scope == [AuthorityScope("spend_lt_2k")]
    assert new is not None and AuthorityScope.WILDCARD in new.authority_scope


def test_onboarding_rerun_repairs_a_roster_holding_two_principals(db: Path) -> None:
    """A roster from before the store refused a second principal can hold
    two; a re-run of setup ends with the drafted one alone, as it used to."""
    from openexecutive.onboarding.commit import save_onboarding_people
    from openexecutive.onboarding.interview import PersonDraft

    pat = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    sam = store.upsert_person(full_name="Sam", db_path=db)
    with sqlite3.connect(str(db)) as conn:  # the legacy second principal
        conn.execute("UPDATE people SET is_principal = 1 WHERE id = ?", (sam,))
    for pid in (pat, sam):
        store.set_authority_scope(pid, [AuthorityScope.WILDCARD], db_path=db)

    ids = save_onboarding_people(
        [PersonDraft(full_name="Pat", role="Founder", is_principal=True), PersonDraft(full_name="Sam", role="CFO")]
    )
    assert ids == {"Pat": pat, "Sam": sam}
    assert _principals(db) == [pat]
    sam_row = store.get_person(sam, db)
    assert sam_row is not None and AuthorityScope.WILDCARD not in sam_row.authority_scope


def test_onboarding_rerun_by_a_new_owner_adds_them_as_the_principal(db: Path) -> None:
    from openexecutive.onboarding.commit import save_onboarding_people
    from openexecutive.onboarding.interview import PersonDraft

    pat = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    ids = save_onboarding_people([PersonDraft(full_name="Nova", role="CEO", is_principal=True)])
    assert _principals(db) == [ids["Nova"]]
    nova = store.get_person(ids["Nova"], db)
    assert nova is not None and nova.role == "CEO" and nova.authority_scope == [AuthorityScope.WILDCARD]
    assert store.get_person(pat, db) is not None


def test_the_wizard_rerun_still_adds_the_team(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The form wizard's re-run sends its principal again: skipped, as there
    is one, while the rest of the team is still added."""
    from openexecutive.onboarding import profile_builder

    pat = store.upsert_person(full_name="Pat", is_principal=True, db_path=db)
    records = [
        {"full_name": "Pat Again", "is_principal": True, "authority_scope": ["wildcard"]},
        {"full_name": "Dana", "role": "Ops", "authority_scope": []},
    ]
    monkeypatch.setattr(profile_builder, "build_people_from_answers", lambda answers: [dict(r) for r in records])
    profile_builder._save_wizard_people({})
    assert _principals(db) == [pat]
    assert {p.full_name for p in store.list_people(db_path=db)} == {"Pat", "Dana"}
