"""``LLMProvider`` decorator that holds every hosted model call to the cost
caps (``audit.cost_caps``).

``get_provider()`` wraps every hosted backend in it, so no model call the
package makes can skip it. Before a call is sent, ``cost_caps.reserve``
refuses it (``CostCapReached``) when its worst case could pass a cap that
is on, and otherwise puts that worst case on its scopes at once. Once the
call answers, ``cost_caps.settle`` replaces it with what the call cost. A
stream stopped early settles on what it had used by then; one that ended
without any usage keeps its worst case, so closing a tab never spends for
free.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from openexecutive.audit import cost_caps
from openexecutive.providers.provider import LLMProvider


def _snapshot(stream: Any) -> Any:
    # What the stream had used when it ended: Anthropic's MessageStream keeps
    # the message assembled so far, usage included; a translated backend may
    # not, and one closed before message_start has none.
    try:
        return stream.current_message_snapshot
    except Exception:  # noqa: BLE001
        pass
    # The OpenAI-compatible stream (OpenRouter) keeps its finished message
    # here instead.
    return getattr(stream, "_finalized", None)


def _not_billed(exc: BaseException) -> bool:
    """Whether a call that raised ``exc`` cost nothing: the provider answered
    with an error, or the connection failed before a request went out. A
    timeout or a cancel may have come after the provider began work, so its
    worst case stays counted."""
    import anthropic
    import httpx

    from openexecutive.providers.refusal import ProviderRefused

    # Never sent: held back after a billing refusal, refused by a nested cap,
    # or no connection made (a connect timeout included).
    if isinstance(exc, ProviderRefused | cost_caps.CostCapReached | httpx.ConnectError | httpx.ConnectTimeout):
        return True
    if isinstance(exc, anthropic.APITimeoutError | httpx.TimeoutException):
        return False
    return isinstance(exc, anthropic.APIStatusError | anthropic.APIConnectionError | httpx.HTTPStatusError)


async def _reserve(model: str, kwargs: dict[str, Any]) -> cost_caps.Reservation:
    """Reserve off the event loop; a refusal tells the principal from it."""
    try:
        return await asyncio.to_thread(cost_caps.reserve, model, kwargs, tell=False)
    except cost_caps.CostCapReached as refused:
        cost_caps.tell_principal(refused)
        raise


class CappedProvider:
    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    async def messages_create(self, **kwargs: Any) -> Any:
        model = str(kwargs.get("model") or "")
        # SQLite work off the event loop: a busy DB never stalls other turns.
        reservation = await _reserve(model, kwargs)
        try:
            message = await self._inner.messages_create(**kwargs)
        except BaseException as exc:
            if _not_billed(exc):
                await asyncio.to_thread(cost_caps.release, reservation)
            raise
        await asyncio.to_thread(cost_caps.settle, reservation, model, message)
        return message

    @asynccontextmanager
    async def _stream(self, kwargs: dict[str, Any]) -> AsyncIterator[Any]:
        model = str(kwargs.get("model") or "")
        reservation = await _reserve(model, kwargs)
        stream: Any = None
        try:
            async with self._inner.messages_stream(**kwargs) as stream:
                yield stream
        except BaseException as exc:
            if stream is None and _not_billed(exc):
                await asyncio.to_thread(cost_caps.release, reservation)
                reservation = None  # type: ignore[assignment]
            raise
        finally:
            if reservation is not None:
                await asyncio.to_thread(
                    cost_caps.settle, reservation, model, _snapshot(stream) if stream is not None else None,
                )

    def messages_stream(self, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
        return self._stream(kwargs)
