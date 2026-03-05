"""Tool call executor with safety gating and audit logging."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from turing.agent.safety import SafetyDecision
from turing.llm.base import ToolCall
from turing.tools.base import ToolResult

if TYPE_CHECKING:
    from turing.agent.safety import SafetyGate
    from turing.discord_bot.bot import TuringBot
    from turing.tools.base import ToolRegistry

logger = structlog.get_logger("turing.agent.executor")


class Executor:
    """Executes tool calls from LLM responses with safety checks."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        safety_gate: SafetyGate,
        bot: TuringBot | None = None,
    ) -> None:
        self.tool_registry = tool_registry
        self.safety_gate = safety_gate
        self.bot = bot

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
        3. If NEEDS_CONFIRMATION: send Discord confirmation view, wait for result
        4. If APPROVED: execute tool, audit log, return result
        """
        tool_name = tool_call.name
        arguments = tool_call.arguments

        logger.info(
            "executor.tool_call_start",
            tool=tool_name,
            user_id=user_id,
            arguments=arguments,
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
        if safety_result.decision == SafetyDecision.NEEDS_CONFIRMATION:
            confirmed = await self._request_confirmation(
                tool_name=tool_name,
                arguments=arguments,
                user_id=user_id,
                channel_id=channel_id,
                reason=safety_result.reason,
            )
            if not confirmed:
                logger.info(
                    "executor.tool_confirmation_denied",
                    tool=tool_name,
                )
                await self._log_action(
                    user_id=user_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    result="User denied confirmation",
                    risk_level=safety_result.risk_level,
                    approved=False,
                )
                return ToolResult(
                    success=False,
                    output="",
                    error="Action was not confirmed by user.",
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

    async def _request_confirmation(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
        channel_id: str,
        reason: str,
    ) -> bool:
        """Request confirmation from the user via Discord.

        If no bot is available or the channel cannot be found, the action is
        denied by default.
        """
        if self.bot is None:
            logger.warning("executor.no_bot_for_confirmation", tool=tool_name)
            return False

        try:
            # Build a description of the action
            args_summary = ", ".join(f"{k}={v!r}" for k, v in list(arguments.items())[:5])
            action_desc = f"{tool_name}({args_summary})"
            if len(action_desc) > 200:
                action_desc = action_desc[:197] + "..."

            from turing.discord_bot.views import ConfirmActionView

            view = ConfirmActionView(
                action_description=action_desc,
                authorized_user_id=int(user_id),
                timeout=60.0,
            )

            # Find the channel and send the confirmation
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                logger.warning(
                    "executor.channel_not_found",
                    channel_id=channel_id,
                )
                return False

            await channel.send(  # type: ignore[union-attr]
                f"**Confirmation required**: {reason}\n"
                f"Action: `{action_desc}`",
                view=view,
            )

            return await view.wait_for_result()

        except Exception as exc:
            logger.error(
                "executor.confirmation_error",
                error=str(exc),
                tool=tool_name,
            )
            return False

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
