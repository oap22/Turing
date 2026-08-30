"""Tests for the ProcessTool.

The process tool is the agent's only HIGH-risk system-control surface
(``kill_process`` / ``manage_service``), so every action path — including the
failure and "access denied" branches — is exercised here. All ``psutil`` and
subprocess calls are mocked so the suite never touches real processes or the
host's service manager.
"""

from __future__ import annotations

import signal
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest

from turing.tools.base import RiskLevel
from turing.tools.process import ProcessTool, _format_bytes, _format_timestamp


@pytest.fixture()
def process_tool() -> ProcessTool:
    return ProcessTool()


def _fake_process(
    *,
    pid: int = 4242,
    name: str = "python",
    status: str = "running",
) -> MagicMock:
    """Build a MagicMock standing in for a ``psutil.Process``.

    ``oneshot()`` is configured as a context manager and every accessor used by
    ``_get_process_info`` returns a concrete value so formatting never blows up.
    """
    proc = MagicMock()
    proc.pid = pid
    proc.name.return_value = name
    proc.status.return_value = status
    proc.ppid.return_value = 1
    proc.username.return_value = "pi"
    proc.cpu_percent.return_value = 12.5
    proc.memory_percent.return_value = 3.25
    proc.memory_info.return_value = MagicMock(rss=1024 * 1024, vms=2 * 1024 * 1024)
    proc.num_threads.return_value = 7
    proc.create_time.return_value = 1_700_000_000.0
    proc.cmdline.return_value = ["python", "-m", "turing"]
    proc.cwd.return_value = "/home/pi/turing"
    # oneshot() is used as a context manager; MagicMock supports the protocol.
    proc.oneshot.return_value = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# Metadata / contract
# ---------------------------------------------------------------------------


class TestContract:
    def test_identity(self, process_tool: ProcessTool):
        assert process_tool.name == "process"
        assert "list_processes" in process_tool.description

    def test_tool_is_high_risk_and_requires_confirmation(self, process_tool: ProcessTool):
        assert process_tool.risk_level is RiskLevel.HIGH
        assert process_tool.requires_confirmation is True

    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("list_processes", RiskLevel.LOW),
            ("get_process_info", RiskLevel.LOW),
            ("kill_process", RiskLevel.HIGH),
            ("manage_service", RiskLevel.HIGH),
            ("anything_else", RiskLevel.HIGH),
        ],
    )
    def test_per_action_risk(self, process_tool: ProcessTool, action: str, expected: RiskLevel):
        assert process_tool.get_action_risk(action) is expected

    def test_parameters_schema_lists_all_actions(self, process_tool: ProcessTool):
        enum = process_tool.parameters["properties"]["action"]["enum"]
        assert set(enum) == {
            "list_processes",
            "get_process_info",
            "kill_process",
            "manage_service",
        }
        assert process_tool.parameters["required"] == ["action"]


# ---------------------------------------------------------------------------
# Dispatch-level error handling
# ---------------------------------------------------------------------------


class TestDispatch:
    async def test_missing_action(self, process_tool: ProcessTool):
        result = await process_tool.execute()
        assert result.success is False
        assert "No action specified" in result.error

    async def test_unknown_action(self, process_tool: ProcessTool):
        result = await process_tool.execute(action="frobnicate")
        assert result.success is False
        assert "Unknown action 'frobnicate'" in result.error

    async def test_no_such_process_is_mapped(self, process_tool: ProcessTool):
        with patch("turing.tools.process.psutil.Process", side_effect=psutil.NoSuchProcess(1)):
            result = await process_tool.execute(action="get_process_info", pid=1)
        assert result.success is False
        assert "Process not found" in result.error

    async def test_access_denied_is_mapped(self, process_tool: ProcessTool):
        with patch("turing.tools.process.psutil.Process", side_effect=psutil.AccessDenied(1)):
            result = await process_tool.execute(action="kill_process", pid=1)
        assert result.success is False
        assert "Access denied" in result.error

    async def test_generic_exception_is_caught(self, process_tool: ProcessTool):
        with patch("turing.tools.process.psutil.Process", side_effect=RuntimeError("boom")):
            result = await process_tool.execute(action="get_process_info", pid=1)
        assert result.success is False
        assert "boom" in result.error


# ---------------------------------------------------------------------------
# list_processes
# ---------------------------------------------------------------------------


