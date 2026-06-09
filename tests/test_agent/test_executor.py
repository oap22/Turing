"""Tests for the tool call Executor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import structlog.testing

from turing.agent.executor import Executor
from turing.agent.safety import SafetyCheckResult, SafetyDecision
from turing.llm.base import ToolCall
from turing.tools.base import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_tool_registry() -> MagicMock:
    registry = MagicMock()
    registry.execute = AsyncMock(return_value=ToolResult(success=True, output="command output"))
    return registry


def _make_mock_safety_gate(
    decision: SafetyDecision = SafetyDecision.APPROVED,
    reason: str = "Auto-approved",
    risk_level: str = "low",
) -> AsyncMock:
    gate = AsyncMock()
    gate.check.return_value = SafetyCheckResult(
        decision=decision,
        reason=reason,
        risk_level=risk_level,
    )
    gate.log_action = AsyncMock()
    return gate


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_successful_execution():
    """Tool call that passes safety should execute and return result."""
    registry = _make_mock_tool_registry()
    gate = _make_mock_safety_gate()
    executor = Executor(registry, gate)

    tool_call = ToolCall(id="tc-1", name="system_info", arguments={})
    result = await executor.execute_tool_call(tool_call, user_id="user-1")

    assert result.success is True
    assert result.output == "command output"
    gate.check.assert_awaited_once_with(tool_name="system_info", arguments={}, user_id="user-1")
    registry.execute.assert_awaited_once_with("system_info")


async def test_safety_denied_tool():
    """Tool call denied by safety gate should return error without executing."""
    registry = _make_mock_tool_registry()
    gate = _make_mock_safety_gate(
        decision=SafetyDecision.DENIED,
        reason="Dangerous command blocked",
        risk_level="high",
    )
    executor = Executor(registry, gate)

    tool_call = ToolCall(id="tc-denied", name="shell", arguments={"command": "rm -rf /"})
    result = await executor.execute_tool_call(tool_call, user_id="user-1")

    assert result.success is False
    assert "denied" in result.error.lower()
    # The tool should NOT have been executed
    registry.execute.assert_not_awaited()


async def test_needs_confirmation_denied_no_surface():
    """A NEEDS_CONFIRMATION tool is denied by default after ADR 0010.

    The interactive Discord confirmation surface was retired; until the webui
    chat pane reintroduces an approval affordance, the executor fails safe by
    denying any action that requires confirmation.
    """
    registry = _make_mock_tool_registry()
    gate = _make_mock_safety_gate(
        decision=SafetyDecision.NEEDS_CONFIRMATION,
        reason="High-risk operation requires confirmation",
        risk_level="high",
    )
    executor = Executor(registry, gate)

    tool_call = ToolCall(
        id="tc-confirm", name="process", arguments={"action": "kill_process", "pid": 1234}
    )
    result = await executor.execute_tool_call(tool_call, user_id="user-1", channel_id="ch-1")

    assert result.success is False
    assert "no operator surface" in result.error.lower()
    registry.execute.assert_not_awaited()


async def test_execute_multiple_tool_calls():
    """Multiple tool calls should be executed sequentially."""
    registry = _make_mock_tool_registry()
    gate = _make_mock_safety_gate()
    executor = Executor(registry, gate)

    tool_calls = [
        ToolCall(id="tc-1", name="shell", arguments={"command": "ls"}),
        ToolCall(id="tc-2", name="system_info", arguments={}),
    ]

    results = await executor.execute_tool_calls(tool_calls, user_id="user-1")

    assert len(results) == 2
    assert all(r.success for _, r in results)
    assert registry.execute.await_count == 2


async def test_tool_execution_error():
    """If tool execution raises, executor should return an error ToolResult."""
    registry = _make_mock_tool_registry()
    registry.execute = AsyncMock(side_effect=RuntimeError("Tool crashed"))
    gate = _make_mock_safety_gate()
    executor = Executor(registry, gate)

    tool_call = ToolCall(id="tc-error", name="shell", arguments={"command": "bad"})
    result = await executor.execute_tool_call(tool_call, user_id="user-1")

    assert result.success is False
    assert "crashed" in result.error.lower()


async def test_audit_logging():
    """Successful execution should log the action via the safety gate."""
    registry = _make_mock_tool_registry()
    gate = _make_mock_safety_gate()
    executor = Executor(registry, gate)

    tool_call = ToolCall(id="tc-audit", name="system_info", arguments={})
    await executor.execute_tool_call(tool_call, user_id="user-1")

    gate.log_action.assert_awaited_once()
    call_kwargs = gate.log_action.await_args.kwargs
    assert call_kwargs["tool_name"] == "system_info"
    assert call_kwargs["user_id"] == "user-1"
    assert call_kwargs["approved"] is True


async def test_start_log_redacts_secrets_in_arguments():
    """Regression for #331: the pre-audit ``tool_call_start`` log must not
    emit raw secrets — argument values pass through the redactor before they
    reach structlog, mirroring the audit-path redaction in SafetyGate."""
    registry = _make_mock_tool_registry()
    gate = _make_mock_safety_gate()
    executor = Executor(registry, gate)

    secret = "Bearer sk-ant-api03-abcdefghijklmnopqrstuvwx"
    tool_call = ToolCall(
        id="tc-secret",
        name="network",
        arguments={"action": "http_request", "body": f"Authorization: {secret}"},
    )

    with structlog.testing.capture_logs() as logs:
        await executor.execute_tool_call(tool_call, user_id="user-1")

    start_events = [e for e in logs if e["event"] == "executor.tool_call_start"]
    assert start_events, "tool_call_start event was not logged"
    logged_args = str(start_events[0]["arguments"])
    assert "sk-ant-api03" not in logged_args
    assert "[REDACTED" in logged_args
    # The raw arguments object handed to the tool itself is untouched.
    registry.execute.assert_awaited_once_with(
        "network", action="http_request", body=f"Authorization: {secret}"
    )
