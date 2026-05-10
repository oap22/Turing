"""Shell command execution tool with sandboxing and safety checks."""

from __future__ import annotations

import asyncio
import re
import shutil
from typing import Any

import structlog

from turing.tools.base import RiskLevel, Tool, ToolResult

logger = structlog.get_logger("turing.tools.shell")

# Maximum output length before truncation (characters).
MAX_OUTPUT_LENGTH = 4000

# Commands considered safe enough for MEDIUM risk instead of HIGH.
SAFE_COMMAND_PREFIXES = (
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
    "du",
    "free",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "grep",
    "find",
    "which",
    "env",
    "printenv",
    "id",
    "ps",
    "top",
    "htop",
    "ip",
    "ifconfig",
    "ping",
    "dig",
    "nslookup",
    "traceroute",
    "ss",
    "netstat",
    "lsblk",
    "lscpu",
    "lsusb",
    "dmesg",
    "journalctl",
    "systemctl status",
    "vcgencmd",
)

# Deny patterns -- commands that are never allowed.
DENY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rm\s+-rf\s+/(?!\w)", re.IGNORECASE),
    re.compile(r"mkfs", re.IGNORECASE),
    re.compile(r"dd\s+.*of=/dev/", re.IGNORECASE),
    re.compile(r":\(\)\{.*\|.*&\s*\};:", re.IGNORECASE),
    re.compile(r"chmod\s+-R\s+777\s+/", re.IGNORECASE),
    re.compile(r">\s*/dev/sd", re.IGNORECASE),
    re.compile(r"curl.*\|\s*(bash|sh)", re.IGNORECASE),
    re.compile(r"wget.*\|\s*(bash|sh)", re.IGNORECASE),
]


class ShellTool(Tool):
    """Execute shell commands with optional bubblewrap sandboxing."""

    def __init__(self, config: Any | None = None) -> None:
        self._config = config
        self._sandbox_enabled = getattr(config, "sandbox_enabled", False) if config else False
        self._sandbox_timeout = getattr(config, "sandbox_timeout", 30) if config else 30

    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        return "Execute a shell command on the system"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
            },
            "required": ["command"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.HIGH

    def get_command_risk(self, command: str) -> RiskLevel:
        """Determine the risk level of a specific command."""
        stripped = command.strip()
        for prefix in SAFE_COMMAND_PREFIXES:
            if stripped.startswith(prefix):
                return RiskLevel.MEDIUM
        return RiskLevel.HIGH

    def check_denylist(self, command: str) -> tuple[bool, str]:
        """Check whether the command matches any deny pattern.

        Returns:
            A tuple of (is_denied, reason).  ``is_denied`` is True when the
            command matches a blocked pattern.
        """
        for pattern in DENY_PATTERNS:
            if pattern.search(command):
                return True, f"Command blocked by deny pattern: {pattern.pattern}"
        return False, ""

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute a shell command.

        Parameters:
            command: The shell command string to run.
        """
        command: str = kwargs.get("command", "")
        if not command:
            return ToolResult(success=False, output="", error="No command provided")

        # --- Deny-list check ---
        is_denied, reason = self.check_denylist(command)
        if is_denied:
            logger.warning("shell_command_denied", command=command, reason=reason)
            return ToolResult(success=False, output="", error=reason)

        # --- Build final command (with optional sandbox) ---
        final_command = self._wrap_with_sandbox(command)

        # --- Execute ---
        try:
            process = await asyncio.create_subprocess_shell(
                final_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self._sandbox_timeout,
                )
            except TimeoutError:
                process.kill()
                await process.communicate()
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Command timed out after {self._sandbox_timeout}s",
                )

            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            # --- Truncation ---
            truncated = False
            if len(stdout_text) > MAX_OUTPUT_LENGTH:
                stdout_text = stdout_text[:MAX_OUTPUT_LENGTH] + "\n... (output truncated)"
                truncated = True

            success = process.returncode == 0
            output = stdout_text
            if stderr_text and not success:
                output = (
                    f"{stdout_text}\n--- stderr ---\n{stderr_text}" if stdout_text else stderr_text
                )

            return ToolResult(
                success=success,
                output=output,
                error=stderr_text if not success else "",
                truncated=truncated,
            )

        except OSError as exc:
            logger.error("shell_execution_error", command=command, error=str(exc))
            return ToolResult(success=False, output="", error=f"Failed to execute command: {exc}")

    def _wrap_with_sandbox(self, command: str) -> str:
        """Optionally wrap the command with bubblewrap if sandboxing is enabled."""
        if not self._sandbox_enabled:
            return command

        bwrap_path = shutil.which("bwrap")
        if bwrap_path is None:
            logger.warning(
                "bwrap_not_found",
                msg="Bubblewrap not available, executing without sandbox",
            )
            return command

        # Build bwrap invocation with restricted filesystem access.
        bwrap_args = [
            bwrap_path,
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--unshare-net",
            "--die-with-parent",
            "--",
            "sh",
            "-c",
            command,
        ]
        # Shell-quote each argument for safe embedding.
        import shlex

        return " ".join(shlex.quote(arg) for arg in bwrap_args)
