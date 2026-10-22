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

``GET /auth/me`` returns the email this request is made as
(``api.caller``), so a host origin's own page (docs/auth.md, "Host
origins") can warn when the session signed in here is someone else's, as
the framed pages do with the session they hold.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from openexecutive.people import store as people_store

router = APIRouter()


class SignedInAs(BaseModel):
    # Lowercased; "" when the request names no one.
    email: str


class AllowedEmail(BaseModel):
    email: str
    person_id: int


@router.get("/auth/allowed-emails", response_model=list[AllowedEmail])
def allowed_emails() -> list[AllowedEmail]:
    """Return ``[{email, person_id}]`` for every non-archived Person with an email.

    Empty list when no people are seeded yet (fresh install); the UI then
    relies on ``ALLOWED_EMAILS`` alone, so a brand-new operator can sign in
    and run onboarding.

    This list is **additive**. The UI admits an email that appears here *or*
    in its ``ALLOWED_EMAILS`` env var — this endpoint reports the roster and
    nothing else, and the union happens in ``auth.ts``. Removing a Person
    therefore does not revoke access on its own if their email is also in
    that env var.
    """
    return [
        AllowedEmail(email=p.email.lower(), person_id=p.id)  # type: ignore[arg-type]
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


class EndedSessionIn(BaseModel):
    """A provider session that ended: by ``sid``, or every session of
    ``sub`` before ``ended_at`` (seconds since the epoch). ``jti`` is the
    logout token's, taken once."""

    iss: str = Field(min_length=1, max_length=500)
    sid: str | None = Field(default=None, max_length=500)
    sub: str | None = Field(default=None, max_length=500)
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