class TestListProcesses:
    async def test_sorted_by_cpu_descending_and_capped(self, process_tool: ProcessTool):
        # 60 processes; output should be capped at the top 50 by CPU.
        fake_infos = [
            {
                "pid": i,
                "name": f"proc{i}",
                "cpu_percent": float(i),
                "memory_percent": 1.0,
                "status": "sleeping",
            }
            for i in range(60)
        ]
        procs = []
        for info in fake_infos:
            p = MagicMock()
            p.info = info
            procs.append(p)

        with patch("turing.tools.process.psutil.process_iter", return_value=procs):
            result = await process_tool.execute(action="list_processes")

        assert result.success is True
        body = result.output.splitlines()[2:]  # drop header rows
        assert len(body) == 50
        # Highest CPU (proc59) first, lowest shown (proc10) last.
        assert "proc59" in body[0]
        assert "proc10" in body[-1]

    async def test_skips_processes_that_vanish_mid_iteration(self, process_tool: ProcessTool):
        good = MagicMock()
        good.info = {
            "pid": 1,
            "name": "init",
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "status": "sleeping",
        }
        gone = MagicMock()
        type(gone).info = property(lambda self: (_ for _ in ()).throw(psutil.NoSuchProcess(2)))

        with patch("turing.tools.process.psutil.process_iter", return_value=[good, gone]):
            result = await process_tool.execute(action="list_processes")

        assert result.success is True
        assert "init" in result.output

    async def test_handles_none_cpu_percent(self, process_tool: ProcessTool):
        p = MagicMock()
        p.info = {
            "pid": 1,
            "name": "init",
            "cpu_percent": None,
            "memory_percent": None,
            "status": "sleeping",
        }
        with patch("turing.tools.process.psutil.process_iter", return_value=[p]):
            result = await process_tool.execute(action="list_processes")
        assert result.success is True
        assert "init" in result.output


# ---------------------------------------------------------------------------
# get_process_info
# ---------------------------------------------------------------------------


class TestGetProcessInfo:
    async def test_missing_pid(self, process_tool: ProcessTool):
        result = await process_tool.execute(action="get_process_info")
        assert result.success is False
        assert "No PID provided" in result.error

    async def test_full_info(self, process_tool: ProcessTool):
        proc = _fake_process(pid=4242, name="python")
        with patch("turing.tools.process.psutil.Process", return_value=proc):
            result = await process_tool.execute(action="get_process_info", pid=4242)
        assert result.success is True
        assert "PID 4242" in result.output
        assert "python" in result.output
        assert "python -m turing" in result.output
        assert "/home/pi/turing" in result.output

    async def test_cmdline_access_denied_is_graceful(self, process_tool: ProcessTool):
        proc = _fake_process()
        proc.cmdline.side_effect = psutil.AccessDenied(1)
        proc.cwd.side_effect = psutil.ZombieProcess(1)
        with patch("turing.tools.process.psutil.Process", return_value=proc):
            result = await process_tool.execute(action="get_process_info", pid=1)
        assert result.success is True
        assert result.output.count("[access denied]") == 2


# ---------------------------------------------------------------------------
# kill_process
# ---------------------------------------------------------------------------


class TestKillProcess:
    async def test_missing_pid(self, process_tool: ProcessTool):
        result = await process_tool.execute(action="kill_process")
        assert result.success is False
        assert "No PID provided" in result.error

    async def test_default_signal_is_sigterm(self, process_tool: ProcessTool):
        proc = _fake_process(pid=99, name="victim")
        with patch("turing.tools.process.psutil.Process", return_value=proc):
            result = await process_tool.execute(action="kill_process", pid=99)
        assert result.success is True
        assert "Sent SIGTERM to process 99 (victim)" in result.output
        proc.send_signal.assert_called_once_with(signal.SIGTERM)

    async def test_explicit_signal(self, process_tool: ProcessTool):
        proc = _fake_process(pid=99)
        with patch("turing.tools.process.psutil.Process", return_value=proc):
            result = await process_tool.execute(
                action="kill_process", pid=99, signal_name="SIGKILL"
            )
        assert result.success is True
        proc.send_signal.assert_called_once_with(signal.SIGKILL)

    async def test_unknown_signal_name_falls_back_to_sigterm(self, process_tool: ProcessTool):
        proc = _fake_process(pid=99)
        with patch("turing.tools.process.psutil.Process", return_value=proc):
            await process_tool.execute(action="kill_process", pid=99, signal_name="SIGBOGUS")
        proc.send_signal.assert_called_once_with(signal.SIGTERM)


# ---------------------------------------------------------------------------
# manage_service
# ---------------------------------------------------------------------------


