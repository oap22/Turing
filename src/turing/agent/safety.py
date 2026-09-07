"""Safety gate for evaluating tool calls before execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

from turing.telemetry import traced
from turing.telemetry.redactor import redact
from turing.tools.command_safety import check_denylist, classify_command_risk


def _redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of tool arguments with secrets scrubbed (issue #240).

    Tool arguments can carry tokens or passwords; routing every string value
    through the telemetry redactor keeps them out of both the structlog
    stream and the persisted SQLite audit trail.
    """

    def _scrub(value: Any) -> Any:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {k: _scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_scrub(v) for v in value]
        return value

    return {key: _scrub(val) for key, val in arguments.items()}


def _safety_event_payload(kind, args, kwargs, result, exc):  # type: ignore[no-untyped-def]
    """Tag every safety event with ``priority="high"`` and the rule that fired.

    Safety decisions are the most diagnostically valuable events the fleet
    produces — they get priority routing (slice 8 promotes them to
    TCP-WHISPER; until then they ride the same SHOUT but as
    ``MessageType.TELEMETRY_PRIORITY``).
    """
    tool = args[1] if len(args) > 1 else kwargs.get("tool_name", "")
    data: dict[str, Any] = {"priority": "high", "tool": tool}
    if kind == "end" and isinstance(result, SafetyCheckResult):
        data["decision"] = result.decision.value
        data["risk_level"] = result.risk_level
        data["rule"] = result.reason
    return data


class SafetyDecision(StrEnum):
    """Outcome of a safety evaluation."""

    APPROVED = "approved"
    DENIED = "denied"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass
class SafetyCheckResult:
    """Result of a safety check on a tool call."""

    decision: SafetyDecision
    reason: str
    risk_level: str = "low"


