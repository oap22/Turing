"""Tests for the SystemInfoTool.

All ``psutil`` calls are mocked so output is deterministic across hosts (CI,
macOS dev laptops, Raspberry Pi). The temperature action reads the Linux
``/sys/class/thermal`` tree, so those tests redirect ``Path`` into ``tmp_path``
to exercise both the Pi thermal-zone branch and the psutil fallback branch
without depending on the host platform.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import psutil
import pytest

from turing.tools.base import RiskLevel
from turing.tools.system_info import SystemInfoTool, _format_bytes


@pytest.fixture()
def system_info_tool() -> SystemInfoTool:
    return SystemInfoTool()


# ---------------------------------------------------------------------------
# Contract + dispatch
# ---------------------------------------------------------------------------


class TestContract:
    def test_identity_and_risk(self, system_info_tool: SystemInfoTool):
        assert system_info_tool.name == "system_info"
        assert system_info_tool.risk_level is RiskLevel.LOW
        assert system_info_tool.requires_confirmation is False

    def test_schema_actions(self, system_info_tool: SystemInfoTool):
        enum = system_info_tool.parameters["properties"]["action"]["enum"]
        assert set(enum) == {
            "cpu_usage",
            "memory_usage",
            "disk_usage",
            "temperature",
            "uptime",
            "full_report",
        }

    async def test_missing_action(self, system_info_tool: SystemInfoTool):
        result = await system_info_tool.execute()
        assert result.success is False
        assert "No action specified" in result.error

    async def test_unknown_action(self, system_info_tool: SystemInfoTool):
        result = await system_info_tool.execute(action="gpu_usage")
        assert result.success is False
        assert "Unknown action 'gpu_usage'" in result.error

    async def test_handler_exception_is_caught(self, system_info_tool: SystemInfoTool):
        with patch("turing.tools.system_info.psutil.cpu_percent", side_effect=RuntimeError("nope")):
            result = await system_info_tool.execute(action="cpu_usage")
        assert result.success is False
        assert "nope" in result.error


# ---------------------------------------------------------------------------
# cpu_usage
# ---------------------------------------------------------------------------


class TestCpuUsage:
    async def test_full_metrics(self, system_info_tool: SystemInfoTool):
        with patch("turing.tools.system_info.psutil") as mock_psutil:
            mock_psutil.cpu_percent.side_effect = [42.0, [10.0, 20.0]]
            mock_psutil.cpu_count.side_effect = [8, 4]  # logical, physical
            mock_psutil.cpu_freq.return_value = MagicMock(current=1500.0, max=2400.0)
            mock_psutil.getloadavg.return_value = (0.5, 0.4, 0.3)
            result = await system_info_tool.execute(action="cpu_usage")
        assert result.success is True
        assert "Overall:       42.0%" in result.output
        assert "4 physical, 8 logical" in result.output
        assert "1500 MHz (max: 2400 MHz)" in result.output
        assert "0.50 / 0.40 / 0.30" in result.output

    async def test_freq_and_loadavg_unavailable(self, system_info_tool: SystemInfoTool):
        with patch("turing.tools.system_info.psutil") as mock_psutil:
            mock_psutil.cpu_percent.side_effect = [42.0, [10.0]]
            mock_psutil.cpu_count.side_effect = [8, 4]
            mock_psutil.cpu_freq.side_effect = FileNotFoundError
            mock_psutil.getloadavg.side_effect = OSError
            result = await system_info_tool.execute(action="cpu_usage")
        assert result.success is True
        assert "Frequency:     N/A (max: N/A)" in result.output
        assert "Load avg:      N/A" in result.output


# ---------------------------------------------------------------------------
# memory_usage
# ---------------------------------------------------------------------------


class TestMemoryUsage:
    async def test_reports_memory_and_swap(self, system_info_tool: SystemInfoTool):
        mem = MagicMock(
            total=8 * 1024**3,
            used=4 * 1024**3,
            available=3 * 1024**3,
            free=1 * 1024**3,
            percent=50.0,
            cached=512 * 1024**2,
            buffers=128 * 1024**2,
        )
        swap = MagicMock(total=2 * 1024**3, used=0, free=2 * 1024**3, percent=0.0)
        with patch("turing.tools.system_info.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mem
            mock_psutil.swap_memory.return_value = swap
            result = await system_info_tool.execute(action="memory_usage")
        assert result.success is True
        assert "Total:         8.0 GB" in result.output
        assert "Used:          4.0 GB (50.0%)" in result.output
        assert "Swap" in result.output


# ---------------------------------------------------------------------------
# disk_usage
# ---------------------------------------------------------------------------


class TestDiskUsage:
    async def test_default_mount_and_partition_list(self, system_info_tool: SystemInfoTool):
        usage = MagicMock(total=100 * 1024**3, used=40 * 1024**3, free=60 * 1024**3, percent=40.0)
        part = MagicMock(device="/dev/sda1", mountpoint="/", fstype="ext4")
        with patch("turing.tools.system_info.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = usage
            mock_psutil.disk_partitions.return_value = [part]
            result = await system_info_tool.execute(action="disk_usage")
        assert result.success is True
        assert "Disk Usage (/)" in result.output
        assert "40.0%" in result.output
        assert "/dev/sda1" in result.output

    async def test_bad_mount_point(self, system_info_tool: SystemInfoTool):
        with patch("turing.tools.system_info.psutil") as mock_psutil:
            mock_psutil.disk_usage.side_effect = OSError("no such mount")
            result = await system_info_tool.execute(action="disk_usage", path="/nope")
        assert result.success is False
        assert "Cannot read disk at '/nope'" in result.error

    async def test_partition_access_denied_is_graceful(self, system_info_tool: SystemInfoTool):
        usage = MagicMock(total=1024**3, used=512 * 1024**2, free=512 * 1024**2, percent=50.0)
        part = MagicMock(device="/dev/loop0", mountpoint="/snap", fstype="squashfs")
        with patch("turing.tools.system_info.psutil") as mock_psutil:
            # First call (root usage) succeeds, second (partition) raises.
            mock_psutil.disk_usage.side_effect = [usage, PermissionError("denied")]
            mock_psutil.disk_partitions.return_value = [part]
            result = await system_info_tool.execute(action="disk_usage")
        assert result.success is True
        assert "[access denied]" in result.output


# ---------------------------------------------------------------------------
# temperature
# ---------------------------------------------------------------------------


def _redirect_thermal(monkeypatch: pytest.MonkeyPatch, base: pathlib.Path) -> None:
    """Redirect ``/sys/class/thermal`` reads into ``base`` (under tmp_path)."""
    real_path = pathlib.Path

    def fake_path(p: str | pathlib.Path) -> pathlib.Path:
        s = str(p)
        prefix = "/sys/class/thermal"
        if s.startswith(prefix):
            return real_path(str(base)) / s[1:]  # strip leading '/'
        return real_path(p)

    monkeypatch.setattr("turing.tools.system_info.Path", fake_path)


class TestTemperature:
    async def test_raspberry_pi_thermal_zone(
        self, system_info_tool: SystemInfoTool, tmp_path: pathlib.Path, monkeypatch
    ):
        zone = tmp_path / "sys/class/thermal/thermal_zone0"
        zone.mkdir(parents=True)
        (zone / "temp").write_text("48123\n")
        (zone / "type").write_text("cpu-thermal\n")
        _redirect_thermal(monkeypatch, tmp_path)

        result = await system_info_tool.execute(action="temperature")
        assert result.success is True
        assert "48.1 C" in result.output

    async def test_psutil_fallback_when_no_thermal_tree(
        self, system_info_tool: SystemInfoTool, tmp_path: pathlib.Path, monkeypatch
    ):
        # tmp_path has no sys/class/thermal subtree -> fall back to psutil.
        _redirect_thermal(monkeypatch, tmp_path)
        entry = MagicMock(label="Core 0", current=55.0)
        monkeypatch.setattr(
            psutil, "sensors_temperatures", lambda: {"coretemp": [entry]}, raising=False
        )
        result = await system_info_tool.execute(action="temperature")
        assert result.success is True
        assert "Core 0: 55.0 C" in result.output

    async def test_no_sensors_anywhere(
        self, system_info_tool: SystemInfoTool, tmp_path: pathlib.Path, monkeypatch
    ):
        _redirect_thermal(monkeypatch, tmp_path)
        monkeypatch.setattr(psutil, "sensors_temperatures", lambda: {}, raising=False)
        result = await system_info_tool.execute(action="temperature")
        assert result.success is True
        assert "No temperature sensors found" in result.output


# ---------------------------------------------------------------------------
# uptime
# ---------------------------------------------------------------------------


class TestUptime:
    async def test_uptime_formatting(self, system_info_tool: SystemInfoTool):
        # boot 1 day, 2 hours, 3 minutes, 4 seconds before "now".
        delta = 86400 + 2 * 3600 + 3 * 60 + 4
        with (
            patch("turing.tools.system_info.psutil") as mock_psutil,
            patch("turing.tools.system_info.time.time", return_value=2_000_000_000.0),
        ):
            mock_psutil.boot_time.return_value = 2_000_000_000.0 - delta
            result = await system_info_tool.execute(action="uptime")
        assert result.success is True
        assert "1d 2h 3m 4s" in result.output


# ---------------------------------------------------------------------------
# full_report
# ---------------------------------------------------------------------------


class TestFullReport:
    async def test_combines_all_sections(
        self, system_info_tool: SystemInfoTool, tmp_path: pathlib.Path, monkeypatch
    ):
        _redirect_thermal(monkeypatch, tmp_path)
        monkeypatch.setattr(psutil, "sensors_temperatures", lambda: {}, raising=False)
        usage = MagicMock(total=1024**3, used=0, free=1024**3, percent=0.0)
        mem = MagicMock(
            total=1024**3,
            used=0,
            available=1024**3,
            free=1024**3,
            percent=0.0,
            cached=0,
            buffers=0,
        )
        swap = MagicMock(total=0, used=0, free=0, percent=0.0)
        with (
            patch("turing.tools.system_info.psutil") as mock_psutil,
            patch("turing.tools.system_info.time.time", return_value=2_000_000_000.0),
        ):
            mock_psutil.cpu_percent.side_effect = [1.0, [1.0]]
            mock_psutil.cpu_count.side_effect = [1, 1]
            mock_psutil.cpu_freq.return_value = None
            mock_psutil.getloadavg.return_value = (0.0, 0.0, 0.0)
            mock_psutil.virtual_memory.return_value = mem
            mock_psutil.swap_memory.return_value = swap
            mock_psutil.disk_usage.return_value = usage
            mock_psutil.disk_partitions.return_value = []
            mock_psutil.boot_time.return_value = 2_000_000_000.0 - 60
            mock_psutil.sensors_temperatures = lambda: {}
            result = await system_info_tool.execute(action="full_report")
        assert result.success is True
        for header in (
            "CPU Usage",
            "Memory Usage",
            "Disk Usage",
            "System Temperature",
            "System Uptime",
        ):
            assert header in result.output


# ---------------------------------------------------------------------------
# _format_bytes
# ---------------------------------------------------------------------------


class TestFormatBytes:
    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            (0, "0 B"),
            (1023, "1023 B"),
            (1024, "1.0 KB"),
            (1024 * 1024, "1.0 MB"),
            (1024**3, "1.0 GB"),
        ],
    )
    def test_thresholds(self, size: int, expected: str):
        assert _format_bytes(size) == expected
