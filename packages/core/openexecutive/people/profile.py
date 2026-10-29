"""What the directory says about a person's business: country, type of
business, industry and jurisdiction (tier 1 of oe_connector's
docs/demo/demo-user-settings-plan.md, decided Oct 29, 2026).

Off unless ``DIRECTORY_PROFILES_ENABLED``: the directory sync
(``integrations.directory_sync``) then reads them from the provider's user
attributes, whose names are settings, and keeps them here.

Every value is a code from a fixed list, never free text, so nothing a
registrant types is stored: anything off-list is dropped, and so is a
jurisdiction whose ISO 3166-2 prefix isn't the person's country. They are
personal data, kept in a table of their own rather than on ``Person``, so no
roster read, People response or prompt carries them: only
``GET /people/{id}/profile`` reads them, for the principal or that person.
Nothing here reaches a prompt (tier 2 is not built).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

TABLE = "person_profiles"

BUSINESS_TYPES: frozenset[str] = frozenset({
    "wholesale", "manufacturing", "retail", "contractor", "services",
    "field-service", "nonprofit", "other",
})
INDUSTRIES: frozenset[str] = frozenset({
    "electrical", "building", "industrial", "automotive", "food", "consumer",
    "electronics", "chemicals", "healthcare", "furniture", "agriculture",
    "energy", "software", "other",
})
_US = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
)
_CA = "AB BC MB NB NL NS NT NU ON PE QC SK YT"
_AU = "ACT NSW NT QLD SA TAS VIC WA"
_GB = "ENG SCT WLS NIR"
JURISDICTIONS: frozenset[str] = frozenset(
    f"{country}-{code}"
    for country, codes in (("US", _US), ("CA", _CA), ("AU", _AU), ("GB", _GB))
    for code in codes.split()
)
# ISO 3166-1 alpha-2: two letters. The provider's own list is the authority
# for the country; a code of the right shape is kept.
_COUNTRY_LEN = 2


class PersonProfile(BaseModel):
    """A person's business settings, each a code or None."""

    country: str | None = None
    business_type: str | None = None
    industry: str | None = None
    jurisdiction: str | None = None

    def is_empty(self) -> bool:
        return not any((self.country, self.business_type, self.industry, self.jurisdiction))


def _country(raw: str | None) -> str | None:
    code = (raw or "").strip().upper()
    return code if len(code) == _COUNTRY_LEN and code.isascii() and code.isalpha() else None


def _from_list(raw: str | None, allowed: frozenset[str]) -> str | None:
    code = (raw or "").strip().lower()
    return code if code in allowed else None


def from_attributes(attributes: Mapping[str, str], names: Mapping[str, str]) -> PersonProfile:
    """The profile in a provider's user ``attributes`` (name -> first value),
    read under the attribute ``names`` configured for each field. Off-list
    values are dropped, and a jurisdiction outside the person's country."""
    def raw(field: str) -> str | None:
        name = names.get(field) or ""
        value = attributes.get(name) if name else None
        return value if isinstance(value, str) else None

    country = _country(raw("country"))
    jurisdiction = (raw("jurisdiction") or "").strip().upper() or None
    if jurisdiction not in JURISDICTIONS or country is None or not jurisdiction.startswith(country + "-"):
        jurisdiction = None
    return PersonProfile(
        country=country,
        business_type=_from_list(raw("business_type"), BUSINESS_TYPES),
        industry=_from_list(raw("industry"), INDUSTRIES),
        jurisdiction=jurisdiction,
    )


def _ensure(conn: object) -> None:
    conn.execute(  # type: ignore[attr-defined]
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        " person_id INTEGER PRIMARY KEY, country TEXT, business_type TEXT,"
        " industry TEXT, jurisdiction TEXT, updated_at TEXT NOT NULL)"
    )


def get_profile(person_id: int, db_path: Path | None = None) -> PersonProfile:
    """The person's profile; an empty one when none is kept."""
    from openexecutive.people.store import _get_conn

    with _get_conn(db_path) as conn:
        _ensure(conn)
        row = conn.execute(
            f"SELECT country, business_type, industry, jurisdiction FROM {TABLE} WHERE person_id = ?",
            (person_id,),
        ).fetchone()
    if row is None:
        return PersonProfile()
    return PersonProfile(country=row[0], business_type=row[1], industry=row[2], jurisdiction=row[3])


def set_profile(person_id: int, profile: PersonProfile, db_path: Path | None = None) -> bool:
    """Keep ``profile`` for the person; an empty one deletes theirs. True when
    it changed what was kept. Kept only while they are an active (not
    archived) team member, checked in the write itself, so a sync that read
    the roster before someone was archived can't bring their profile back."""
    from openexecutive.people.store import _get_conn

    if get_profile(person_id, db_path) == profile:
        return False
    with _get_conn(db_path) as conn:
        _ensure(conn)
        if profile.is_empty():
            cur = conn.execute(f"DELETE FROM {TABLE} WHERE person_id = ?", (person_id,))
        else:
            cur = conn.execute(
                f"INSERT INTO {TABLE} (person_id, country, business_type, industry, jurisdiction, updated_at)"
                " SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS ("
                " SELECT 1 FROM people WHERE id = ? AND archived = 0 AND kind = 'team')"
                " ON CONFLICT(person_id) DO UPDATE SET"
                " country = excluded.country, business_type = excluded.business_type,"
                " industry = excluded.industry, jurisdiction = excluded.jurisdiction,"
                " updated_at = excluded.updated_at",
                (person_id, profile.country, profile.business_type, profile.industry,
                 profile.jurisdiction, datetime.now(UTC).isoformat(), person_id),
            )
    return cur.rowcount > 0


def delete_profile(person_id: int, db_path: Path | None = None) -> None:
    """Forget the person's profile (their row is archived or deleted)."""
    set_profile(person_id, PersonProfile(), db_path)
