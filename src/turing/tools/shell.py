"""Shell command execution tool with sandboxing and safety checks."""

from __future__ import annotations

import asyncio
import shlex
import shutil
from pathlib import Path
from typing import Any

import structlog

from turing import oscompat
from turing.tools.base import RiskLevel, Tool, ToolResult
from turing.tools.command_safety import check_denylist, classify_command_risk

logger = structlog.get_logger("turing.tools.shell")

# Maximum output length before truncation (characters).
MAX_OUTPUT_LENGTH = 4000

# Filesystem locations bound read-only into the bubblewrap sandbox so commands
# can find their binaries and shared libraries. Home directories, /root, and
# the project tree are deliberately NOT bound — that keeps ``.env``, SSH keys,
# and the rest of the host out of a sandboxed command's reach (issue #240).
_SANDBOX_RO_PATHS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")


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
        # Name the shell so the LLM writes syntax that will actually run.
        if oscompat.is_windows():
            return "Execute a shell command on the system (runs under PowerShell)"
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
        """Determine the risk level of a specific command.

        Classification is parse-based (see :mod:`turing.tools.command_safety`):
        a command is only MEDIUM when every pipeline segment is a known
        read-only builtin. A shell metacharacter can no longer downgrade a
        destructive command via a "safe" prefix.
        """
        tier = classify_command_risk(command)
        return RiskLevel.MEDIUM if tier == "medium" else RiskLevel.HIGH

    def check_denylist(self, command: str) -> tuple[bool, str]:
        """Check whether the command matches any shared deny pattern.

        Returns:
            A tuple of (is_denied, reason).  ``is_denied`` is True when the
            command matches a blocked pattern.
        """
        return check_denylist(command)

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
        try:
            final_command = self._wrap_with_sandbox(command)
        except RuntimeError as exc:
            # Sandbox required but unavailable — fail closed, never run
            # the command unsandboxed.
            logger.warning("shell_sandbox_unavailable", command=command, error=str(exc))
            return ToolResult(success=False, output="", error=str(exc))

        # --- Execute ---
        try:
            if oscompat.is_windows():
                # Run under PowerShell explicitly (create_subprocess_shell
                # would use cmd.exe via %COMSPEC%) and suppress the console
                # window a GUI-launched process would otherwise flash.
                process = await asyncio.create_subprocess_exec(
                    *oscompat.windows_shell_argv(final_command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=oscompat.subprocess_creation_flags(),
                )
            else:
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
        """Wrap the command with a hardened bubblewrap invocation.

        Fails **closed**: when sandboxing is enabled but ``bwrap`` is not
        installed, raises :class:`RuntimeError` instead of running the
        command unsandboxed (``execute`` turns that into a denial).

        The sandbox binds only the OS directories a command needs to run
        (read-only), mounts a fresh ``/tmp``, drops the network, and clears
        the environment — so neither host secrets (``.env``, SSH keys, the
        project tree) nor inherited API keys are reachable inside it.
        """
        if not self._sandbox_enabled:
            return command

        if oscompat.is_windows():
            # bubblewrap is Linux-only; there is no equivalent sandbox here.
            # Same fail-closed posture, but an honest message instead of
            # "install bwrap" (which is impossible on Windows).
            raise RuntimeError(
                "sandbox_enabled is set but bubblewrap sandboxing is not "
                "available on Windows; set TURING_SANDBOX_ENABLED=false to "
                "run shell commands unsandboxed"
            )

        bwrap_path = shutil.which("bwrap")
        if bwrap_path is None:
            raise RuntimeError(
                "sandbox_enabled is set but bubblewrap (bwrap) is not installed; "
                "refusing to run the command unsandboxed"
            )

        bwrap_args: list[str] = [bwrap_path]
        # Bind only the OS locations that exist — binding a missing path
        # makes bwrap abort.
        for ro_path in _SANDBOX_RO_PATHS:
            if Path(ro_path).exists():
                bwrap_args += ["--ro-bind", ro_path, ro_path]
        bwrap_args += [
            "--tmpfs", "/tmp",
            "--dev", "/dev",
            "--proc", "/proc",
            "--chdir", "/tmp",
            "--unshare-net",
            "--die-with-parent",
            "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin:/usr/sbin:/sbin",
            "--setenv", "HOME", "/tmp",
            "--",
            "sh", "-c", command,
        ]  # fmt: skip
        # Shell-quote each argument for safe embedding.
        return " ".join(shlex.quote(arg) for arg in bwrap_args)
