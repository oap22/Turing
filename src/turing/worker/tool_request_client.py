"""Worker-side carrier for shell tool_requests (ADR 0003 §7).

Mirrors :class:`SubtaskDispatchClient`: subscribes to the per-request reply
subject, publishes the request, awaits the result. The token presented on
each request is the one the worker received in
``SubtaskDispatch.capability_token``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from turing.coordinator.capability_token.transport import (
    ToolRequest,
    ToolResult,
    request_subject,
    result_subject,
)
from turing.transport.envelope import MeshMessage

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.coordinator.capability_token.token import CapabilityToken
    from turing.transport.signed_transport import SignedTransport

logger = logging.getLogger(__name__)


class ToolRequestTimeoutError(TimeoutError):
    """Raised when no ToolResult arrives within the supplied deadline."""


class ToolRequestClient:
    def __init__(
        self,
        *,
        transport: SignedTransport,
        sender_id: str,
        now_ms: Callable[[], int],
    ) -> None:
        self._transport = transport
        self._sender_id = sender_id
        self._now_ms = now_ms
        self._pending: dict[str, asyncio.Future[ToolResult]] = {}

    async def request(
        self,
        *,
        subtask_id: str,
        tool: str,
        args: dict[str, Any],
        token: CapabilityToken,
        timeout_s: float = 30.0,
    ) -> ToolResult:
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolResult] = loop.create_future()
        self._pending[request_id] = future

        async def _on_result(msg: MeshMessage) -> None:
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
                result = ToolResult.from_dict(payload)
            except (ValueError, KeyError) as exc:
                logger.warning("dropping malformed tool_result: %s", exc)
                return
            pending = self._pending.get(result.request_id)
            if pending is None or pending.done():
                return
            pending.set_result(result)

        await self._transport.subscribe(result_subject(request_id), _on_result)

        envelope = ToolRequest(
            request_id=request_id,
            subtask_id=subtask_id,
            tool=tool,
            args=args,
            capability_token=token.to_dict(),
        )
        msg = MeshMessage(
            request_id=request_id,
            sender_id=self._sender_id,
            subject=request_subject(subtask_id),
            payload=json.dumps(envelope.to_dict()).encode("utf-8"),
            timestamp_ms=self._now_ms(),
        )
        await self._transport.publish(msg)

        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError as exc:
            raise ToolRequestTimeoutError(
                f"no tool_result for {request_id} within {timeout_s}s"
            ) from exc
        finally:
            self._pending.pop(request_id, None)
