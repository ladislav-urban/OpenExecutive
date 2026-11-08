"""FastAPI routes for People.

Phase 3 surface: CRUD + archive + approver lookup.
All mutations invalidate the 60s registry cache so the next
Executive turn picks up the change. Adding, editing and archiving
people are the principal's alone (``_require_roster_owner``).

``GET /people`` lists team members only unless ``include_contacts=true`` (the
People page asks for both; pickers such as a department head or a workflow
approver keep the default and never offer a contact).

Contacts are private to the principal. ``include_contacts`` is honoured only
for a caller that resolves to the principal (``caller_is_principal``), a
contact's id reads as 404 for anyone else on the read routes — and the write
routes refuse anyone else before looking the id up — so its existence is not
revealed, and only the principal may create a contact or turn someone into
one. ``GET /people/me`` tells the UI whether to offer contacts at all.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from openexecutive.people import contact_info
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import (
    AuthorityScope,
    AvailabilityWindow,
    ContactDetail,
    Person,
    PersonKind,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from openexecutive.attunement.style import StyleProfile

router = APIRouter()


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class PersonCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="", max_length=200)
    is_principal: bool = False
    kind: PersonKind = "team"
    department_slugs: list[str] = Field(default_factory=list)
    email: str | None = None
    # Other addresses they write from: they match their mail and may be
    # emailed, but never sign in (``Person.email_aliases``).
    email_aliases: list[str] = Field(default_factory=list, max_length=10)
    slack_user_id: str | None = None
    telegram_chat_id: str | None = None
    discord_user_id: str | None = None
    # Phone, website, profiles, postal address (``Person.details``).
    details: list[ContactDetail] = Field(default_factory=list, max_length=20)
    preferred_channel: str = "any"
    response_sla_hours: int = Field(default=24, ge=1, le=8760)
    on_leave_until: date | None = None
    reports_to_person_id: int | None = None
    authority_scope: list[AuthorityScope] = Field(default_factory=list)
    availability: list[AvailabilityWindow] = Field(default_factory=list)


class PersonPatch(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    kind: PersonKind | None = None
    email: str | None = None
    # The full list; replaces the current one. Omit to leave it unchanged.
    email_aliases: list[str] | None = Field(default=None, max_length=10)
    slack_user_id: str | None = None
    telegram_chat_id: str | None = None
    discord_user_id: str | None = None
    # The full list; replaces the current one. Omit to leave it unchanged.
    details: list[ContactDetail] | None = Field(default=None, max_length=20)
    preferred_channel: str | None = None
    response_sla_hours: int | None = Field(default=None, ge=1, le=8760)
    on_leave_until: date | None = None
    clear_on_leave: bool = False
    reports_to_person_id: int | None = None
    department_slugs: list[str] | None = None
    authority_scope: list[AuthorityScope] | None = None
    availability: list[AvailabilityWindow] | None = None


# --------------------------------------------------------------------------- #
# Who is asking (contacts are the principal's alone)
# --------------------------------------------------------------------------- #

_DETAIL_NAMES = {
    "phone": "a phone number", "mobile": "a mobile number", "website": "a website",
    "linkedin": "a LinkedIn profile", "social": "a profile link", "address": "an address",
}


def _checked_details(details: list[ContactDetail]) -> list[ContactDetail]:
    """Each detail as stored, blanks dropped; 422 naming the first that
    isn't plainly its kind."""
    out: list[ContactDetail] = []
    for detail in details:
        if not detail.value.strip():
            continue
        value = contact_info.clean_detail(detail.kind, detail.value)
        if not value:
            raise HTTPException(
                status_code=422, detail=f"\"{detail.value[:60]}\" doesn't look like {_DETAIL_NAMES[detail.kind]}.",
            )
        out.append(ContactDetail(kind=detail.kind, value=value))
    return out


def caller_is_principal(request: Request) -> bool:
    """Whether the caller resolves to the principal: their signed-in email is
    the principal's, or there is no ``x-caller-email`` (the CLI, direct curl,
    local login — see ``chat._resolve_caller_person_id``). Stricter than
    ``_caller_is_principal_or_unclaimed``: with no principal on the roster
    nobody is. Fails closed."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    try:
        return people_store.is_principal_or_self(_resolve_caller_person_id(request), None)
    except Exception:
        logger.exception("people: principal check failed — contacts stay hidden")
        return False


def _demo_viewer(request: Request) -> tuple[bool, int | None]:
    """Whether the caller is a demo person (people.demo), and their id. The
    principal never is; a roster that can't be read answers yes."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.people.demo import is_demo_person

    if caller_is_principal(request):
        return False, None
    viewer = _resolve_caller_person_id(request)
    return (viewer is not None and is_demo_person(viewer)), viewer


