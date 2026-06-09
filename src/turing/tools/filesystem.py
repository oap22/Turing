"""Filesystem operations tool with path-based access control."""

from __future__ import annotations

import glob as globmod
from pathlib import Path
from typing import Any

import structlog

from turing.tools.base import RiskLevel, Tool, ToolResult

logger = structlog.get_logger("turing.tools.filesystem")

# Maximum file size for read operations (100 KB).
MAX_READ_SIZE = 100 * 1024

# Read actions (read_file/list_directory/search_files) are auto-approved by the
# safety gate, so they must refuse to surface credential material directly. The
# patterns below are matched against the *resolved* path (symlinks followed) so
# a symlink under an allowed dir cannot point at a secret and exfiltrate it.
_SENSITIVE_NAMES = frozenset(
    {
        ".netrc",
        ".pgpass",
        ".htpasswd",
        "shadow",
        "credentials",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_SENSITIVE_DIR_COMPONENTS = frozenset({".ssh", ".aws", ".gnupg"})
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _is_sensitive_path(path: Path) -> bool:
    """True if ``path`` (resolved) names a secret file or lives in a secret dir."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    name = resolved.name
    lower = name.lower()
    if name in _SENSITIVE_NAMES:
        return True
    # dotenv files in any form: ".env", ".env.local", "production.env".
    if lower.startswith(".env") or lower.endswith(".env"):
        return True
    if lower.endswith(_SENSITIVE_SUFFIXES):
        return True
    return bool({part.lower() for part in resolved.parts} & _SENSITIVE_DIR_COMPONENTS)


class FileSystemTool(Tool):
    """Perform filesystem operations: read, write, list, and search."""

    def __init__(self, config: Any | None = None) -> None:
        self._config = config
        self._allowed_write_paths: list[str] = (
            getattr(config, "allowed_write_paths", ["/tmp"]) if config else ["/tmp"]
        )

    @property
    def name(self) -> str:
        return "filesystem"

    @property
    def description(self) -> str:
        return (
            "Perform filesystem operations. Supported actions: "
            "read_file, write_file, list_directory, search_files"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read_file", "write_file", "list_directory", "search_files"],
                    "description": "The filesystem action to perform",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory path (absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write (for write_file action)",
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern for search_files (e.g. '*.py')",
                },
            },
            "required": ["action", "path"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        # Overall tool is MEDIUM because write_file can modify files.
        return RiskLevel.MEDIUM

    def get_action_risk(self, action: str) -> RiskLevel:
        """Return the risk level for a specific action."""
        if action == "write_file":
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> ToolResult:
        action: str = kwargs.get("action", "")
        path_str: str = kwargs.get("path", "")

        if not action:
            return ToolResult(success=False, output="", error="No action specified")
        if not path_str:
            return ToolResult(success=False, output="", error="No path specified")

        dispatch = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "list_directory": self._list_directory,
            "search_files": self._search_files,
        }

        handler = dispatch.get(action)
        if handler is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown action '{action}'. Valid: {', '.join(dispatch)}",
            )

        try:
            return await handler(path_str, **kwargs)
        except PermissionError:
            return ToolResult(success=False, output="", error=f"Permission denied: {path_str}")
        except Exception as exc:
            logger.error("filesystem_error", action=action, path=path_str, error=str(exc))
            return ToolResult(success=False, output="", error=str(exc))

    @staticmethod
    def _deny_if_sensitive(path: Path) -> ToolResult | None:
        """Return a denial result if ``path`` resolves to credential material."""
        if _is_sensitive_path(path):
            return ToolResult(
                success=False,
                output="",
                error=f"Access denied: {path} matches a protected secret path",
            )
        return None

    async def _read_file(self, path_str: str, **_kwargs: Any) -> ToolResult:
        """Read a file's contents (up to MAX_READ_SIZE bytes)."""
        filepath = Path(path_str)
        if (denied := self._deny_if_sensitive(filepath)) is not None:
            return denied
        if not filepath.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path_str}")
        if not filepath.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path_str}")

        file_size = filepath.stat().st_size
        if file_size > MAX_READ_SIZE:
            return ToolResult(
                success=False,
                output="",
                error=f"File too large ({file_size} bytes). Maximum is {MAX_READ_SIZE} bytes.",
            )

        content = filepath.read_text(encoding="utf-8", errors="replace")
        return ToolResult(success=True, output=content)

    async def _write_file(self, path_str: str, **kwargs: Any) -> ToolResult:
        """Write content to a file (restricted to allowed paths)."""
        content: str = kwargs.get("content", "")
        if content is None:
            content = ""

        filepath = Path(path_str).resolve()

        # Path validation: the file must reside under an allowed write path.
        if not self._is_write_allowed(filepath):
            allowed = ", ".join(self._allowed_write_paths)
            return ToolResult(
                success=False,
                output="",
                error=f"Write denied: {filepath} is not under allowed paths ({allowed})",
            )

        # Ensure parent directory exists.
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        logger.info("file_written", path=str(filepath), size=len(content))
        return ToolResult(
            success=True, output=f"Successfully wrote {len(content)} bytes to {filepath}"
        )

    async def _list_directory(self, path_str: str, **_kwargs: Any) -> ToolResult:
        """List the contents of a directory."""
        dirpath = Path(path_str)
        if (denied := self._deny_if_sensitive(dirpath)) is not None:
            return denied
        if not dirpath.exists():
            return ToolResult(success=False, output="", error=f"Directory not found: {path_str}")
        if not dirpath.is_dir():
            return ToolResult(success=False, output="", error=f"Not a directory: {path_str}")

        entries: list[str] = []
        try:
            for entry in sorted(dirpath.iterdir()):
                entry_type = "dir" if entry.is_dir() else "file"
                try:
                    size = entry.stat().st_size if entry.is_file() else 0
                except OSError:
                    size = 0
                size_str = _format_bytes(size) if entry.is_file() else "-"
                entries.append(f"  [{entry_type}] {entry.name:40s} {size_str}")
        except PermissionError:
            return ToolResult(success=False, output="", error=f"Permission denied: {path_str}")

        header = f"Directory listing of {path_str} ({len(entries)} entries):\n"
        return ToolResult(success=True, output=header + "\n".join(entries))

    async def _search_files(self, path_str: str, **kwargs: Any) -> ToolResult:
        """Search for files matching a glob pattern under a directory."""
        pattern: str = kwargs.get("pattern", "*")
        dirpath = Path(path_str)
        if (denied := self._deny_if_sensitive(dirpath)) is not None:
            return denied
        if not dirpath.exists():
            return ToolResult(success=False, output="", error=f"Directory not found: {path_str}")
        if not dirpath.is_dir():
            return ToolResult(success=False, output="", error=f"Not a directory: {path_str}")

        full_pattern = str(dirpath / "**" / pattern)
        # Filter out any secret paths so search can't enumerate credentials.
        matches = sorted(
            m for m in globmod.glob(full_pattern, recursive=True) if not _is_sensitive_path(Path(m))
        )

        if not matches:
            return ToolResult(success=True, output=f"No files matching '{pattern}' in {path_str}")

        # Limit output to first 200 matches.
        truncated = len(matches) > 200
        display = matches[:200]
        lines = [f"Found {len(matches)} file(s) matching '{pattern}' in {path_str}:"]
        for m in display:
            lines.append(f"  {m}")
        if truncated:
            lines.append(f"  ... and {len(matches) - 200} more")
        return ToolResult(success=True, output="\n".join(lines), truncated=truncated)

    def _is_write_allowed(self, filepath: Path) -> bool:
        """Check whether the resolved filepath is under an allowed write path."""
        resolved = filepath.resolve()
        for allowed in self._allowed_write_paths:
            allowed_path = Path(allowed).resolve()
            try:
                resolved.relative_to(allowed_path)
                return True
            except ValueError:
                continue
        return False


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
