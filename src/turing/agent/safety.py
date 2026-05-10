"""Safety gate for evaluating tool calls before execution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

import structlog

from turing.telemetry import traced


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
    """

    DENY_PATTERNS: ClassVar[list[str]] = [
        r"rm\s+-rf\s+/(?!\w)",  # rm -rf /
        r"mkfs\.",  # Format filesystems
        r"dd\s+.*of=/dev/",  # Raw disk writes
        r":\(\)\{.*\|.*&",  # Fork bombs
        r"chmod\s+-R\s+777\s+/",  # World-writable root
        r">\s*/dev/sd",  # Overwrite disks
        r"shutdown|reboot|halt|poweroff",  # System power
        r"userdel|useradd|passwd",  # User management
    ]

    def __init__(self, config: Any, audit_store: Any | None = None) -> None:
        self.config = config
        self.audit_store = audit_store
        self.logger = structlog.get_logger("turing.safety")
        self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.DENY_PATTERNS]

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

        # Step 2: Process tool deny for kill/manage actions.
        if tool_name == "process":
            action = arguments.get("action", "")
            if action in ("kill_process", "manage_service") and not self._is_admin(user_id):
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
        """Check if command matches any deny pattern.

        Returns:
            A tuple ``(is_denied, reason)``.  ``is_denied`` is ``True``
            when the command matches a blocked pattern.
        """
        for pattern in self._compiled_patterns:
            if pattern.search(command):
                return True, f"Command blocked by safety rule: {pattern.pattern}"
        return False, ""

    def _get_tool_risk(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Determine the effective risk level based on tool and action."""
        # Tools with action-dependent risk.
        medium_risk_tools = {"filesystem", "network"}
        low_risk_tools = {"system_info"}

        if tool_name in low_risk_tools:
            return "low"

        if tool_name == "shell":
            # Check if command matches safe prefixes.
            command = arguments.get("command", "").strip()
            safe_prefixes = (
                "echo",
                "cat",
                "ls",
                "pwd",
                "whoami",
                "date",
                "uptime",
                "hostname",
                "uname",
                "df",
                "free",
                "head",
                "tail",
                "wc",
                "grep",
                "find",
                "which",
                "id",
                "ps",
            )
            for prefix in safe_prefixes:
                if command.startswith(prefix):
                    return "medium"
            return "high"

        if tool_name == "process":
            action = arguments.get("action", "")
            if action in ("list_processes", "get_process_info"):
                return "low"
            return "high"

        if tool_name in medium_risk_tools:
            return "medium"

        # Default: treat unknown tools as high risk.
        return "high"

    def _is_admin(self, user_id: str) -> bool:
        """Check whether the user is an admin."""
        admin_ids = getattr(self.config, "discord_admin_ids", [])
        # discord_admin_ids may be list[int] or list[str]; compare as strings.
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

        self.logger.info(
            "audit_log",
            timestamp=timestamp,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
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
                    arguments=arguments,
                    result=result,
                    risk_level=risk_level,
                    approved=approved,
                )
            except Exception as exc:
                self.logger.error("audit_store_error", error=str(exc))
