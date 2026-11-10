"""Auth-facing surface — the People roster as one of two inputs to who
can sign in via Google OAuth on the UI.

The Next.js layer's ``auth.ts`` calls ``GET /auth/allowed-emails`` during
the NextAuth ``signIn`` callback to decide whether to admit a logged-in
Google user. Serving the roster here means adding someone to your team is
also how you grant them access — but it is **additive**, not a
replacement: the UI unions this list with its own ``ALLOWED_EMAILS`` env
var, so a roster change can never revoke an email the operator configured
through the environment (issue #132).

This route is still gated by the shared-secret middleware — the UI's
server-side fetch carries ``x-api-key`` so an unauthenticated caller
can't enumerate emails.

``POST /auth/ended-sessions`` records a sign-in session the identity
provider ended, as the web app's back-channel logout route checked it, and
``GET /auth/ended-sessions`` lists what ended lately, which every server of
the web app reads to sign those sessions out (``memory.ended_sessions``,
docs/auth.md "Signing out"). Both are for the web app's server only: the
proxy refuses them, and a call made as someone is refused here too.

``POST /auth/directory-admit`` is the web app's sign-in asking, for an SSO
sign-in the roster refused, whether the provider's directory has just
approved that one person (``integrations.directory_sync.admit_one``). Server
only, the same way; it answers ``{admitted, code}`` and nothing the
provider said.

``GET /auth/me`` returns the email this request is made as
(``api.caller``), so a host origin's own page (docs/auth.md, "Host
origins") can warn when the session signed in here is someone else's, as
the framed pages do with the session they hold.
"""
from __future__ import annotations

import math
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from openexecutive.people import store as people_store

router = APIRouter()


class SignedInAs(BaseModel):
    # Lowercased; "" when the request names no one.
    email: str


class AllowedEmail(BaseModel):
    email: str
    person_id: int
    # While the directory sync is on, the provider's id for a Person it
    # manages: the UI then signs them in only through that provider, as
    # that identity (packages/ui/src/lib/directory.ts). None otherwise.
    directory_subject: str | None = None


@router.get(
    "/auth/allowed-emails", response_model=list[AllowedEmail], response_model_exclude_none=True
)
def allowed_emails() -> list[AllowedEmail]:
    """Return ``[{email, person_id}]`` for every active (not archived, not
    suspended) team member with an email.

    Empty list when no people are seeded yet (fresh install); the UI then
    relies on ``ALLOWED_EMAILS`` alone, so a brand-new operator can sign in
    and run onboarding.

    This list is **additive**. The UI admits an email that appears here *or*
    in its ``ALLOWED_EMAILS`` env var — this endpoint reports the roster and
    nothing else, and the union happens in ``auth.ts``. Removing a Person
    therefore does not revoke access on its own if their email is also in
    that env var.
    """
    from openexecutive.config import get_settings

    directory_on = get_settings().directory_sync_enabled
    return [
        AllowedEmail(
            email=p.email.lower(),
            person_id=p.id,  # type: ignore[arg-type]
            # Never the principal's: the principal is never from the
            # directory, so their sign-in (Google, ALLOWED_EMAILS) stays the
            # way in when the provider is wrong or the realm was re-created.
            directory_subject=(
                p.directory_subject if directory_on and p.directory_managed and not p.is_principal else None
            ),
        )
        for p in people_store.list_people()
        if p.email and p.id is not None
    ]


@router.get("/auth/me", response_model=SignedInAs)
def signed_in_as(request: Request) -> SignedInAs:
    """The email this request is made as: the signed-in person's for a call
    through the web app (verified when signed callers are on), else what
    the caller header names. Nothing about them beyond what the caller
    already sent."""
    from openexecutive.api.caller import caller

    return SignedInAs(email=caller(request).email)


# How far ahead of this clock a logout's time may be (the web app allows the
# provider's clock a minute; a little more here).
_ENDED_AT_SKEW_S = 300


class EndedSessionIn(BaseModel):
    """A provider session that ended: by ``sid``, or every session of
    ``sub`` before ``ended_at`` (seconds since the epoch). ``jti`` is the
    logout token's, taken once."""

    iss: str = Field(min_length=1, max_length=500)
    sid: str | None = Field(default=None, max_length=500)
    sub: str | None = Field(default=None, max_length=500)
    # A logout token's iat, which the web app takes within two minutes of
    # now: a time far ahead would end that person's future sign-ins too.
    # Checked in the route, so a refusal never echoes a non-finite number.
    ended_at: float
    jti: str = Field(min_length=1, max_length=500)


class EndedSessionOut(BaseModel):
    iss: str
    sid: str | None = None
    sub: str | None = None
    ended_at: float


def _server_only(request: Request) -> None:
    """The web app's server calls these with no caller: refuse one made as
    someone (through the proxy, which also refuses these paths)."""
    from openexecutive.api.caller import caller

    who = caller(request)
    if who.email or who.kind in ("user", "operator"):
        raise HTTPException(status_code=404, detail="Not Found")


@router.post("/auth/ended-sessions", status_code=204)
def record_ended_session(body: EndedSessionIn, request: Request) -> Response:
    """Record a session the provider ended. 409 for a ``jti`` seen before."""
    from openexecutive.memory import ended_sessions

    _server_only(request)
    if not body.sid and not body.sub:
        raise HTTPException(status_code=422, detail="A sid or a sub is needed")
    if not math.isfinite(body.ended_at) or not 0 <= body.ended_at <= time.time() + _ENDED_AT_SKEW_S:
        raise HTTPException(status_code=422, detail="ended_at must be a time, not ahead of now")
    if not ended_sessions.record(
        iss=body.iss, sid=body.sid, sub=body.sub, ended_at=body.ended_at, jti=body.jti,
    ):
        raise HTTPException(status_code=409, detail="That logout token was used already")
    return Response(status_code=204)


@router.get("/auth/ended-sessions", response_model=list[EndedSessionOut])
def list_ended_sessions(request: Request) -> list[EndedSessionOut]:
    """The sessions the provider ended lately (``ended_sessions.KEEP_SECONDS``)."""
    from openexecutive.memory import ended_sessions

    _server_only(request)
    return [EndedSessionOut(**row) for row in ended_sessions.recent()]


class DirectoryAdmitIn(BaseModel):
    """Who signed in: the provider's id for them and their email. Only who
    to look up; everything else is read from the provider."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=254)


class DirectoryAdmitOut(BaseModel):
    admitted: bool
    # One of directory_sync.ADMIT_CODES.
    code: str


@router.post("/auth/directory-admit", response_model=DirectoryAdmitOut)
def directory_admit(body: DirectoryAdmitIn, request: Request) -> DirectoryAdmitOut:
    """Admit the one person the web app's sign-in just refused, if the
    provider's directory says they are an active member of the group: a
    Person is created for them as the next sync run would. Rate limited;
    answers 200 whatever happened. A plain ``def``: the reads are blocking,
    so it runs in the threadpool."""
    from openexecutive.integrations import directory_sync

    _server_only(request)
    result = directory_sync.admit_one(body.subject, body.email)
    return DirectoryAdmitOut(admitted=result.admitted, code=result.code)
