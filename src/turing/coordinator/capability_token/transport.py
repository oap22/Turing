"""Capability gate wire layer (ADR 0003 §7).

Subjects:
    tool_requests.<subtask_id>           # worker → coordinator
    tool_results.<request_id>.result     # coordinator → worker

This module owns:
- The :class:`ToolRequest` / :class:`ToolResult` dataclasses
- :class:`GateService` — subscribes to ``tool_requests.>``, runs steps 1-2
  (signed-frame + replay), forwards to :class:`Gate` (steps 3-8), publishes
  the result on the per-request reply subject.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from turing.coordinator.capability_token.token import CapabilityToken
from turing.transport.envelope import MeshMessage, ReplayError, ReplayWindow

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.coordinator.capability_token.gate import Gate, GateDecision
    from turing.transport.signed_transport import SignedTransport

logger = logging.getLogger(__name__)


def request_subject(subtask_id: str) -> str:
    return f"tool_requests.{subtask_id}"


def result_subject(request_id: str) -> str:
    return f"tool_results.{request_id}.result"


@dataclass(frozen=True)
class ToolRequest:
    request_id: str
    subtask_id: str
    tool: str
    args: dict[str, Any]
    capability_token: dict[str, Any] | None = None
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "request_id": self.request_id,
            "subtask_id": self.subtask_id,
            "tool": self.tool,
            "args": self.args,
            "capability_token": self.capability_token,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolRequest:
        return cls(
            version=d.get("version", 1),
            request_id=d["request_id"],
            subtask_id=d["subtask_id"],
            tool=d["tool"],
            args=d.get("args", {}),
            capability_token=d.get("capability_token"),
        )


@dataclass(frozen=True)
class ToolResult:
    request_id: str
    status: str  # ALLOWED | DENIED | ERROR
    exit_code: int | None = None
    output: str = ""
    stderr: str = ""
    duration_ms: int = 0
    error: str | None = None
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "request_id": self.request_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "output": self.output,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolResult:
        return cls(
            version=d.get("version", 1),
            request_id=d["request_id"],
            status=d["status"],
            exit_code=d.get("exit_code"),
            output=d.get("output", ""),
            stderr=d.get("stderr", ""),
            duration_ms=d.get("duration_ms", 0),
            error=d.get("error"),
        )


class GateService:
    """Subscribes to tool_requests.> and runs the gate.

    Owns steps 1-2 of ADR 0003 §8 (frame signature is enforced by the
    underlying :class:`SignedTransport` — invalid frames never reach our
    handler — and replay is enforced by :class:`ReplayWindow`). Steps 3-8
    are delegated to :class:`Gate`.
    """

    def __init__(
        self,
        *,
        transport: SignedTransport,
        gate: Gate,
        sender_id: str,
        now_ms: Callable[[], int],
        replay_window_ms: int = 60_000,
    ) -> None:
        self._transport = transport
        self._gate = gate
        self._sender_id = sender_id
        self._now_ms = now_ms
        self._replay = ReplayWindow(ttl_ms=replay_window_ms, now_ms=now_ms)

    async def subscribe(self, subtask_id: str) -> None:
        await self._transport.subscribe(
            request_subject(subtask_id),
            self._on_message,
            on_error=lambda exc: logger.warning(
                "tool_request handler error on %s: %s", subtask_id, exc
            ),
        )

    async def _on_message(self, msg: MeshMessage) -> None:
        # Step 2: replay window. (Step 1 — signature — already passed since
        # we got here through SignedTransport.)
        try:
            self._replay.observe(request_id=msg.request_id, timestamp_ms=msg.timestamp_ms)
        except ReplayError:
            # No reply for DROPPED — workers that resent will time out
            # locally if this was a stuck retry.
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
                request = ToolRequest.from_dict(payload)
            except (ValueError, KeyError):
                return
            self._gate.audit_dropped(
                subtask_id=request.subtask_id,
                worker_id=msg.sender_id,
                request_id=request.request_id,
                tool=request.tool,
                command=request.args.get("command", ""),
                reason="replay",
            )
            return

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            request = ToolRequest.from_dict(payload)
        except (ValueError, KeyError) as exc:
            logger.warning("dropping malformed tool_request: %s", exc)
            return

        token: CapabilityToken | None = None
        if request.capability_token is not None:
            try:
                token = CapabilityToken.from_dict(request.capability_token)
            except (KeyError, ValueError) as exc:
                logger.warning("malformed token on %s: %s", request.request_id, exc)
                token = None

        decision: GateDecision = await self._gate.evaluate(
            request=request, worker_id=msg.sender_id, token=token
        )

        result = ToolResult(
            request_id=request.request_id,
            status=decision.outcome,
            exit_code=decision.exit_code,
            output=decision.stdout,
            stderr=decision.stderr,
            duration_ms=decision.duration_ms,
            error=decision.reason if decision.outcome != "ALLOWED" else None,
        )
        reply = MeshMessage(
            request_id=f"reply-{request.request_id}",
            sender_id=self._sender_id,
            subject=result_subject(request.request_id),
            payload=json.dumps(result.to_dict()).encode("utf-8"),
            timestamp_ms=self._now_ms(),
        )
        await self._transport.publish(reply)


__all__ = [
    "GateService",
    "ToolRequest",
    "ToolResult",
    "request_subject",
    "result_subject",
]
