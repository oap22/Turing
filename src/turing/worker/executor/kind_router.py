"""KindRouter — dispatches a SubtaskDispatch to a handler keyed by `kind`.

Issue #113 / ADR 0002 forward-compat slice. Workers register a handler per
kind string; the default kind preserves the existing research/agent loop.
Unknown kinds return a clean error result rather than crashing — this lets
new kinds roll out from the coordinator side without simultaneously updating
every worker.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from turing.coordinator.dispatch import SubtaskDispatch, SubtaskKind

KindHandler = Callable[[SubtaskDispatch], Awaitable[Any]]


class KindRouter:
    def __init__(self) -> None:
        self._handlers: dict[str, KindHandler] = {}

    def register(self, kind: str, handler: KindHandler) -> None:
        self._handlers[kind] = handler

    async def dispatch(self, envelope: SubtaskDispatch) -> Any:
        handler = self._handlers.get(envelope.kind)
        if handler is None:
            if envelope.kind == SubtaskKind.DEFAULT.value:
                default = self._handlers.get(SubtaskKind.DEFAULT.value)
                if default is not None:
                    return await default(envelope)
            return {"status": "unknown_kind", "kind": envelope.kind}
        return await handler(envelope)
