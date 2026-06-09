"""Tool call executor with safety gating and audit logging."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from turing.agent.safety import SafetyDecision, _redact_arguments
from turing.tools.base import ToolResult

if TYPE_CHECKING:
    from turing.agent.safety import SafetyGate
    from turing.llm.base import ToolCall
    from turing.tools.base import ToolRegistry

logger = structlog.get_logger("turing.agent.executor")


class Executor:
    """Executes tool calls from LLM responses with safety checks."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        safety_gate: SafetyGate,
    ) -> None:
        self.tool_registry = tool_registry
        self.safety_gate = safety_gate

    async def execute_tool_call(
        self,
        tool_call: ToolCall,
        user_id: str,
        channel_id: str = "",
    ) -> ToolResult:
        """Execute a single tool call with safety check.

        Flow:
        1. Safety gate check
        2. If DENIED: return error ToolResult
        3. If NEEDS_CONFIRMATION: deny by default (fail-safe). The interactive
           confirmation surface moved off Discord with ADR 0010; the webui chat
           pane will reintroduce an approval affordance in a follow-on slice.
        4. If APPROVED: execute tool, audit log, return result
        """
        tool_name = tool_call.name
        arguments = tool_call.arguments

        # Redact before this pre-audit log line: raw arguments can carry
        # tokens or passwords, and SafetyGate's own redaction only covers the
        # audit path, not this structlog event (#331).
        logger.info(
            "executor.tool_call_start",
            tool=tool_name,
            user_id=user_id,
            arguments=_redact_arguments(arguments),
        )

        # Step 1: Safety gate check
        safety_result = await self.safety_gate.check(
            tool_name=tool_name,
            arguments=arguments,
            user_id=user_id,
        )

        # Step 2: Handle DENIED
        if safety_result.decision == SafetyDecision.DENIED:
            logger.warning(
                "executor.tool_denied",
                tool=tool_name,
                reason=safety_result.reason,
            )
            await self._log_action(
                user_id=user_id,
                tool_name=tool_name,
                arguments=arguments,
                result=f"DENIED: {safety_result.reason}",
                risk_level=safety_result.risk_level,
                approved=False,
            )
            return ToolResult(
                success=False,
                output="",
                error=f"Safety check denied: {safety_result.reason}",
            )

        # Step 3: Handle NEEDS_CONFIRMATION
        # The Discord button confirmation flow was retired with ADR 0010. Until
        # the webui chat pane reintroduces an approval affordance, an action
        # requiring confirmation is denied by default — the same fail-safe the
        # old path took when no surface could solicit a decision.
        if safety_result.decision == SafetyDecision.NEEDS_CONFIRMATION:
            logger.info(
                "executor.tool_confirmation_unavailable",
                tool=tool_name,
                reason=safety_result.reason,
            )
            await self._log_action(
                user_id=user_id,
                tool_name=tool_name,
                arguments=arguments,
                result="Confirmation surface unavailable; denied by default",
                risk_level=safety_result.risk_level,
                approved=False,
            )
            return ToolResult(
                success=False,
                output="",
                error="Action requires confirmation but no operator surface is available.",
            )

        # Step 4: Execute the tool
        start_time = time.monotonic()
        try:
            result = await self.tool_registry.execute(tool_name, **arguments)
        except Exception as exc:
            logger.error(
                "executor.tool_execution_error",
                tool=tool_name,
                error=str(exc),
            )
            result = ToolResult(
                success=False,
                output="",
                error=f"Execution failed: {exc}",
            )

        duration_ms = int((time.monotonic() - start_time) * 1000)
        assert isinstance(result, ToolResult)

        # Audit log
        result_text = result.output if result.success else f"Error: {result.error}"
        await self._log_action(
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result_text[:500],
            risk_level=safety_result.risk_level,
            approved=True,
        )

        logger.info(
            "executor.tool_call_complete",
            tool=tool_name,
            success=result.success,
            duration_ms=duration_ms,
        )

        return result

    async def execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
        user_id: str,
        channel_id: str = "",
    ) -> list[tuple[ToolCall, ToolResult]]:
        """Execute multiple tool calls sequentially."""
        results: list[tuple[ToolCall, ToolResult]] = []
        for tc in tool_calls:
            result = await self.execute_tool_call(tc, user_id, channel_id)
            results.append((tc, result))
        return results

    async def _log_action(
        self,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        risk_level: str,
        approved: bool,
    ) -> None:
        """Log a tool action via the safety gate's audit mechanism."""
        try:
            await self.safety_gate.log_action(
                user_id=user_id,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                risk_level=risk_level,
                approved=approved,
            )
        except Exception as exc:
            logger.error("executor.audit_log_error", error=str(exc))