def _visible_person(person_id: int, request: Request) -> Person:
    """The person, or 404 — also for a contact or a suspended person when
    the caller is not the principal, exactly as for an id that does not
    exist: who is suspended is the principal's business alone. A demo
    caller gets the same 404 for another demo person, and sees nobody's
    email or chat ids but their own (people.demo)."""
    from openexecutive.people.demo import shown_to_demo_viewer

    person = people_store.get_person(person_id)
    if person is None or (
        (person.kind != "team" or person.suspended) and not caller_is_principal(request)
    ):
        raise HTTPException(status_code=404, detail="Person not found")
    demo, viewer = _demo_viewer(request)
    if demo:
        shown = shown_to_demo_viewer([person], viewer)
        if not shown:
            raise HTTPException(status_code=404, detail="Person not found")
        person = shown[0]
    return _shown([person], request)[0]


def _shown(people: list[Person], request: Request) -> list[Person]:
    """``people`` as the caller may see them: a person's phone, website and
    other details (some read from their email signature) go only to the
    principal and to that person. Fails closed."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    try:
        caller = _resolve_caller_person_id(request)
        principal = people_store.is_principal_or_self(caller, None)
    except Exception:
        logger.exception("people: caller check failed — details stay hidden")
        caller, principal = None, False
    if principal:
        return people
    return [p if not p.details or (caller is not None and p.id == caller) else p.model_copy(update={"details": []})
            for p in people]


class PeopleViewer(BaseModel):
    person_id: int | None
    is_principal: bool


# --------------------------------------------------------------------------- #
# Read routes
# --------------------------------------------------------------------------- #

@router.get("/people", response_model=list[Person])
def list_people(
    request: Request,
    include_archived: bool = False,
    include_contacts: bool = False,
    include_suspended: bool = False,
) -> list[Person]:
    # Anyone but the principal gets the team as if no contact or suspended
    # person existed — ``include_archived`` included.
    principal = (include_contacts or include_suspended or include_archived) and caller_is_principal(request)
    people = people_store.list_people(
        include_archived=include_archived,
        include_contacts=include_contacts and principal,
        include_suspended=include_suspended and principal,
    )
    if principal:
        return people
    people = [p for p in people if not p.suspended]
    demo, viewer = _demo_viewer(request)
    if demo:
        from openexecutive.people.demo import shown_to_demo_viewer

        people = shown_to_demo_viewer(people, viewer)
    return _shown(people, request)


@router.get("/people/me", response_model=PeopleViewer)
def people_viewer(request: Request) -> PeopleViewer:
    """Who the caller is on the roster, so the UI can offer contacts (the
    principal's alone) only to the principal."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    return PeopleViewer(
        person_id=_resolve_caller_person_id(request),
        is_principal=caller_is_principal(request),
    )


@router.get("/people/by-scope/{token}", response_model=list[Person])
def people_by_scope(token: str, request: Request) -> list[Person]:
    """Return active people who can approve the given scope token."""
    try:
        scope = AuthorityScope(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scope token: {token!r}. Valid tokens: {[s.value for s in AuthorityScope]}",
        ) from exc
    approvers = people_store.find_approvers(scope)
    demo, viewer = _demo_viewer(request)
    if demo:
        from openexecutive.people.demo import shown_to_demo_viewer

        approvers = shown_to_demo_viewer(approvers, viewer)
    return _shown(approvers, request)


# --------------------------------------------------------------------------- #
# Roster requests — "who is this new sender?" (people.roster_requests)
#
# Declared before ``/people/{person_id}``: that route's int parameter would
# otherwise answer ``/people/requests`` with a 422. The principal's alone: a
# request names someone who wrote to them, and anyone else gets a 404, as for
# an id that does not exist.
# --------------------------------------------------------------------------- #

class RosterRequestOut(BaseModel):
    id: int
    channel: str
    channel_ref: str
    display_name: str
    profile_email: str | None
    on_company_domain: bool
    suggested_kind: str | None
    suggested_person_id: int | None
    suggested_person_name: str | None = None
    status: str
    resolved_person_id: int | None
    resolved_kind: str | None
    message_count: int
    first_seen_at: str
    last_seen_at: str
    expires_at: str
    ack_sent: bool
    # The held messages' first lines — shown on the card, never to a model.
    previews: list[str] = Field(default_factory=list)


class RosterRequestApprove(BaseModel):
    """Either ``link_person_id`` (they are someone already on the list) or
    ``full_name`` + ``kind`` (add them). ``kind`` has no default: the
    principal says whether they are on the team."""

    link_person_id: int | None = None
    full_name: str | None = Field(default=None, max_length=200)
    kind: PersonKind | None = None
    role: str = Field(default="", max_length=200)
    replace_channel_id: bool = False


