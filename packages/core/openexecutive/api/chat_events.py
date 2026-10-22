"""The events a chat turn streams (``POST /chat``, ``POST /chat/upload``).

The response is ``text/event-stream``: one ``data: <json>`` line per event,
each a JSON object whose ``type`` names it. These models describe them in
the API description, so a client checks itself against the description alone
(a renamed or reshaped event shows there). The stream itself is written by
``api.routes.chat`` from what ``Executive.chat`` yields; nothing here changes
it.

A turn ends with ``done``, after ``stopped`` when the person stopped it and
after ``error`` when it failed. ``debug_event`` rows trace the turn for the
agent-activity panel.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, RootModel

from openexecutive.api.models import AnswerSource


class _Event(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChunkEvent(_Event):
    """A piece of the reply's text, in order."""

    type: Literal["chunk"]
    content: str
    session_id: str | None = None


class ThinkingEvent(_Event):
    """The Executive is working (a round of tools); no text yet."""

    type: Literal["thinking"]
    session_id: str | None = None


class ActivityEvent(_Event):
    """What the running round of tools is doing, in words ("Reading your
    mail…"), sent just before ``thinking``. Progress only; never kept."""

    type: Literal["activity"]
    label: str
    tool: str
    iteration: int | None = None


class PhaseEvent(_Event):
    """Committee review: which step the answer is in."""

    type: Literal["phase"]
    phase: Literal["drafting", "reviewing", "finalizing"]
    session_id: str | None = None


class CommitteeCritiqueEvent(_Event):
    """Committee review: one reviewer's severity (the critique stays on the server)."""

    type: Literal["committee_critique"]
    reviewer: str
    severity: Literal["low", "medium", "high"]
    session_id: str | None = None


class ActionTakenEvent(_Event):
    """Something the Executive did, or an approval card it left
    (``decision_id``); kept with the reply as one of its ``actions``."""

    type: Literal["action_taken"]
    tool: str
    summary: str
    target: str | None = None
    link: str | None = None
    iteration: int | None = None
    decision_id: int | None = None


class FormPatchEvent(_Event):
    """Suggested values for the form on the page the person is on (the Ask
    panel); applied as suggestions, never submitted."""

    type: Literal["form_patch"]
    form_id: str
    fields: dict[str, Any]
    rationale: str = ""
    iteration: int | None = None


class SourcesEvent(_Event):
    """After the reply: what it looked at, and the areas it had to leave out."""

    type: Literal["sources"]
    session_id: str | None = None
    sources: list[AnswerSource] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)


class MessageAddedEvent(_Event):
    """The running turn took messages sent while it worked (``POST
    /chat/add``), by the ids the client gave them."""

    type: Literal["message_added"]
    ids: list[str]


class StoppedEvent(_Event):
    """The person stopped the turn; ``done`` follows on the same stream."""

    type: Literal["stopped"]
    session_id: str | None = None


class DebugEventEvent(_Event):
    """A trace row for the agent-activity panel."""

    type: Literal["debug_event"]
    kind: str
    ts: float
    data: dict[str, Any]
    turn_id: str | None = None


class ErrorEvent(_Event):
    """The turn failed (or timed out); ``done`` follows."""

    type: Literal["error"]
    message: str
    session_id: str | None = None


class DoneEvent(_Event):
    """The turn ended. ``session_id`` is the conversation (a new one's id on
    its first turn); ``message_id`` the saved reply, for 👍/👎."""

    type: Literal["done"]
    session_id: str | None = None
    message_id: int | None = None


ChatStreamEvent = Annotated[
    ChunkEvent
    | ThinkingEvent
    | ActivityEvent
    | PhaseEvent
    | CommitteeCritiqueEvent
    | ActionTakenEvent
    | FormPatchEvent
    | SourcesEvent
    | MessageAddedEvent
    | StoppedEvent
    | DebugEventEvent
    | ErrorEvent
    | DoneEvent,
    Field(discriminator="type"),
]

# The events' names, in the order a client reads about them.
EVENT_TYPES: tuple[str, ...] = (
    "chunk", "thinking", "activity", "phase", "committee_critique", "action_taken", "form_patch",
    "sources", "message_added", "stopped", "debug_event", "error", "done",
)


class ChatStreamEventSchema(RootModel[ChatStreamEvent]):
    """One event of a chat turn's stream; ``type`` names it."""


class EventStreamResponse(StreamingResponse):
    """A ``text/event-stream`` response, so the API description files the
    event schema under that media type."""

    media_type = "text/event-stream"


# What a streaming chat route declares (``responses=``, with
# ``response_class=EventStreamResponse``).
STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "model": ChatStreamEventSchema,
        "description": (
            "A text/event-stream of `data: <json>` lines, one event each, ending with `done`. "
            "Each event's `type` is one of: " + ", ".join(EVENT_TYPES) + "."
        ),
    }
}
