"""``openexecutive principal --email <a>``: name who runs this install.

Onboarding makes the principal as its first step. An install whose company
comes from elsewhere (a demo installation's company is loaded with
``openexecutive demo-reset``) skips onboarding, so its operator names the
principal with this instead. Run it before anyone else can sign in: until
there is a principal, anyone signed in may edit the People list.

- With no principal yet, the team member with that address becomes it, or,
  when nobody holds the address, a new one is added with ``--name`` (and
  ``--role``). Either way they get WILDCARD authority, as onboarding gives
  the principal (onboarding/commit.py), in the same transaction, which wins
  only while there is no principal (``store.claim_principal``,
  ``store.add_principal``).
- An existing row is taken only as it could have been made by its owner:
  not a contact, suspended or demo person, the address its own and not an
  alias, and carrying no chat ids, aliases or authority someone may have put
  there while the People list was open. ``--name``, if given, must be its
  name: the command never renames someone into the principal.
- When they are already the principal, ``--name`` and ``--role`` update
  their row (a directory-managed person's name stays the directory's).
- While someone else is the principal it changes nothing: replacing the
  principal is done deliberately, on the People page.

Every change is audited (``principal_set``, private to the principal). The
API's roster cache picks it up within a minute (people/registry.py).
"""
from __future__ import annotations

import re
from pathlib import Path

from openexecutive.people import store
from openexecutive.people.models import Person

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


def _same_name(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _planted(person: Person) -> list[str]:
    """What on a row would reach or empower someone besides its owner."""
    found = [
        label
        for label, value in (
            ("a Slack id", person.slack_user_id),
            ("a Telegram chat", person.telegram_chat_id),
            ("a Discord id", person.discord_user_id),
        )
        if value
    ]
    if person.email_aliases:
        found.append("other addresses (" + ", ".join(person.email_aliases) + ")")
    if person.authority_scope:
        found.append("approval scopes (" + ", ".join(s.value for s in person.authority_scope) + ")")
    return found


def set_principal(
    email: str, *, name: str | None = None, role: str | None = None, db_path: Path | None = None
) -> str:
    """Make the person with ``email`` the principal; a line saying what changed."""
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

    if current is not None and current.id is not None:
        if current.directory_managed:
            full_name = None  # the directory owns their name
        if full_name is None and role_text is None:
            return f"{current.full_name} <{address}> is the principal already. Nothing changed."
        store.update_person(current.id, full_name=full_name, role=role_text, db_path=db_path)
        return _done(f"Updated the principal, {full_name or current.full_name} <{address}>.", current.id, False)

    if holder is not None and holder.id is not None:
        if holder.kind != "team":
            raise PrincipalError(f"{address} is a contact's address. A contact can't be the principal.")
        if holder.suspended:
            raise PrincipalError(f"{holder.full_name} is suspended. Restore them on the People page first.")
        if holder.demo:
            raise PrincipalError(f"{holder.full_name} is a demo person, who can't be the principal.")
        if store.normalize_email(holder.email or "") != address:
            raise PrincipalError(f"{address} is another address of {holder.full_name}. Use their own address.")
        if full_name is not None and not _same_name(full_name, holder.full_name):
            raise PrincipalError(
                f"{address} is {holder.full_name}'s. Leave out --name, or rename them on the People page first."
            )
        planted = _planted(holder)
        if planted:
            raise PrincipalError(
                f"{holder.full_name}'s row also has {'; '.join(planted)}, which would become the principal's."
                " Remove them on the People page if they aren't theirs, then run this again."
            )
        if not store.claim_principal(holder.id, address, db_path):
            raise PrincipalError(
                "there is a principal already (it may be suspended), or their row changed meanwhile."
                " Nothing changed."
            )
        if role_text is not None:
            store.update_person(holder.id, role=role_text, db_path=db_path)
        return _done(f"{holder.full_name} <{address}> is now the principal.", holder.id, False)

    if full_name is None:
        raise PrincipalError(f"nobody has {address} yet. Give their --name to add them.")
    person_id = store.add_principal(full_name=full_name, role=role_text or "", email=address, db_path=db_path)
    if person_id is None:
        raise PrincipalError(
            "there is a principal already (it may be suspended), or someone took the address meanwhile."
            " Nothing changed."
        )
    return _done(f"Added {full_name} <{address}> as the principal.", person_id, True)


def _done(outcome: str, person_id: int, created: bool) -> str:
    from openexecutive.audit import log_event
    from openexecutive.people import registry

    registry.invalidate()
    log_event(
        "principal_set",
        outcome,
        actor="cli",
        details={"person_id": person_id, "created": created},
        private=True,
    )
    return outcome