def _require_principal_requests(request: Request) -> None:
    if not caller_is_principal(request):
        raise HTTPException(status_code=404, detail="Not found")


def _request_out(req: object) -> RosterRequestOut:
    from openexecutive.people import roster_requests as rr

    assert isinstance(req, rr.RosterRequest)
    suggested = (
        people_store.get_person(req.suggested_person_id)
        if req.suggested_person_id is not None else None
    )
    return RosterRequestOut(
        id=req.id,
        channel=req.channel,
        channel_ref=req.channel_ref,
        display_name=req.display_name,
        profile_email=req.profile_email,
        on_company_domain=req.on_company_domain,
        suggested_kind=req.suggested_kind,
        suggested_person_id=req.suggested_person_id if suggested is not None else None,
        suggested_person_name=suggested.full_name if suggested is not None else None,
        status=req.status,
        resolved_person_id=req.resolved_person_id,
        resolved_kind=req.resolved_kind,
        message_count=req.message_count,
        first_seen_at=req.first_seen_at,
        last_seen_at=req.last_seen_at,
        expires_at=req.expires_at,
        ack_sent=req.ack_sent_at is not None,
        previews=rr.previews(req.id) if req.status == "pending" else [],
    )


@router.get("/people/requests", response_model=list[RosterRequestOut])
def list_roster_requests(request: Request, status: str = "pending") -> list[RosterRequestOut]:
    from openexecutive.people import roster_requests as rr

    _require_principal_requests(request)
    wanted = None if status == "all" else status
    return [_request_out(r) for r in rr.list_requests(wanted, limit=100)]


@router.get("/people/requests/{request_id}", response_model=RosterRequestOut)
def get_roster_request(request_id: int, request: Request) -> RosterRequestOut:
    from openexecutive.people import roster_requests as rr

    _require_principal_requests(request)
    req = rr.get_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _request_out(req)


