"""System information tool for monitoring CPU, memory, disk, temperature, and uptime."""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from typing import Any

import psutil
import structlog

from turing.tools.base import RiskLevel, Tool, ToolResult

logger = structlog.get_logger("turing.tools.system_info")


class SystemInfoTool(Tool):
    """Retrieve system information and resource metrics."""

    @property
    def name(self) -> str:
        return "system_info"

    @property
    def description(self) -> str:
        return (
            "Get system information and metrics. Supported actions: "
            "cpu_usage, memory_usage, disk_usage, temperature, uptime, full_report"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "cpu_usage",
                        "memory_usage",
                        "disk_usage",
                        "temperature",
                        "uptime",
                        "full_report",
                    ],
                    "description": "The system info action to perform",
                },
                "path": {
                    "type": "string",
                    "description": "Mount point for disk_usage (default: /)",
                },
            },
            "required": ["action"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> ToolResult:
        action: str = kwargs.get("action", "")
        if not action:
            return ToolResult(success=False, output="", error="No action specified")

        dispatch = {
            "cpu_usage": self._cpu_usage,
            "memory_usage": self._memory_usage,
            "disk_usage": self._disk_usage,
            "temperature": self._temperature,
            "uptime": self._uptime,
            "full_report": self._full_report,
        }

        handler = dispatch.get(action)
        if handler is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown action '{action}'. Valid: {', '.join(dispatch)}",
            )

        try:
            return await handler(**kwargs)
        except Exception as exc:
            logger.error("system_info_error", action=action, error=str(exc))
            return ToolResult(success=False, output="", error=str(exc))

    async def _cpu_usage(self, **_kwargs: Any) -> ToolResult:
        """Get CPU usage information."""
        cpu_percent = psutil.cpu_percent(interval=0.5)
        cpu_count_logical = psutil.cpu_count(logical=True)
        cpu_count_physical = psutil.cpu_count(logical=False)
        per_cpu = psutil.cpu_percent(interval=0.1, percpu=True)

        try:
            freq = psutil.cpu_freq()
            freq_str = f"{freq.current:.0f} MHz" if freq else "N/A"
            freq_max_str = f"{freq.max:.0f} MHz" if freq and freq.max else "N/A"
        except (AttributeError, FileNotFoundError):
            freq_str = "N/A"
            freq_max_str = "N/A"

        try:
            load_1, load_5, load_15 = psutil.getloadavg()
            load_str = f"{load_1:.2f} / {load_5:.2f} / {load_15:.2f}"
        except (AttributeError, OSError):
            load_str = "N/A"

        lines = [
            "CPU Usage",
            f"{'=' * 40}",
            f"  Overall:       {cpu_percent:.1f}%",
            f"  Cores:         {cpu_count_physical} physical, {cpu_count_logical} logical",
            f"  Frequency:     {freq_str} (max: {freq_max_str})",
            f"  Load avg:      {load_str}",
            f"  Per-core:      {', '.join(f'{p:.1f}%' for p in per_cpu)}",
        ]

        return ToolResult(success=True, output="\n".join(lines))

    async def _memory_usage(self, **_kwargs: Any) -> ToolResult:
        """Get memory usage information."""
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        lines = [
            "Memory Usage",
            f"{'=' * 40}",
            f"  Total:         {_format_bytes(mem.total)}",
            f"  Used:          {_format_bytes(mem.used)} ({mem.percent:.1f}%)",
            f"  Available:     {_format_bytes(mem.available)}",
            f"  Free:          {_format_bytes(mem.free)}",
            f"  Cached:        {_format_bytes(getattr(mem, 'cached', 0))}",
            f"  Buffers:       {_format_bytes(getattr(mem, 'buffers', 0))}",
            "",
            "Swap",
            f"  Total:         {_format_bytes(swap.total)}",
            f"  Used:          {_format_bytes(swap.used)} ({swap.percent:.1f}%)",
            f"  Free:          {_format_bytes(swap.free)}",
        ]

        return ToolResult(success=True, output="\n".join(lines))

    async def _disk_usage(self, **kwargs: Any) -> ToolResult:
        """Get disk usage information."""
        mount_point = kwargs.get("path", "/")

        try:
            usage = psutil.disk_usage(mount_point)
        except OSError as exc:
            return ToolResult(
                success=False, output="", error=f"Cannot read disk at '{mount_point}': {exc}"
            )

        # Also list all partitions.
        partitions = psutil.disk_partitions(all=False)

        lines = [
            f"Disk Usage ({mount_point})",
            f"{'=' * 40}",
            f"  Total:         {_format_bytes(usage.total)}",
            f"  Used:          {_format_bytes(usage.used)} ({usage.percent:.1f}%)",
            f"  Free:          {_format_bytes(usage.free)}",
            "",
            "All Partitions:",
        ]

        for part in partitions:
            try:
                part_usage = psutil.disk_usage(part.mountpoint)
                lines.append(
                    f"  {part.device:20s} -> {part.mountpoint:15s}  "
                    f"{_format_bytes(part_usage.used):>10s} / {_format_bytes(part_usage.total):>10s}  "
                    f"({part_usage.percent:.1f}%)  [{part.fstype}]"
                )
            except (PermissionError, OSError):
                lines.append(
                    f"  {part.device:20s} -> {part.mountpoint:15s}  [access denied]  [{part.fstype}]"
                )

        return ToolResult(success=True, output="\n".join(lines))

    async def _temperature(self, **_kwargs: Any) -> ToolResult:
        """Get system temperature readings.

        Tries the Raspberry Pi thermal zone first, then falls back to psutil.
        """
        lines = ["System Temperature", f"{'=' * 40}"]
        found_any = False

        # Try Raspberry Pi thermal zone first.
        thermal_path = Path("/sys/class/thermal/thermal_zone0/temp")
        if thermal_path.exists():
            try:
                raw = thermal_path.read_text().strip()
                temp_c = int(raw) / 1000.0
                lines.append(f"  CPU (thermal_zone0): {temp_c:.1f} C")
                found_any = True
            except (ValueError, OSError):
                pass

        # Try any other thermal zones.
        thermal_dir = Path("/sys/class/thermal")
        if thermal_dir.exists():
            for zone in sorted(thermal_dir.glob("thermal_zone*")):
                if zone.name == "thermal_zone0" and found_any:
                    continue  # Already handled above.
                temp_file = zone / "temp"
                type_file = zone / "type"
                if temp_file.exists():
                    try:
                        raw = temp_file.read_text().strip()
                        temp_c = int(raw) / 1000.0
                        zone_type = (
                            type_file.read_text().strip() if type_file.exists() else zone.name
                        )
                        lines.append(f"  {zone_type}: {temp_c:.1f} C")
                        found_any = True
                    except (ValueError, OSError):
                        continue

        # Fallback to psutil sensors_temperatures.
        if not found_any:
            try:
                sensors_temperatures = getattr(psutil, "sensors_temperatures", None)
                if sensors_temperatures is None:
                    raise AttributeError("sensors_temperatures unavailable on this platform")
                temps = sensors_temperatures()
                if temps:
                    for sensor_name, entries in temps.items():
                        for entry in entries:
                            label = entry.label or sensor_name
                            lines.append(f"  {label}: {entry.current:.1f} C")
                            found_any = True
                else:
                    lines.append("  No temperature sensors found.")
            except (AttributeError, RuntimeError):
                lines.append("  Temperature reading not available on this platform.")

        if not found_any:
            lines.append("  No temperature sensors found.")

        return ToolResult(success=True, output="\n".join(lines))

    async def _uptime(self, **_kwargs: Any) -> ToolResult:
        """Get system uptime."""
        boot_time = psutil.boot_time()
        now = time.time()
        uptime_seconds = now - boot_time

        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        seconds = int(uptime_seconds % 60)

        boot_dt = datetime.datetime.fromtimestamp(boot_time, tz=datetime.UTC)

        lines = [
            "System Uptime",
            f"{'=' * 40}",
            f"  Uptime:      {days}d {hours}h {minutes}m {seconds}s",
            f"  Boot time:   {boot_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ]

        return ToolResult(success=True, output="\n".join(lines))

    async def _full_report(self, **kwargs: Any) -> ToolResult:
        """Generate a comprehensive system report combining all metrics."""
        sections: list[str] = []

        cpu_result = await self._cpu_usage()
        sections.append(cpu_result.output)

        mem_result = await self._memory_usage()
        sections.append(mem_result.output)

        disk_result = await self._disk_usage(**kwargs)
        sections.append(disk_result.output)

        temp_result = await self._temperature()
        sections.append(temp_result.output)

        uptime_result = await self._uptime()
        sections.append(uptime_result.output)

        full_report = "\n\n".join(sections)
        return ToolResult(success=True, output=full_report)


def _format_bytes(size: int) -> str:
    """Format a byte count as a human-readable string."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.1f} GB"
