"""Capability gate (ADR 0003 §8) — the 8-step verification + shell executor.

The transport layer (``capability_token.transport``) handles steps 1-2
(signed-frame check + replay window) and calls into ``Gate`` for the
unwrapped 3-8 path. Both DROPPED and ALLOWED/DENIED outcomes flow through
the same audit log.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from turing.coordinator.capability_token.audit import GateAuditLog, GateAuditRow
from turing.coordinator.capability_token.token import (
    CapabilityToken,
    CapabilityVerifier,
    ScopeViolationError,
    TokenSignatureError,
)
from turing.coordinator.lifecycle.lifecycle import SubtaskState

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from turing.coordinator.capability_token.transport import ToolRequest


_DROPPED = "DROPPED"
_DENIED = "DENIED"
_ALLOWED = "ALLOWED"

_MAX_OUTPUT_BYTES = 4096


class SubtaskStateLookup(Protocol):
    def state_of(self, subtask_id: str) -> SubtaskState: ...


@dataclass(frozen=True)
class GateDecision:
    outcome: str
    reason: str
    exit_code: int | None = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""


class Gate:
    """Coordinator-side shell gate.

    Owns the verifier (signature step), lifecycle lookup (state step),
    workspace dir resolution, and the actual subprocess.
    """

    def __init__(
        self,
        *,
        verifier: CapabilityVerifier,
        lifecycle: SubtaskStateLookup,
        workspace_root: Path,
        audit_log: GateAuditLog,
        now_ms: Callable[[], int],
    ) -> None:
        self._verifier = verifier
        self._lifecycle = lifecycle
        self._workspace_root = workspace_root
        self._audit = audit_log
        self._now_ms = now_ms

    # ── audit helpers used by transport layer (steps 1-2) ────────────
    def audit_dropped(
        self,
        *,
        subtask_id: str,
        worker_id: str,
        request_id: str,
        tool: str,
        command: str,
        reason: str,
        token_fingerprint: str = "",
    ) -> None:
        self._audit.append(
            GateAuditRow(
                ts_ms=self._now_ms(),
                subtask_id=subtask_id,
                worker_id=worker_id,
                request_id=request_id,
                tool=tool,
                command=command,
                outcome=_DROPPED,
                reason=reason,
                exit_code=None,
                duration_ms=0,
                stdout_truncated_bytes=0,
                stderr_truncated_bytes=0,
                token_fingerprint=token_fingerprint,
            )
        )

    # ── steps 3-8 ────────────────────────────────────────────────────
    async def evaluate(
        self,
        *,
        request: ToolRequest,
        worker_id: str,
        token: CapabilityToken | None,
    ) -> GateDecision:
        """Run steps 3-8 of ADR 0003 §8 in order."""
        # We always audit at the end with a known fingerprint; if the token
        # itself is missing or malformed, the fingerprint is empty.
        fingerprint = token.fingerprint() if token is not None else ""

        def _denied(reason: str) -> GateDecision:
            self._record(request, worker_id, fingerprint, _DENIED, reason)
            return GateDecision(outcome=_DENIED, reason=reason)

        # Step 1.5 (issue body): only "shell" is routed in v1.
        if request.tool != "shell":
            return _denied("unsupported-tool")

        if token is None:
            return _denied("token-missing")

        # Step 3: token signature.
        try:
            self._verifier.verify(token, now_ms=self._now_ms())
        except TokenSignatureError:
            return _denied("token-signature")

        # Step 4: subtask binding + state.
        if token.scope.subtask_id != request.subtask_id:
            return _denied("subtask-mismatch")
        try:
            state = self._lifecycle.state_of(request.subtask_id)
        except KeyError:
            return _denied("subtask-not-running")
        if state is not SubtaskState.RUNNING:
            return _denied("subtask-not-running")

        # Step 5: expiry. Step 6: command-regex (fullmatch).
        try:
            decision = self._verifier.authorize(
                token, command=request.args.get("command", ""), now_ms=self._now_ms()
            )
        except ScopeViolationError as exc:
            msg = str(exc)
            if "expired" in msg:
                return _denied("expired")
            return _denied("command-not-allowed")

        # Step 7: workspace.
        workspace = self._workspace_root / token.scope.task_id / token.scope.subtask_id
        workspace.mkdir(parents=True, exist_ok=True)

        # Step 8: execute shell.
        command = request.args.get("command", "")
        started = self._now_ms()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=decision.timeout_s
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return self._record_allowed_or_timeout(
                    request,
                    worker_id,
                    fingerprint,
                    exit_code=None,
                    duration_ms=self._now_ms() - started,
                    stdout="",
                    stderr="",
                    timeout=True,
                )
        except OSError as exc:
            self._record(request, worker_id, fingerprint, _DENIED, f"exec-error: {exc}")
            return GateDecision(outcome=_DENIED, reason=f"exec-error: {exc}")

        return self._record_allowed_or_timeout(
            request,
            worker_id,
            fingerprint,
            exit_code=proc.returncode,
            duration_ms=self._now_ms() - started,
            stdout=_decode_truncated(stdout),
            stderr=_decode_truncated(stderr),
            timeout=False,
        )

    def _record_allowed_or_timeout(
        self,
        request: ToolRequest,
        worker_id: str,
        fingerprint: str,
        *,
        exit_code: int | None,
        duration_ms: int,
        stdout: str,
        stderr: str,
        timeout: bool,
    ) -> GateDecision:
        outcome = _DENIED if timeout else _ALLOWED
        reason = "timeout" if timeout else "ok"
        self._audit.append(
            GateAuditRow(
                ts_ms=self._now_ms(),
                subtask_id=request.subtask_id,
                worker_id=worker_id,
                request_id=request.request_id,
                tool=request.tool,
                command=request.args.get("command", ""),
                outcome=outcome,
                reason=reason,
                exit_code=exit_code,
                duration_ms=duration_ms,
                stdout_truncated_bytes=len(stdout.encode("utf-8")),
                stderr_truncated_bytes=len(stderr.encode("utf-8")),
                token_fingerprint=fingerprint,
            )
        )
        return GateDecision(
            outcome=outcome,
            reason=reason,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
        )

    def _record(
        self,
        request: ToolRequest,
        worker_id: str,
        fingerprint: str,
        outcome: str,
        reason: str,
    ) -> None:
        self._audit.append(
            GateAuditRow(
                ts_ms=self._now_ms(),
                subtask_id=request.subtask_id,
                worker_id=worker_id,
                request_id=request.request_id,
                tool=request.tool,
                command=request.args.get("command", ""),
                outcome=outcome,
                reason=reason,
                exit_code=None,
                duration_ms=0,
                stdout_truncated_bytes=0,
                stderr_truncated_bytes=0,
                token_fingerprint=fingerprint,
            )
        )


def _decode_truncated(data: bytes) -> str:
    truncated = data[:_MAX_OUTPUT_BYTES]
    return truncated.decode("utf-8", errors="replace")


__all__ = ["Gate", "GateDecision", "SubtaskStateLookup"]