async def _answer_request(
    request_id: int, decision: str, body: RosterRequestApprove | None
) -> RosterRequestOut:
    from openexecutive.integrations.roster_intake import answer
    from openexecutive.people import roster_requests as rr

    kwargs: dict[str, object] = {}
    if body is not None:
        if body.link_person_id is not None:
            decision = "link"
            kwargs["link_person_id"] = body.link_person_id
            kwargs["replace_channel_id"] = body.replace_channel_id
        else:
            if not (body.full_name or "").strip() or body.kind is None:
                raise HTTPException(
                    status_code=422,
                    detail="Give their name and say whether they are on the team or a contact.",
                )
            kwargs.update(full_name=body.full_name, kind=body.kind, role=body.role)
    try:
        done = await answer(request_id, decision, via="web", **kwargs)  # type: ignore[arg-type]
    except rr.RequestNotFound as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except rr.RequestNotPending as exc:
        raise HTTPException(status_code=409, detail="That request was already answered.") from exc
    except (rr.ChannelIdConflict, people_store.AddressInUseError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _request_out(done)


@router.post("/people/requests/{request_id}/approve", response_model=RosterRequestOut)
async def approve_roster_request(
    request_id: int, body: RosterRequestApprove, request: Request
) -> RosterRequestOut:
    """Add the sender (or link them to someone on the list); their held
    messages are then answered."""
    _require_principal_requests(request)
    return await _answer_request(request_id, "approve", body)


@router.post("/people/requests/{request_id}/decline", response_model=RosterRequestOut)
async def decline_roster_request(request_id: int, request: Request) -> RosterRequestOut:
    """Leave the sender off the list; their held messages are dropped."""
    _require_principal_requests(request)
    return await _answer_request(request_id, "decline", None)


# --------------------------------------------------------------------------- #
# The identity provider's directory (integrations.directory_sync)
#
# Declared before ``/people/{person_id}`` for the same reason as the requests.
# The principal's alone: a 404 for anyone else.
# --------------------------------------------------------------------------- #

class DirectoryCandidate(BaseModel):
    person_id: int
    email: str


class DirectoryState(BaseModel):
    enabled: bool
    last_run_at: str | None = None
    last_ok_at: str | None = None
    stopped: str | None = None
    # People on the list who are active members but not linked yet: the
    # People page offers Link for each.
    candidates: list[DirectoryCandidate] = Field(default_factory=list)


@router.get("/people/directory", response_model=DirectoryState)
def directory_state(request: Request) -> DirectoryState:
    from openexecutive.config import get_settings
    from openexecutive.integrations import directory_sync

    _require_principal_requests(request)
    if not get_settings().directory_sync_enabled:
        return DirectoryState(enabled=False)
    last = directory_sync.status() or {}
    candidates = []
    for email in last.get("candidates") or []:
        person = people_store.find_person_by_email(email)
        # Still on the list, still unlinked: a run since may have linked them.
        if person is not None and person.id is not None and not person.directory_managed:
            candidates.append(DirectoryCandidate(person_id=person.id, email=email))
    return DirectoryState(
        enabled=True,
        last_run_at=last.get("last_run_at"),
        last_ok_at=last.get("last_ok_at"),
        stopped=last.get("stopped"),
        candidates=candidates,
    )


@router.post("/people/{person_id}/directory-link", response_model=Person)
def link_to_directory(person_id: int, request: Request) -> Person:
    """Let the directory manage this person: their name and email follow
    the identity provider from the next run, and leaving it suspends them.
    Needs an active member with their address."""
    from openexecutive.config import get_settings
    from openexecutive.integrations import directory_sync

    _require_roster_owner(request)
    person = _visible_person(person_id, request)
    if not get_settings().directory_sync_enabled:
        raise HTTPException(status_code=409, detail="The directory sync is off.")
    if person.kind != "team" or not person.email or not person.active:
        raise HTTPException(status_code=409, detail="Only an active team member with an email can be linked.")
    try:
        directory_sync.link(person.email)
    except directory_sync.DirectoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _reread(person_id)


@router.get("/people/{person_id}", response_model=Person)
def get_person(person_id: int, request: Request) -> Person:
    return _visible_person(person_id, request)


# --------------------------------------------------------------------------- #
# Mutation routes
# --------------------------------------------------------------------------- #

_PRINCIPAL_CONTACT_DETAIL = "The principal is always on the team and cannot be a contact."
_CONTACTS_ARE_PRIVATE = "Only the principal can add contacts or make someone a contact."


def _require_roster_owner(request: Request) -> None:
    """403 unless the caller may change the roster: the principal, or anyone
    while no one is principal yet, so a first setup can add its owner.

    A People row is also the web sign-in allow-list, the outbound-email
    allow-list and approval routing, so without this any signed-in teammate
    could put their own address on the principal's row and sign in as them.
    The same owner rule as the workspace settings; the Executive's roster
    tools (``orchestrator.people_tools``) are the principal's too.

    A person can't edit their own row either: its email and chat ids are how
    they sign in and how the Executive reaches them, and the rest decides
    their approvals and how they are chased. Their working style and open
    loops, further down, stay theirs.

    Called before the id lookup, so a refused caller can't probe which ids
    exist.
    """
    from openexecutive.api.routes.chat import _caller_is_principal_or_unclaimed

    if not _caller_is_principal_or_unclaimed(request):
        raise HTTPException(status_code=403, detail="Only the principal can change the People list")


@router.post("/people", response_model=Person, status_code=status.HTTP_201_CREATED)
def create_person(body: PersonCreate, request: Request) -> Person:
    _require_roster_owner(request)
    # Checked before anything is written, so a bad one changes nothing.
    details = _checked_details(body.details)
    if body.is_principal and body.kind != "team":
        raise HTTPException(status_code=422, detail=_PRINCIPAL_CONTACT_DETAIL)
    if body.kind != "team" and not caller_is_principal(request):
        # Before a principal exists anyone may add people, but not contacts:
        # a contact is private to a principal there is not yet.
        raise HTTPException(status_code=403, detail=_CONTACTS_ARE_PRIVATE)
    if body.authority_scope and not body.is_principal and _signed_in_non_principal(request):
        # Nor approval scopes: they are the principal's to grant, and claiming
        # the install strips any put on before (people/store.py).
        raise HTTPException(status_code=403, detail=_SCOPES_ARE_THE_PRINCIPALS)
    aliases = _checked_aliases(body.email_aliases, primary=body.email, person_id=None)
    if body.email and (
        people_store.alias_holder(body.email) is not None or _held_by_suspended(body.email, None)
    ):
        raise HTTPException(status_code=409, detail=_ADDRESS_IN_USE)
    try:
        pid = _create(body)
    except people_store.PrincipalExistsError:
        raise HTTPException(status_code=409, detail=_PRINCIPAL_EXISTS) from None
    if details:
        people_store.set_person_details(pid, details)
    scopes = list(body.authority_scope)
    if body.is_principal and AuthorityScope.WILDCARD not in scopes:
        # The principal approves anything, as onboarding and `openexecutive
        # principal` make them: added here, whatever else was asked.
        scopes.append(AuthorityScope.WILDCARD)
    if scopes:
        people_store.set_authority_scope(pid, scopes)
    if body.availability:
        people_store.set_availability(pid, body.availability)
    if aliases:
        _store_aliases(pid, aliases)
    people_registry.invalidate()
    _after_roster_write()
    person = people_store.get_person(pid)
    if person is None:
        raise HTTPException(status_code=500, detail="Person vanished after insert")
    return person


def _create(body: PersonCreate) -> int:
    return people_store.upsert_person(
        full_name=body.full_name,
        role=body.role,
        is_principal=body.is_principal,
        department_slugs=body.department_slugs,
        email=body.email,
        slack_user_id=body.slack_user_id,
        telegram_chat_id=body.telegram_chat_id,
        discord_user_id=body.discord_user_id,
        preferred_channel=body.preferred_channel,  # type: ignore[arg-type]
        response_sla_hours=body.response_sla_hours,
        on_leave_until=body.on_leave_until,
        reports_to_person_id=body.reports_to_person_id,
        kind=body.kind,
    )


_PRINCIPAL_EXISTS = "Someone else is the principal. There is one principal."
_SCOPES_ARE_THE_PRINCIPALS = "Only the principal grants approval scopes."


def _signed_in_non_principal(request: Request) -> bool:
    """A person signed in through the web app who isn't the principal: the one
    who may edit People only because nobody is the principal yet. Not the
    operator's own calls (the CLI, a backend call with the shared secret and
    no signed-in person), which keep setting scopes as before."""
    from openexecutive.api import caller as api_caller

    return api_caller.signed_in(request) and not caller_is_principal(request)
_ADDRESS_IN_USE = "That address is already on another person."
_MANAGED_IN_DIRECTORY = "Their name and email are managed in your identity provider."


def _held_by_suspended(email: str, person_id: int | None) -> bool:
    """Whether a suspended person (other than ``person_id``) holds
    ``email``: they keep their address while suspended, and a second row
    with it would sign in as them (``find_person_by_email`` skips them)."""
    holder = people_store.address_holder(email, exclude_person_id=person_id)
    if holder is None:
        return False
    person = people_store.get_person(holder)
    return bool(person is not None and person.suspended)


def _checked_aliases(
    emails: list[str], *, primary: str | None, person_id: int | None
) -> list[str]:
    """``emails`` cleaned (``people.store.clean_aliases``), or 422 for a
    value that is not an address and 409 for one that is someone else's."""
    try:
        clean = people_store.clean_aliases(emails, primary=primary)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for addr in clean:
        if people_store.address_holder(addr, exclude_person_id=person_id) is not None:
            raise HTTPException(status_code=409, detail=_ADDRESS_IN_USE)
    return clean


def _store_aliases(person_id: int, aliases: list[str]) -> None:
    try:
        people_store.set_person_emails(person_id, aliases)
    except people_store.AddressInUseError as exc:
        raise HTTPException(status_code=409, detail=_ADDRESS_IN_USE) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _after_roster_write() -> None:
    """A pending roster request whose sender now matches someone is closed
    and its messages answered (``roster_intake.after_roster_write``)."""
    from openexecutive.integrations.roster_intake import after_roster_write

    after_roster_write()


@router.patch("/people/{person_id}", response_model=Person)
def patch_person(person_id: int, body: PersonPatch, request: Request) -> Person:
    _require_roster_owner(request)
    # Checked before anything is written, so a bad one changes nothing.
    details = _checked_details(body.details) if body.details is not None else None
    existing = _visible_person(person_id, request)
    if body.kind is not None and body.kind != "team" and existing.is_principal:
        raise HTTPException(status_code=422, detail=_PRINCIPAL_CONTACT_DETAIL)
    if body.kind == "contact" and existing.kind == "team" and not caller_is_principal(request):
        raise HTTPException(status_code=403, detail=_CONTACTS_ARE_PRIVATE)

    raw = body.model_dump(exclude_unset=True)
    if "authority_scope" in raw and _signed_in_non_principal(request):
        # Before a principal exists anyone may edit People, but approval
        # scopes are the principal's to grant (as in create_person).
        raise HTTPException(status_code=403, detail=_SCOPES_ARE_THE_PRINCIPALS)
    if existing.directory_managed and (
        (body.full_name is not None and body.full_name != existing.full_name)
        or (body.email is not None and body.email != existing.email)
    ):
        raise HTTPException(status_code=409, detail=_MANAGED_IN_DIRECTORY)
    new_primary = body.email if body.email is not None else existing.email
    aliases: list[str] | None = None
    if body.email_aliases is not None:
        aliases = _checked_aliases(
            body.email_aliases, primary=new_primary, person_id=person_id
        )
    if body.email and (
        people_store.alias_holder(body.email, exclude_person_id=person_id) is not None
        or _held_by_suspended(body.email, person_id)
    ):
        raise HTTPException(status_code=409, detail=_ADDRESS_IN_USE)
    if raw:
        people_store.update_person(
            person_id,
            full_name=body.full_name,
            role=body.role,
            email=body.email,
            slack_user_id=body.slack_user_id,
            telegram_chat_id=body.telegram_chat_id,
            discord_user_id=body.discord_user_id,
            preferred_channel=body.preferred_channel,  # type: ignore[arg-type]
            response_sla_hours=body.response_sla_hours,
            on_leave_until=body.on_leave_until,
            clear_on_leave=body.clear_on_leave,
            reports_to_person_id=body.reports_to_person_id,
            department_slugs=body.department_slugs,
            kind=body.kind,
        )
    if details is not None:
        people_store.set_person_details(person_id, details)
    if "authority_scope" in raw:
        people_store.set_authority_scope(
            person_id, body.authority_scope or []
        )
    if "availability" in raw:
        people_store.set_availability(
            person_id, body.availability or []
        )
    if aliases is not None:
        _store_aliases(person_id, aliases)
    people_registry.invalidate()
    _after_roster_write()
    person = people_store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=500, detail="Person vanished")
    return person