def _patch_subprocess(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    """Patch ``asyncio.create_subprocess_exec`` and capture the argv issued."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return patch(
        "turing.tools.process.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    )


class TestManageService:
    async def test_missing_service_name(self, process_tool: ProcessTool):
        result = await process_tool.execute(action="manage_service", service_action="start")
        assert result.success is False
        assert "No service name provided" in result.error

    async def test_missing_service_action(self, process_tool: ProcessTool):
        result = await process_tool.execute(action="manage_service", service_name="nginx")
        assert result.success is False
        assert "No service action provided" in result.error

    async def test_invalid_service_action(self, process_tool: ProcessTool):
        result = await process_tool.execute(
            action="manage_service", service_name="nginx", service_action="nuke"
        )
        assert result.success is False
        assert "Invalid service action" in result.error

    async def test_successful_start(self, process_tool: ProcessTool):
        with _patch_subprocess(0, stdout=b"") as p:
            result = await process_tool.execute(
                action="manage_service", service_name="nginx", service_action="start"
            )
        assert result.success is True
        assert "started successfully" in result.output
        # No shell: argv tokens are passed separately so the unit name can never
        # be interpreted as part of a command. ``patch(new=...)`` yields the mock
        # itself, so ``p`` is the AsyncMock for create_subprocess_exec.
        p.assert_awaited_once()
        assert p.await_args.args == ("systemctl", "start", "nginx")

    async def test_status_does_not_get_success_banner(self, process_tool: ProcessTool):
        with _patch_subprocess(0, stdout=b"active (running)"):
            result = await process_tool.execute(
                action="manage_service", service_name="nginx", service_action="status"
            )
        assert result.success is True
        assert "successfully" not in result.output
        assert "active (running)" in result.output

    async def test_failure_surfaces_stderr(self, process_tool: ProcessTool):
        with _patch_subprocess(1, stderr=b"Unit not found"):
            result = await process_tool.execute(
                action="manage_service", service_name="ghost", service_action="restart"
            )
        assert result.success is False
        assert "Unit not found" in result.error

    @pytest.mark.parametrize(
        "service_name",
        [
            "nginx; rm -rf /",
            "nginx $(rm -rf /)",
            "`reboot`",
            "svc with spaces",
            "-flag",
            "a&b",
            "a|b",
        ],
    )
    async def test_rejects_injection_payloads(self, process_tool: ProcessTool, service_name: str):
        """Service names with shell metacharacters / whitespace are rejected.

        Regression guard for the command-injection fix (#312): the value never
        reaches a subprocess, so no shell can interpret it.
        """
        with _patch_subprocess(0) as p:
            result = await process_tool.execute(
                action="manage_service",
                service_name=service_name,
                service_action="status",
            )
        assert result.success is False
        assert "Invalid service name" in result.error
        p.assert_not_awaited()

    async def test_accepts_legitimate_unit_names(self, process_tool: ProcessTool):
        for name in ("nginx", "turing-gateway.service", "user@1000.service", "foo.slice"):
            with _patch_subprocess(0, stdout=b"ok"):
                result = await process_tool.execute(
                    action="manage_service", service_name=name, service_action="status"
                )
            assert result.success is True


# ---------------------------------------------------------------------------
# Windows degradation (issue #399)
# ---------------------------------------------------------------------------


class TestWindowsBehavior:
    """Windows branches, exercised via a monkeypatched platform."""

    async def test_manage_service_degrades_with_clear_error(
        self, process_tool: ProcessTool, monkeypatch: pytest.MonkeyPatch
    ):
        """systemctl does not exist on Windows: no subprocess is spawned and
        the error points at Task Scheduler instead."""
        monkeypatch.setattr(sys, "platform", "win32")
        with _patch_subprocess(0) as p:
            result = await process_tool.execute(
                action="manage_service", service_name="nginx", service_action="start"
            )
        assert result.success is False
        assert "not available on Windows" in result.error
        assert "Task Scheduler" in result.error
        p.assert_not_awaited()

    async def test_kill_process_sigterm_maps_to_terminate(
        self, process_tool: ProcessTool, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(sys, "platform", "win32")
        proc = _fake_process(pid=99, name="victim")
        with patch("turing.tools.process.psutil.Process", return_value=proc):
            result = await process_tool.execute(action="kill_process", pid=99)
        assert result.success is True
        proc.terminate.assert_called_once_with()
        proc.send_signal.assert_not_called()
        proc.kill.assert_not_called()

    async def test_kill_process_sigkill_maps_to_kill(
        self, process_tool: ProcessTool, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(sys, "platform", "win32")
        proc = _fake_process(pid=99)
        with patch("turing.tools.process.psutil.Process", return_value=proc):
            result = await process_tool.execute(
                action="kill_process", pid=99, signal_name="SIGKILL"
            )
        assert result.success is True
        proc.kill.assert_called_once_with()
        proc.send_signal.assert_not_called()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


class TestFormatHelpers:
    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            (512, "512 B"),
            (2048, "2.0 KB"),
            (5 * 1024 * 1024, "5.0 MB"),
            (3 * 1024 * 1024 * 1024, "3.0 GB"),
        ],
    )
    def test_format_bytes(self, size: int, expected: str):
        assert _format_bytes(size) == expected

    def test_format_timestamp_is_utc(self):
        # 1_700_000_000 -> 2023-11-14 22:13:20 UTC
        out = _format_timestamp(1_700_000_000.0)
        assert out == "2023-11-14 22:13:20 UTC"