class SafetyGate:
    """Evaluates tool calls for safety before execution.

    Applies deny-list pattern matching for shell commands, risk-level
    assessments, and admin privilege checks to determine whether a tool
    call should be approved, denied, or flagged for confirmation.

    The deny-list and shell risk classification both live in the shared
    :mod:`turing.tools.command_safety` module so the shell tool and this
    gate can never drift out of sync (issue #240).
    """

    def __init__(self, config: Any, audit_store: Any | None = None) -> None:
        self.config = config
        self.audit_store = audit_store
        self.logger = structlog.get_logger("turing.safety")

    @traced("safety.check", payload=_safety_event_payload)
    async def check(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
    ) -> SafetyCheckResult:
        """Check if a tool call is safe to execute.

        Steps:
            1. If the tool is ``shell``, check the command against the deny list.
            2. Determine the effective risk level of the operation.
            3. For HIGH-risk operations, require admin privileges or confirmation.
            4. Return the appropriate decision.
        """
        # Determine base risk level from the tool.
        risk_level = self._get_tool_risk(tool_name, arguments)

        # Step 1: Shell deny-list.
        if tool_name == "shell":
            command = arguments.get("command", "")
            is_denied, reason = self._check_denylist(command)
            if is_denied:
                self.logger.warning(
                    "safety_denied",
                    tool=tool_name,
                    user=user_id,
                    reason=reason,
                )
                return SafetyCheckResult(
                    decision=SafetyDecision.DENIED,
                    reason=reason,
                    risk_level="high",
                )

        # Step 2: Process tool deny for kill/manage actions. The operator
        # auto-approve flag applies here as well as in step 4, so it is one
        # switch and not a half-switch that still stalls process management.
        if tool_name == "process":
            action = arguments.get("action", "")
            if (
                action in ("kill_process", "manage_service")
                and not self._is_admin(user_id)
                and not self._auto_approve_high_risk(user_id)
            ):
                return SafetyCheckResult(
                    decision=SafetyDecision.NEEDS_CONFIRMATION,
                    reason=f"Action '{action}' requires admin confirmation",
                    risk_level="high",
                )

        # Step 3: Filesystem write path validation.
        if tool_name == "filesystem" and arguments.get("action") == "write_file":
            risk_level = "medium"

        # Step 4: Risk-based decision.
        if risk_level == "high":
            if self._is_admin(user_id):
                return SafetyCheckResult(
                    decision=SafetyDecision.APPROVED,
                    reason="Approved: user is admin",
                    risk_level=risk_level,
                )
            if self._auto_approve_high_risk(user_id):
                return SafetyCheckResult(
                    decision=SafetyDecision.APPROVED,
                    reason="Approved: safety_auto_approve_high_risk operator authority",
                    risk_level=risk_level,
                )
            return SafetyCheckResult(
                decision=SafetyDecision.NEEDS_CONFIRMATION,
                reason=f"High-risk operation '{tool_name}' requires confirmation",
                risk_level=risk_level,
            )

        # Low and medium risk: auto-approve.
        return SafetyCheckResult(
            decision=SafetyDecision.APPROVED,
            reason="Auto-approved: acceptable risk level",
            risk_level=risk_level,
        )

    def _check_denylist(self, command: str) -> tuple[bool, str]:
        """Check if command matches any shared deny pattern.

        Returns:
            A tuple ``(is_denied, reason)``.  ``is_denied`` is ``True``
            when the command matches a blocked pattern.
        """
        return check_denylist(command)

    def _get_tool_risk(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Determine the effective risk level based on tool and action."""
        # Tools with action-dependent risk.
        medium_risk_tools = {"filesystem", "network", "agent_mailbox"}
        low_risk_tools = {"system_info"}

        if tool_name in low_risk_tools:
            return "low"

        if tool_name == "shell":
            # Parse-based classification — a command is only MEDIUM when
            # every pipeline segment is a known read-only builtin. A shell
            # metacharacter can no longer downgrade a destructive command
            # via a "safe" prefix (issue #237).
            command = arguments.get("command", "")
            return classify_command_risk(command)

        if tool_name == "process":
            action = arguments.get("action", "")
            if action in ("list_processes", "get_process_info"):
                return "low"
            return "high"

        if tool_name in medium_risk_tools:
            return "medium"

        # Default: treat unknown tools as high risk.
        return "high"

    def _auto_approve_high_risk(self, user_id: str) -> bool:
        """Authorize the convenience flag only for an explicit operator.

        The flag is not an authentication mechanism.  It is useful on a
        single-operator node only when the caller is either in the configured
        admin list or matches the separately configured operator identity.
        Reached only *after* the deny-list and the admin check, so a denied
        command stays denied whatever this flag says.
        """
        # `is True`, not truthiness: tests hand this gate a MagicMock config,
        # and an auto-created attribute must read as "off", never "on".
        if getattr(self.config, "safety_auto_approve_high_risk", False) is not True:
            return False
        if self._is_admin(user_id):
            return True
        operator_id = getattr(self.config, "safety_single_operator_user_id", None)
        return isinstance(operator_id, str) and bool(operator_id) and user_id == operator_id

    def _is_admin(self, user_id: str) -> bool:
        """Check whether the user is an admin.

        Admins come from the surface-agnostic ``admin_user_ids`` config list
        (ADR 0010 retired the Discord-specific admin list). Entries may be
        ``int`` or ``str``; comparison is done as strings.
        """
        admin_ids = getattr(self.config, "admin_user_ids", [])
        return str(user_id) in [str(aid) for aid in admin_ids]

    async def log_action(
        self,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        risk_level: str,
        approved: bool,
    ) -> None:
        """Log a tool action to the audit trail.

        If an audit store is configured, the action is persisted for later
        review.  Otherwise, the action is logged via structlog only.
        """
        timestamp = datetime.now(tz=UTC).isoformat()
        # Tool arguments can carry tokens or passwords — scrub them before
        # they reach the structlog stream or the persisted audit store
        # (issue #240, finding 6).
        redacted_arguments = _redact_arguments(arguments)

        self.logger.info(
            "audit_log",
            timestamp=timestamp,
            user_id=user_id,
            tool_name=tool_name,
            arguments=redacted_arguments,
            result_preview=result[:200] if result else "",
            risk_level=risk_level,
            approved=approved,
        )

        if self.audit_store is not None:
            try:
                # MemoryStore.log_audit owns the persisted timestamp via
                # _utcnow(); the structlog line above carries the same wall
                # time for human inspection.
                await self.audit_store.log_audit(
                    user_id=user_id,
                    action="tool_call",
                    tool_name=tool_name,
                    arguments=redacted_arguments,
                    result=result,
                    risk_level=risk_level,
                    approved=approved,
                )
            except Exception as exc:
                self.logger.error("audit_store_error", error=str(exc))