@router.post("/people/{person_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_person(person_id: int, request: Request) -> Response:
    _require_roster_owner(request)
    _visible_person(person_id, request)
    people_store.archive_person(person_id)
    people_registry.invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _audit_suspension(event_type: str, person: Person, by: str) -> None:
    from openexecutive.audit import log_event

    verb = "suspended" if event_type == "person_suspended" else "restored"
    log_event(
        event_type,
        f"person #{person.id} {verb}",
        actor="user" if by == "principal" else "directory",
        details={"person_id": person.id, "by": by},
        private=True,
    )


@router.post("/people/{person_id}/suspend", response_model=Person)
def suspend_person(person_id: int, request: Request) -> Person:
    """Take someone out of every access decision (sign-in, chat, approvals,
    mail, chasing) without deleting anything of theirs. ``/restore`` undoes
    it. The principal's alone, and the principal can't be suspended."""
    _require_roster_owner(request)
    person = _visible_person(person_id, request)
    if person.archived:
        raise HTTPException(status_code=409, detail="This person is archived")
    if person.suspended:
        return person
    try:
        people_store.suspend_person(person_id, by="principal")
    except people_store.PrincipalSuspendError as exc:
        raise HTTPException(status_code=422, detail="The principal can't be suspended.") from exc
    people_registry.invalidate()
    _audit_suspension("person_suspended", person, "principal")
    return _reread(person_id)


@router.post("/people/{person_id}/restore", response_model=Person)
def restore_person(person_id: int, request: Request) -> Person:
    """Bring a suspended person back with their loops, working style and
    addresses as they were."""
    _require_roster_owner(request)
    person = _visible_person(person_id, request)
    if person.archived:
        raise HTTPException(status_code=409, detail="This person is archived")
    if not person.suspended:
        return person
    people_store.restore_person(person_id)
    people_registry.invalidate()
    _after_roster_write()
    _audit_suspension("person_restored", person, "principal")
    return _reread(person_id)


def _reread(person_id: int) -> Person:
    person = people_store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=500, detail="Person vanished")
    return person


