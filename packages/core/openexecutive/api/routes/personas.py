"""REST endpoints for voice persona management.

GET  /personas            — list all (built-in + custom)
GET  /personas/{slug}     — get one persona with full body
PUT  /personas/{slug}     — upsert (create or update) a persona
POST /personas            — create new custom persona (slug auto-derived)
POST /personas/{slug}/reset — restore built-in, deleting any DB override
DELETE /personas/{slug}   — delete a persona (built-in cannot be deleted, only reset)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from openexecutive.api.principal_gate import principal_only
from openexecutive.personas.loader import (
    Persona,
    PersonaMeta,
    create_persona,
    delete_persona,
    get_persona,
    list_personas,
    persona_exists,
    reset_persona,
    upsert_persona,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# A persona's body is the Executive's voice in its system prompt, which every
# turn of the principal's runs on: only the principal writes one
# (api.principal_gate), refused before any work. Reads stay open.
PRINCIPAL_ONLY_DETAIL = "Only the principal can change the personas."
_principal_only = principal_only(PRINCIPAL_ONLY_DETAIL)


class PersonaUpsertRequest(BaseModel):
    display_name: str
    body: str


class PersonaCreateRequest(BaseModel):
    display_name: str
    body: str


@router.get("/personas", response_model=list[PersonaMeta])
def list_all_personas() -> list[PersonaMeta]:
    return list_personas()


@router.get("/personas/{slug}", response_model=Persona)
def get_one_persona(slug: str) -> Persona:
    p = get_persona(slug)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Persona {slug!r} not found")
    return p


@router.put("/personas/{slug}", response_model=Persona, dependencies=[Depends(_principal_only)])
def upsert_one_persona(slug: str, req: PersonaUpsertRequest) -> Persona:
    if not req.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name must not be empty")
    if not req.body.strip():
        raise HTTPException(status_code=400, detail="body must not be empty")
    return upsert_persona(slug, req.display_name, req.body)


@router.post(
    "/personas", response_model=Persona, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_principal_only)],
)
def create_new_persona(req: PersonaCreateRequest) -> Persona:
    if not req.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name must not be empty")
    if not req.body.strip():
        raise HTTPException(status_code=400, detail="body must not be empty")
    return create_persona(req.display_name, req.body)


@router.post("/personas/{slug}/reset", response_model=Persona, dependencies=[Depends(_principal_only)])
def reset_one_persona(slug: str) -> Persona:
    p = reset_persona(slug)
    if p is None:
        raise HTTPException(
            status_code=400,
            detail=f"Persona {slug!r} has no built-in to reset to",
        )
    return p


@router.delete(
    "/personas/{slug}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_principal_only)],
)
def delete_one_persona(slug: str) -> Response:
    if not persona_exists(slug):
        raise HTTPException(status_code=404, detail=f"Persona {slug!r} not found")

    from openexecutive.personas.loader import _get_builtins
    builtins = _get_builtins()
    if slug in builtins:
        raise HTTPException(
            status_code=400,
            detail=f"Persona {slug!r} is a built-in and cannot be deleted. Use /reset to restore it.",
        )

    delete_persona(slug)

    # If this persona is currently active for the executive, reset to default.
    _maybe_reset_executive_persona(slug)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _maybe_reset_executive_persona(deleted_slug: str) -> None:
    """If the executive's active voice persona was just deleted, reset to default."""
    try:
        from openexecutive.agents.overrides import (
            EXECUTIVE_AGENT_ID,
            get_override,
            set_override,
        )
        ov = get_override(EXECUTIVE_AGENT_ID)
        if ov is not None and ov.voice_persona_slug == deleted_slug:
            set_override(
                EXECUTIVE_AGENT_ID,
                voice_persona_slug=None,
                voice_persona_slug_set=True,
            )
    except Exception:
        logger.exception("Failed to reset executive voice persona after deletion")
