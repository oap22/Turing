"""Process management tool for listing, inspecting, and controlling system processes."""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import psutil
import structlog

from turing.tools.base import RiskLevel, Tool, ToolResult

logger = structlog.get_logger("turing.tools.process")


class ProcessTool(Tool):
    """Manage system processes and systemd services."""

    @property
    def name(self) -> str:
        return "process"

    @property
    def description(self) -> str:
        return (
            "Manage system processes. Supported actions: "
            "list_processes, get_process_info, kill_process, manage_service"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_processes",
                        "get_process_info",
                        "kill_process",
                        "manage_service",
                    ],
                    "description": "The process management action to perform",
                },
                "pid": {
                    "type": "integer",
                    "description": "Process ID (for get_process_info and kill_process)",
                },
                "signal_name": {
                    "type": "string",
                    "description": "Signal to send (for kill_process). Default: SIGTERM",
                    "enum": ["SIGTERM", "SIGKILL", "SIGINT", "SIGHUP"],
                },
                "service_name": {
                    "type": "string",
                    "description": "Systemd service name (for manage_service)",
                },
                "service_action": {
                    "type": "string",
                    "enum": ["start", "stop", "restart", "status"],
                    "description": "Action to perform on the service",
                },
            },
            "required": ["action"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.HIGH

    def get_action_risk(self, action: str) -> RiskLevel:
        """Return the risk level for a specific action."""
        if action in ("list_processes", "get_process_info"):
            return RiskLevel.LOW
        return RiskLevel.HIGH

    async def execute(self, **kwargs: Any) -> ToolResult:
        action: str = kwargs.get("action", "")
        if not action:
            return ToolResult(success=False, output="", error="No action specified")

        dispatch = {
            "list_processes": self._list_processes,
            "get_process_info": self._get_process_info,
            "kill_process": self._kill_process,
            "manage_service": self._manage_service,
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
        except psutil.NoSuchProcess as exc:
            return ToolResult(success=False, output="", error=f"Process not found: {exc}")
        except psutil.AccessDenied as exc:
            return ToolResult(success=False, output="", error=f"Access denied: {exc}")
        except Exception as exc:
            logger.error("process_error", action=action, error=str(exc))
            return ToolResult(success=False, output="", error=str(exc))

    async def _list_processes(self, **_kwargs: Any) -> ToolResult:
        """List running processes sorted by CPU usage."""
        procs: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
            try:
                info = proc.info
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Sort by CPU percent descending.
        procs.sort(key=lambda p: p.get("cpu_percent", 0) or 0, reverse=True)

        lines = [
            f"{'PID':>7s}  {'CPU%':>6s}  {'MEM%':>6s}  {'STATUS':10s}  NAME",
            f"{'---':>7s}  {'----':>6s}  {'----':>6s}  {'------':10s}  ----",
        ]
        for p in procs[:50]:  # Limit to top 50.
            pid = p.get("pid", 0)
            cpu = p.get("cpu_percent", 0.0) or 0.0
            mem = p.get("memory_percent", 0.0) or 0.0
            status = p.get("status", "unknown")
            pname = p.get("name", "unknown")
            lines.append(f"{pid:>7d}  {cpu:>6.1f}  {mem:>6.1f}  {status:10s}  {pname}")

        return ToolResult(success=True, output="\n".join(lines))

    async def _get_process_info(self, **kwargs: Any) -> ToolResult:
        """Get detailed information about a specific process."""
        pid = kwargs.get("pid")
        if pid is None:
            return ToolResult(success=False, output="", error="No PID provided")

        proc = psutil.Process(int(pid))
        with proc.oneshot():
            info_lines = [
                f"Process Information (PID {pid})",
                f"{'=' * 40}",
                f"  Name:           {proc.name()}",
                f"  Status:         {proc.status()}",
                f"  PID:            {proc.pid}",
                f"  PPID:           {proc.ppid()}",
                f"  Username:       {proc.username()}",
                f"  CPU %:          {proc.cpu_percent(interval=0.1):.1f}%",
                f"  Memory %:       {proc.memory_percent():.1f}%",
                f"  Memory (RSS):   {_format_bytes(proc.memory_info().rss)}",
                f"  Memory (VMS):   {_format_bytes(proc.memory_info().vms)}",
                f"  Threads:        {proc.num_threads()}",
                f"  Created:        {_format_timestamp(proc.create_time())}",
            ]
            try:
                cmdline = " ".join(proc.cmdline())
                info_lines.append(f"  Command line:   {cmdline[:200]}")
            except (psutil.AccessDenied, psutil.ZombieProcess):
                info_lines.append("  Command line:   [access denied]")

            try:
                cwd = proc.cwd()
                info_lines.append(f"  Working dir:    {cwd}")
            except (psutil.AccessDenied, psutil.ZombieProcess):
                info_lines.append("  Working dir:    [access denied]")

        return ToolResult(success=True, output="\n".join(info_lines))

    async def _kill_process(self, **kwargs: Any) -> ToolResult:
        """Send a signal to a process."""
        pid = kwargs.get("pid")
        if pid is None:
            return ToolResult(success=False, output="", error="No PID provided")

        signal_name = kwargs.get("signal_name", "SIGTERM")
        sig = getattr(signal, signal_name, signal.SIGTERM)

        proc = psutil.Process(int(pid))
        proc_name = proc.name()
        proc.send_signal(sig)

        logger.warning("process_killed", pid=pid, signal=signal_name, name=proc_name)
        return ToolResult(
            success=True,
            output=f"Sent {signal_name} to process {pid} ({proc_name})",
        )

    async def _manage_service(self, **kwargs: Any) -> ToolResult:
        """Manage a systemd service (start, stop, restart, status)."""
        service_name = kwargs.get("service_name", "")
        service_action = kwargs.get("service_action", "")

        if not service_name:
            return ToolResult(success=False, output="", error="No service name provided")
        if not service_action:
            return ToolResult(success=False, output="", error="No service action provided")
        if service_action not in ("start", "stop", "restart", "status"):
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid service action '{service_action}'. Valid: start, stop, restart, status",
            )

        # Sanitize service name to prevent injection.
        safe_name = service_name.replace(";", "").replace("&", "").replace("|", "").strip()

        cmd = f"systemctl {service_action} {safe_name}"
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        success = process.returncode == 0
        output = stdout_text if stdout_text else stderr_text
        error = stderr_text if not success else ""

        action_past = {
            "start": "started",
            "stop": "stopped",
            "restart": "restarted",
            "status": "queried",
        }[service_action]

        if success and service_action != "status":
            output = f"Service '{safe_name}' {action_past} successfully.\n{output}"

        logger.info(
            "service_managed",
            service=safe_name,
            action=service_action,
            success=success,
        )
        return ToolResult(success=success, output=output, error=error)


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


def _format_timestamp(ts: float) -> str:
    """Format a UNIX timestamp as a human-readable datetime string."""
    import datetime

    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