# --------------------------------------------------------------------------- #
# Open loops (attunement) — what each person owes
# --------------------------------------------------------------------------- #

class AssignNoticeOut(BaseModel):
    """Whether the person was told about a task just assigned to them:
    ``sent`` (on ``channel``), ``held`` for their quiet hours or brief,
    ``not_reached`` (no channel worked) or ``skipped`` (their own task, or
    notices are off). A teammate assigning gets no channel, and ``later``
    in place of ``held``: those are the person's own settings."""

    status: str
    channel: str | None = None


class OpenLoopOut(BaseModel):
    loop_id: int
    owner_person_id: int
    owner_name: str
    description: str
    due_at: str
    created_at: str
    # Set only on the response to an assignment.
    notified: AssignNoticeOut | None = None


class OpenLoopClose(BaseModel):
    reason: str = Field(default="done", pattern="^(done|not_needed|cancelled)$")


def _require_principal_or(person_id: int, request: Request) -> int:
    """The resolved caller, if it is ``person_id`` or the principal; else 403."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    caller = _resolve_caller_person_id(request)
    if caller is None or not people_store.is_principal_or_self(caller, person_id):
        raise HTTPException(status_code=403, detail="Only the principal or the owner")
    return int(caller)


@router.get("/people/{person_id}/profile")
def get_person_profile(person_id: int, request: Request) -> dict[str, Any]:
    """Their business profile from the directory (people.profile): country,
    type of business, industry and jurisdiction, as codes. Personal data, so
    the principal's or that person's own; anyone else, and every caller while
    profiles are off (DIRECTORY_PROFILES_ENABLED), gets 404 as for an id that
    does not exist."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.config import get_settings
    from openexecutive.people.profile import get_profile

    caller = _resolve_caller_person_id(request)
    if (
        not get_settings().directory_profiles_enabled
        or caller is None
        or not people_store.is_principal_or_self(caller, person_id)
        or people_store.get_person(person_id) is None
    ):
        raise HTTPException(status_code=404, detail="Person not found")
    return get_profile(person_id).model_dump()


