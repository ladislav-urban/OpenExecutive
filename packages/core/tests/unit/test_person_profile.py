"""Business profiles from the directory (people/profile.py): codes from fixed
lists, written by the sync only while DIRECTORY_PROFILES_ENABLED, read only by
the principal or that person, never on Person or in a prompt."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.people import profile
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from tests.unit.test_directory_sync import Keycloak, by_email, env, kc, principal, run  # noqa: F401

NAMES = {"country": "country", "business_type": "businessType", "industry": "industry",
         "jurisdiction": "jurisdiction"}


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ({"country": "us", "businessType": "Wholesale", "industry": "electrical", "jurisdiction": "us-tx"},
         profile.PersonProfile(country="US", business_type="wholesale", industry="electrical", jurisdiction="US-TX")),
        # Off-list values are dropped, field by field.
        ({"country": "GB", "businessType": "pirate", "industry": "<b>x</b>", "jurisdiction": "GB-SCT"},
         profile.PersonProfile(country="GB", jurisdiction="GB-SCT")),
        # A jurisdiction outside the person's country, or with no country, is dropped.
        ({"country": "CA", "jurisdiction": "US-TX"}, profile.PersonProfile(country="CA")),
        ({"jurisdiction": "AU-NSW"}, profile.PersonProfile()),
        ({"country": "U1", "jurisdiction": "US-ZZ"}, profile.PersonProfile()),
        ({}, profile.PersonProfile()),
    ],
)
def test_from_attributes_keeps_only_listed_codes(attrs: dict[str, str], expected: profile.PersonProfile) -> None:
    assert profile.from_attributes(attrs, NAMES) == expected


def test_the_lists_are_the_approved_ones() -> None:
    assert len(profile.BUSINESS_TYPES) == 8 and len(profile.INDUSTRIES) == 14
    by_country: dict[str, int] = {}
    for code in profile.JURISDICTIONS:
        by_country[code.split("-")[0]] = by_country.get(code.split("-")[0], 0) + 1
    assert by_country == {"US": 51, "CA": 13, "AU": 8, "GB": 4}


def _set_attrs(realm: Keycloak, username: str, **attrs: str) -> None:
    realm.user(username)["attributes"] = {k: [v] for k, v in attrs.items()}


def test_the_sync_keeps_a_profile_only_while_profiles_are_on(
    kc: Keycloak, principal: int, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    _set_attrs(kc, "accountant", country="US", businessType="retail", industry="food", jurisdiction="US-CA")
    run(kc)
    accountant = by_email("dir-accountant@example.com")
    assert profile.get_profile(accountant.id).is_empty()  # off by default

    monkeypatch.setenv("DIRECTORY_PROFILES_ENABLED", "true")
    report = run(kc)
    assert profile.get_profile(accountant.id) == profile.PersonProfile(
        country="US", business_type="retail", industry="food", jurisdiction="US-CA"
    )
    assert ("profile updated", "dir-accountant@example.com", "") in report.changes
    # A changed attribute follows; an unchanged run writes nothing.
    assert ("profile updated", "dir-accountant@example.com", "") not in run(kc).changes
    _set_attrs(kc, "accountant", country="US", businessType="retail", industry="pirates")
    run(kc)
    assert profile.get_profile(accountant.id).industry is None


def test_attribute_names_are_settings(kc: Keycloak, principal: int, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("DIRECTORY_PROFILES_ENABLED", "true")
    monkeypatch.setenv("DIRECTORY_PROFILE_ATTR_INDUSTRY", "dc_industry")
    _set_attrs(kc, "adviser", country="AU", dc_industry="energy", industry="food")
    run(kc)
    assert profile.get_profile(by_email("dir-adviser@example.com").id).industry == "energy"


def test_nothing_reaches_person_and_archiving_forgets_it(kc: Keycloak, principal: int, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("DIRECTORY_PROFILES_ENABLED", "true")
    _set_attrs(kc, "adviser", country="US", industry="software")
    run(kc)
    adviser = by_email("dir-adviser@example.com")
    assert "industry" not in adviser.model_dump() and "software" not in adviser.model_dump_json()
    people_store.archive_person(adviser.id)
    assert profile.get_profile(adviser.id).is_empty()


def _client() -> TestClient:
    from openexecutive.api.routes import people as people_route

    app = FastAPI()
    app.include_router(people_route.router)
    return TestClient(app)


def test_only_the_principal_or_the_person_reads_it(kc: Keycloak, principal: int, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("DIRECTORY_PROFILES_ENABLED", "true")
    _set_attrs(kc, "adviser", country="US", industry="software")
    run(kc)
    people_registry.invalidate()
    adviser = by_email("dir-adviser@example.com").id
    client = _client()

    def get(who: str) -> Any:
        return client.get(f"/people/{adviser}/profile", headers={"x-caller-email": who})

    assert get("dir-owner@example.com").json()["industry"] == "software"
    assert get("dir-adviser@example.com").json() == {
        "country": "US", "business_type": None, "industry": "software", "jurisdiction": None,
    }
    assert get("dir-accountant@example.com").status_code == 404  # a teammate: as if no such person
    monkeypatch.setenv("DIRECTORY_PROFILES_ENABLED", "false")
    assert get("dir-owner@example.com").status_code == 404  # off: not there at all


def test_an_archived_or_contact_person_gets_no_profile_back(principal: int) -> None:  # noqa: F811
    pid = people_store.upsert_person(full_name="Gone Soon", email="gone@example.com")
    people_store.archive_person(pid)
    # A sync that read the roster before the archive writes nothing.
    assert not profile.set_profile(pid, profile.PersonProfile(country="US"))
    assert profile.get_profile(pid).is_empty()
    other = people_store.upsert_person(full_name="Now Contact", email="contact@example.com")
    assert profile.set_profile(other, profile.PersonProfile(country="GB"))
    people_store.update_person(other, kind="contact")
    assert profile.get_profile(other).is_empty()


def test_a_profile_write_that_fails_is_a_warning_not_a_failed_person(
    kc: Keycloak, principal: int, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setenv("DIRECTORY_PROFILES_ENABLED", "true")
    _set_attrs(kc, "adviser", country="US", industry="software")

    def locked(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(profile, "set_profile", locked)
    report = run(kc)
    assert ("created", "dir-adviser@example.com", "") in report.changes
    assert any(c[0] == "warning" and c[1] == "dir-adviser@example.com" for c in report.changes)
    assert not any(c[0] == "failed" for c in report.changes)


def test_profile_writes_count_as_the_runs_writes(kc: Keycloak, principal: int, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    run(kc)  # everyone created, profiles off
    monkeypatch.setenv("DIRECTORY_PROFILES_ENABLED", "true")
    _set_attrs(kc, "adviser", country="US", industry="software")
    assert run(kc).wrote  # only the profile changed, and it is audited as a write
