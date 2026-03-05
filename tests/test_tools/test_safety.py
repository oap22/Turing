"""Tests for the SafetyGate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from turing.agent.safety import SafetyCheckResult, SafetyDecision, SafetyGate


@pytest.fixture()
def config():
    """Create a mock config."""
    cfg = MagicMock()
    cfg.discord_admin_ids = [12345, 67890]
    cfg.sandbox_enabled = False
    cfg.allowed_write_paths = ["/tmp"]
    return cfg


@pytest.fixture()
def audit_store():
    """Create a mock audit store."""
    store = MagicMock()
    store.log_audit = AsyncMock()
    return store


@pytest.fixture()
def safety_gate(config, audit_store):
    """Create a SafetyGate with mock dependencies."""
    return SafetyGate(config=config, audit_store=audit_store)


@pytest.fixture()
def safety_gate_no_audit(config):
    """Create a SafetyGate without an audit store."""
    return SafetyGate(config=config, audit_store=None)


class TestLowRiskTools:
    """Test that low-risk tool calls are auto-approved."""

    async def test_system_info_approved(self, safety_gate: SafetyGate):
        """Test that system_info tool is auto-approved."""
        result = await safety_gate.check("system_info", {"action": "cpu_usage"}, "user1")
        assert result.decision == SafetyDecision.APPROVED
        assert result.risk_level == "low"

    async def test_process_list_approved(self, safety_gate: SafetyGate):
        """Test that listing processes is auto-approved."""
        result = await safety_gate.check("process", {"action": "list_processes"}, "user1")
        assert result.decision == SafetyDecision.APPROVED
        assert result.risk_level == "low"

    async def test_process_info_approved(self, safety_gate: SafetyGate):
        """Test that getting process info is auto-approved."""
        result = await safety_gate.check("process", {"action": "get_process_info", "pid": 1}, "user1")
        assert result.decision == SafetyDecision.APPROVED
        assert result.risk_level == "low"


class TestDenylistCommands:
    """Test that denied commands are blocked."""

    async def test_rm_rf_root_denied(self, safety_gate: SafetyGate):
        """Test that rm -rf / is denied."""
        result = await safety_gate.check("shell", {"command": "rm -rf /"}, "user1")
        assert result.decision == SafetyDecision.DENIED

    async def test_mkfs_denied(self, safety_gate: SafetyGate):
        """Test that mkfs is denied."""
        result = await safety_gate.check("shell", {"command": "mkfs.ext4 /dev/sda1"}, "user1")
        assert result.decision == SafetyDecision.DENIED

    async def test_dd_denied(self, safety_gate: SafetyGate):
        """Test that dd to disk is denied."""
        result = await safety_gate.check("shell", {"command": "dd if=/dev/zero of=/dev/sda"}, "user1")
        assert result.decision == SafetyDecision.DENIED

    async def test_fork_bomb_denied(self, safety_gate: SafetyGate):
        """Test that fork bombs are denied."""
        result = await safety_gate.check("shell", {"command": ":(){ :|:& };:"}, "user1")
        assert result.decision == SafetyDecision.DENIED

    async def test_shutdown_denied(self, safety_gate: SafetyGate):
        """Test that shutdown commands are denied."""
        result = await safety_gate.check("shell", {"command": "shutdown -h now"}, "user1")
        assert result.decision == SafetyDecision.DENIED

    async def test_reboot_denied(self, safety_gate: SafetyGate):
        """Test that reboot is denied."""
        result = await safety_gate.check("shell", {"command": "reboot"}, "user1")
        assert result.decision == SafetyDecision.DENIED

    async def test_useradd_denied(self, safety_gate: SafetyGate):
        """Test that user management is denied."""
        result = await safety_gate.check("shell", {"command": "useradd hacker"}, "user1")
        assert result.decision == SafetyDecision.DENIED

    async def test_passwd_denied(self, safety_gate: SafetyGate):
        """Test that passwd is denied."""
        result = await safety_gate.check("shell", {"command": "passwd root"}, "user1")
        assert result.decision == SafetyDecision.DENIED

    async def test_chmod_777_root_denied(self, safety_gate: SafetyGate):
        """Test that chmod -R 777 / is denied."""
        result = await safety_gate.check("shell", {"command": "chmod -R 777 /"}, "user1")
        assert result.decision == SafetyDecision.DENIED


class TestHighRiskConfirmation:
    """Test that high-risk operations require confirmation from non-admins."""

    async def test_shell_high_risk_needs_confirmation(self, safety_gate: SafetyGate):
        """Test that a high-risk shell command needs confirmation for non-admin."""
        result = await safety_gate.check("shell", {"command": "apt install vim"}, "user1")
        assert result.decision == SafetyDecision.NEEDS_CONFIRMATION
        assert result.risk_level == "high"

    async def test_kill_process_needs_confirmation(self, safety_gate: SafetyGate):
        """Test that killing a process needs confirmation for non-admin."""
        result = await safety_gate.check(
            "process", {"action": "kill_process", "pid": 1234}, "user1"
        )
        assert result.decision == SafetyDecision.NEEDS_CONFIRMATION

    async def test_manage_service_needs_confirmation(self, safety_gate: SafetyGate):
        """Test that managing services needs confirmation for non-admin."""
        result = await safety_gate.check(
            "process",
            {"action": "manage_service", "service_name": "nginx", "service_action": "restart"},
            "user1",
        )
        assert result.decision == SafetyDecision.NEEDS_CONFIRMATION


class TestAdminOverride:
    """Test that admin users can bypass confirmation requirements."""

    async def test_admin_shell_approved(self, safety_gate: SafetyGate):
        """Test that admins can execute high-risk shell commands."""
        # 12345 is in the discord_admin_ids list.
        result = await safety_gate.check("shell", {"command": "apt install vim"}, "12345")
        assert result.decision == SafetyDecision.APPROVED

    async def test_admin_kill_process_approved(self, safety_gate: SafetyGate):
        """Test that admins can kill processes without confirmation."""
        result = await safety_gate.check(
            "process", {"action": "kill_process", "pid": 1234}, "12345"
        )
        assert result.decision == SafetyDecision.APPROVED

    async def test_admin_string_id_match(self, safety_gate: SafetyGate):
        """Test that admin IDs are compared as strings."""
        result = await safety_gate.check("shell", {"command": "apt install vim"}, "67890")
        assert result.decision == SafetyDecision.APPROVED

    async def test_admin_still_denied_for_denylist(self, safety_gate: SafetyGate):
        """Test that even admins cannot run deny-listed commands."""
        result = await safety_gate.check("shell", {"command": "rm -rf /"}, "12345")
        assert result.decision == SafetyDecision.DENIED


class TestMediumRiskTools:
    """Test that medium-risk tools are auto-approved."""

    async def test_network_tool_approved(self, safety_gate: SafetyGate):
        """Test that network tool is auto-approved."""
        result = await safety_gate.check(
            "network", {"action": "ping", "host": "google.com"}, "user1"
        )
        assert result.decision == SafetyDecision.APPROVED
        assert result.risk_level == "medium"

    async def test_filesystem_write_approved(self, safety_gate: SafetyGate):
        """Test that filesystem write is auto-approved (path check is in the tool)."""
        result = await safety_gate.check(
            "filesystem", {"action": "write_file", "path": "/tmp/test.txt"}, "user1"
        )
        assert result.decision == SafetyDecision.APPROVED
        assert result.risk_level == "medium"

    async def test_safe_shell_command(self, safety_gate: SafetyGate):
        """Test that safe shell commands are auto-approved."""
        result = await safety_gate.check("shell", {"command": "echo hello"}, "user1")
        assert result.decision == SafetyDecision.APPROVED
        assert result.risk_level == "medium"

    async def test_ls_command(self, safety_gate: SafetyGate):
        """Test that ls is auto-approved."""
        result = await safety_gate.check("shell", {"command": "ls -la /tmp"}, "user1")
        assert result.decision == SafetyDecision.APPROVED
        assert result.risk_level == "medium"


class TestAuditLogging:
    """Test audit logging functionality."""

    async def test_log_action_with_store(self, safety_gate: SafetyGate, audit_store):
        """Test that actions are logged to the audit store."""
        await safety_gate.log_action(
            user_id="user1",
            tool_name="shell",
            arguments={"command": "echo test"},
            result="test",
            risk_level="low",
            approved=True,
        )
        audit_store.log_audit.assert_called_once()
        call_kwargs = audit_store.log_audit.call_args[1]
        assert call_kwargs["user_id"] == "user1"
        assert call_kwargs["tool_name"] == "shell"
        assert call_kwargs["approved"] is True

    async def test_log_action_without_store(self, safety_gate_no_audit: SafetyGate):
        """Test that logging works without an audit store (no crash)."""
        await safety_gate_no_audit.log_action(
            user_id="user1",
            tool_name="shell",
            arguments={"command": "echo test"},
            result="test",
            risk_level="low",
            approved=True,
        )
        # Should not raise any exception.

    async def test_log_action_store_error(self, safety_gate: SafetyGate, audit_store):
        """Test that audit store errors are handled gracefully."""
        audit_store.log_audit = AsyncMock(side_effect=RuntimeError("DB error"))
        # Should not raise.
        await safety_gate.log_action(
            user_id="user1",
            tool_name="shell",
            arguments={"command": "echo test"},
            result="test",
            risk_level="low",
            approved=True,
        )


class TestDenyPatterns:
    """Test individual deny patterns via _check_denylist."""

    def test_check_denylist_clean(self, safety_gate: SafetyGate):
        """Test that safe commands pass the deny list."""
        denied, reason = safety_gate._check_denylist("echo hello")
        assert denied is False
        assert reason == ""

    def test_check_denylist_rm_rf(self, safety_gate: SafetyGate):
        """Test that rm -rf / is caught."""
        denied, reason = safety_gate._check_denylist("rm -rf /")
        assert denied is True
        assert "blocked" in reason.lower()

    def test_check_denylist_poweroff(self, safety_gate: SafetyGate):
        """Test that poweroff is caught."""
        denied, reason = safety_gate._check_denylist("poweroff")
        assert denied is True

    def test_check_denylist_halt(self, safety_gate: SafetyGate):
        """Test that halt is caught."""
        denied, reason = safety_gate._check_denylist("halt")
        assert denied is True