@router.get("/people/{person_id}/open-loops", response_model=list[OpenLoopOut])
def get_person_open_loops(person_id: int, request: Request) -> list[OpenLoopOut]:
    """Open loops this person owns, soonest due first. The principal or that
    person only — what someone owes is not roster-public."""
    from openexecutive.attunement.open_loops import list_open_loops

    _visible_person(person_id, request)
    _require_principal_or(person_id, request)
    return [
        OpenLoopOut(
            loop_id=loop.id,
            owner_person_id=loop.owner_person_id,
            owner_name=loop.owner_name,
            description=loop.description,
            due_at=loop.due_at,
            created_at=loop.created_at,
        )
        for loop in list_open_loops(person_id=person_id, limit=100)
    ]


class OpenLoopCreate(BaseModel):
    task: str = Field(min_length=1, max_length=200)
    # A local date; the loop is due at 17:00 that day in the user's timezone
    # (clamped to 60 days out). Omit for the default due window.
    due_date: date | None = None


# Why an assignment was refused, as the People page shows it.
_ASSIGN_REFUSED: dict[str, tuple[int, str]] = {
    "disabled": (409, "Open loops are turned off for this workspace"),
    "unknown_owner": (404, "Person not found"),
    "owner_is_contact": (409, "Tasks are assigned to team members, not contacts"),
    "owner_archived": (409, "This person is archived"),
    "owner_suspended": (409, "This person is suspended"),
    "unknown_assigner": (403, "Only someone on the team can assign a task"),
    "missing_text": (422, "Describe the task"),
    "owner_at_cap": (409, "They already have as many open loops as they can carry — close some first"),
    "duplicate": (409, "That task is already open for them"),
}


@router.post(
    "/people/{person_id}/open-loops",
    response_model=OpenLoopOut,
    status_code=status.HTTP_201_CREATED,
)
async def assign_person_open_loop(
    person_id: int, body: OpenLoopCreate, request: Request
) -> OpenLoopOut:
    """Assign this person a task: an open loop, followed up once it is due.

    The principal or any active team member may assign one to anyone on the
    team (themselves included). Someone assigned a task by another person is
    told once on their own channel (``attunement.assignment_notice``), and
    ``notified`` says how that went. Listing stays the
    principal's or the owner's (``GET`` above), so a teammate who assigns Ben
    a task gets it back here but does not see Ben's other loops."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.attunement.assignment_notice import notify_assignee
    from openexecutive.attunement.open_loops import assign_open_loop, get_open_loop

    _visible_person(person_id, request)
    caller = _resolve_caller_person_id(request)
    assigner = people_store.get_person(caller) if caller is not None else None
    if assigner is None or assigner.id is None or not assigner.active or assigner.kind != "team":
        raise HTTPException(status_code=403, detail="Only someone on the team can assign a task")
    result = assign_open_loop(
        owner_person_id=person_id, text=body.task, assigned_by_person_id=assigner.id,
        due_date=body.due_date,
    )
    loop = get_open_loop(result.loop_id) if result.loop_id is not None else None
    if loop is None:
        reason = result.reason or ""
        if reason == "owner_is_contact" and not caller_is_principal(request):
            # Became a contact after the check above: still not theirs to see.
            reason = "unknown_owner"
        code, detail = _ASSIGN_REFUSED.get(reason, (409, "Could not assign the task"))
        raise HTTPException(status_code=code, detail=detail)
    notice = await notify_assignee(
        loop_id=loop.id, owner_person_id=person_id, assigned_by_person_id=assigner.id,
        task=body.task, due_at=loop.due_at,
    )
    return OpenLoopOut(
        loop_id=loop.id,
        owner_person_id=loop.owner_person_id,
        owner_name=loop.owner_name,
        description=loop.description,
        due_at=loop.due_at,
        created_at=loop.created_at,
        notified=AssignNoticeOut.model_validate(
            notice.as_dict(for_principal=bool(assigner.is_principal))
        ),
    )


@router.post("/open-loops/{loop_id}/close", status_code=status.HTTP_204_NO_CONTENT)
def close_open_loop_route(loop_id: int, body: OpenLoopClose, request: Request) -> Response:
    """Close one open loop. Only the principal or the loop's owner may."""
    from openexecutive.attunement.open_loops import close_open_loop, get_open_loop

    loop = get_open_loop(loop_id)
    if loop is None:
        raise HTTPException(status_code=404, detail="Open loop not found")
    caller = _require_principal_or(loop.owner_person_id, request)
    close_open_loop(loop_id, reason=body.reason, closed_by_person_id=caller)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class OutreachStat(BaseModel):
    source: str
    label: str
    sent: int
    replied: int
    acted: int
    ignored: int
    pending: int


