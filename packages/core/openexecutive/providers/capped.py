"""``LLMProvider`` decorator that checks the cost caps before each call.

``get_provider()`` wraps every hosted backend in it, so no model call the
package makes can skip the check (``audit.cost_caps``). A call that could
pass a cap raises ``CostCapReached`` before anything is sent. The call
site's ``log_model_usage`` counts what the call spent afterwards.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from openexecutive.audit import cost_caps
from openexecutive.providers.provider import LLMProvider


class CappedProvider:
    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    async def messages_create(self, **kwargs: Any) -> Any:
        cost_caps.check(str(kwargs.get("model") or ""), kwargs)
        return await self._inner.messages_create(**kwargs)

    @asynccontextmanager
    async def _stream(self, kwargs: dict[str, Any]) -> AsyncIterator[Any]:
        cost_caps.check(str(kwargs.get("model") or ""), kwargs)
        async with self._inner.messages_stream(**kwargs) as stream:
            yield stream

    def messages_stream(self, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
        return self._stream(kwargs)
