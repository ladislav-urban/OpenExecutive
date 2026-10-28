"""``openexecutive principal --email <a>``: name who runs this install.

Onboarding makes the principal as its first step. An install whose company
comes from elsewhere (a demo installation's company is loaded with
``openexecutive demo-reset``) skips onboarding, so its operator names the
principal with this instead, before or after signing in:

- With no principal yet, the team member with that address becomes it, or,
  when there is none, a new one is added with ``--name`` (and ``--role``).
  Either way they get WILDCARD authority, as onboarding gives the principal
  (onboarding/commit.py).
  A contact, a suspended person and a demo person can't be the principal.
  Nobody else holds the address, as the People page requires.
- When they are already the principal, ``--name`` and ``--role`` update
  their row (a directory-managed person's name stays the directory's).
- While someone else is the principal it changes nothing: replacing the
  principal is done deliberately, on the People page.

Every change is audited (``principal_set``). The API's roster cache picks it
up within a minute (people/registry.py), so the API need not restart.
"""
from __future__ import annotations

import re
from pathlib import Path

from openexecutive.people import store
from openexecutive.people.models import AuthorityScope

EMAIL_RE = re.compile(r"^[^\s@<>\"']+@[^\s@<>\"']+\.[^\s@<>\"']+$")
MAX_NAME = 120
MAX_ROLE = 120


class PrincipalError(ValueError):
    """Nothing was changed, and why."""


def _clean(value: str | None, limit: int, what: str) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    if not text or len(text) > limit:
        raise PrincipalError(f"the {what} must be 1 to {limit} characters")
    return text


def set_principal(
    email: str, *, name: str | None = None, role: str | None = None, db_path: Path | None = None
) -> str:
    """Make the person with ``email`` the principal; a line saying what changed."""
    from openexecutive.audit import log_event
    from openexecutive.people import registry

    address = store.normalize_email(email)
    if not address or not EMAIL_RE.match(address):
        raise PrincipalError(f"{email!r} is not an email address")
    full_name = _clean(name, MAX_NAME, "name")
    role_text = _clean(role, MAX_ROLE, "role")
    store.initialize_db(db_path)

    current = store.find_principal_person(db_path)
    holder_id = store.address_holder(address, db_path)
    holder = store.get_person(holder_id, db_path) if holder_id is not None else None

    if current is not None and current.id != holder_id:
        raise PrincipalError(
            f"{current.full_name} is the principal already. Change the principal on the People page."
        )

    if current is not None:
        if current.directory_managed and full_name is not None:
            full_name = None  # the directory owns their name
        if full_name is None and role_text is None:
            return f"{current.full_name} <{address}> is the principal already. Nothing changed."
        store.update_person(current.id, full_name=full_name, role=role_text, db_path=db_path)  # type: ignore[arg-type]
        outcome = f"Updated the principal, {full_name or current.full_name} <{address}>."
        person_id, created = current.id, False
    elif holder is not None:
        if holder.kind != "team":
            raise PrincipalError(f"{address} is a contact's address. A contact can't be the principal.")
        if holder.suspended:
            raise PrincipalError(f"{holder.full_name} is suspended. Restore them on the People page first.")
        if holder.demo:
            raise PrincipalError(f"{holder.full_name} is a demo person, who can't be the principal.")
        if store.normalize_email(holder.email or "") != address:
            raise PrincipalError(f"{address} is another address of {holder.full_name}. Use their own address.")
        if holder.id is None or not store.claim_principal(holder.id, db_path):
            raise PrincipalError("someone became the principal meanwhile. Nothing changed.")
        store.set_authority_scope(holder.id, [AuthorityScope.WILDCARD], db_path=db_path)
        if holder.directory_managed:
            full_name = None
        if full_name is not None or role_text is not None:
            store.update_person(holder.id, full_name=full_name, role=role_text, db_path=db_path)
        outcome = f"{full_name or holder.full_name} <{address}> is now the principal."
        person_id, created = holder.id, False
    else:
        if full_name is None:
            raise PrincipalError(f"nobody has {address} yet. Give their --name to add them.")
        person_id = store.upsert_person(
            full_name=full_name, role=role_text or "", email=address, is_principal=True, kind="team", db_path=db_path
        )
        # Someone may have become the principal meanwhile: keep the oldest.
        winner = store.find_principal_person(db_path)
        if winner is None or winner.id != person_id:
            store.archive_person(person_id, db_path)
            raise PrincipalError("someone became the principal meanwhile. Nothing changed.")
        store.set_authority_scope(person_id, [AuthorityScope.WILDCARD], db_path=db_path)
        outcome = f"Added {full_name} <{address}> as the principal."
        created = True

    registry.invalidate()
    log_event(
        "principal_set",
        outcome,
        actor="cli",
        details={"person_id": person_id, "created": created},
    )
    return outcome