@router.get("/people/{person_id}/outreach", response_model=list[OutreachStat])
def get_person_outreach(person_id: int, request: Request) -> list[OutreachStat]:
    """How this person responded to proactive outreach over the last 30 days,
    per kind of outreach. The principal or that person only."""
    from openexecutive.attunement import outcomes

    # Authorize first, so a non-principal can't probe which ids exist.
    _require_principal_or(person_id, request)
    if people_store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    rows: list[OutreachStat] = []
    for (_, source), s in sorted(outcomes.acceptance(person_id=person_id).items()):
        rows.append(OutreachStat(
            source=source,
            label=outcomes.SOURCE_LABELS.get(source, source),
            sent=s.sent,
            replied=s.counts.get(outcomes.OUTCOME_REPLIED, 0),
            acted=s.counts.get(outcomes.OUTCOME_ACTED, 0),
            ignored=s.counts.get(outcomes.OUTCOME_IGNORED, 0),
            pending=s.pending,
        ))
    return rows


# ---------------------------------------------------------------------------
# Working style (attunement) — how this person likes replies
# ---------------------------------------------------------------------------


class WorkingStyleRule(BaseModel):
    text: str
    basis: str


class WorkingStyleOut(BaseModel):
    rules: list[WorkingStyleRule]
    locked: bool
    updated_at: str | None = None
    updated_by: str | None = None


class WorkingStyleIn(BaseModel):
    """``rules`` omitted keeps the current rules (and their provenance) and
    only sets the lock."""

    rules: list[str] | None = Field(default=None, max_length=8)  # style.MAX_RULES
    locked: bool = False


def _style_out(profile: StyleProfile) -> WorkingStyleOut:
    return WorkingStyleOut(
        rules=[WorkingStyleRule(text=r.text, basis=r.basis) for r in profile.rules],
        locked=profile.locked,
        updated_at=profile.updated_at,
        updated_by=profile.updated_by,
    )


def _style_person(person_id: int, request: Request) -> int:
    """Authorize (principal or that person), then 404 an unknown or archived
    id — archiving drops the profile, and nothing may re-create it."""
    caller = _require_principal_or(person_id, request)
    person = people_store.get_person(person_id)
    if person is None or person.archived:
        raise HTTPException(status_code=404, detail="Person not found")
    return caller


@router.get("/people/{person_id}/attunement", response_model=WorkingStyleOut)
def get_person_working_style(person_id: int, request: Request) -> WorkingStyleOut:
    """The short working-style rules pinned into this person's turns. The
    principal or that person only."""
    from openexecutive.attunement.style import get_profile

    _style_person(person_id, request)
    return _style_out(get_profile(person_id))


@router.put("/people/{person_id}/attunement", response_model=WorkingStyleOut)
def put_person_working_style(
    person_id: int, body: WorkingStyleIn, request: Request
) -> WorkingStyleOut:
    """Replace the rules and set the lock. Rules pass the same style-only
    checks as learned ones (they reach a tool-capable turn). Rules typed here
    are kept by the learning pass, which only fills the remaining slots; a
    locked profile is never rewritten by it at all."""
    from openexecutive.attunement.style import (
        BASIS_EDITED,
        StyleRule,
        get_profile,
        save_profile,
        validate_edited_rule,
    )

    caller = _style_person(person_id, request)
    if body.rules is None:
        # Only the lock changes; rules stored under an older check that no
        # longer pass it are dropped rather than carried forward.
        current = [r for r in get_profile(person_id).rules if validate_edited_rule(r.text)[1] is None]
        return _style_out(save_profile(person_id, current, locked=body.locked,
                                       updated_by=f"person:{caller}"))
    rules: list[StyleRule] = []
    for raw in body.rules:
        text, rejection = validate_edited_rule(raw)
        if rejection:
            raise HTTPException(
                status_code=422,
                detail=f"Rule not accepted ({rejection}): keep each rule to one short "
                "sentence about how replies are written — no actions, people, links "
                "or amounts.",
            )
        if text.lower() not in {r.text.lower() for r in rules}:
            rules.append(StyleRule(text=text, basis=BASIS_EDITED))
    return _style_out(save_profile(person_id, rules, locked=body.locked,
                                   updated_by=f"person:{caller}"))


@router.delete("/people/{person_id}/attunement", status_code=status.HTTP_204_NO_CONTENT)
def delete_person_working_style(person_id: int, request: Request) -> Response:
    """Forget the rules and unlock, so they are re-learned from scratch."""
    from openexecutive.attunement.style import reset_profile

    caller = _style_person(person_id, request)
    reset_profile(person_id, updated_by=f"person:{caller}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
