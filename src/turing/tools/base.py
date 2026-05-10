"""Tool interface and registry for the Turing agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from turing.telemetry import traced

if TYPE_CHECKING:
    from turing.llm.base import ToolDefinition

logger = structlog.get_logger("turing.tools")


def _tool_dispatch_payload(kind, args, kwargs, result, exc):  # type: ignore[no-untyped-def]
    name = args[1] if len(args) > 1 else kwargs.get("tool_name", "")
    data: dict[str, Any] = {"tool": name}
    if kind == "end" and result is not None:
        data["success"] = bool(getattr(result, "success", False))
    if kind == "error" and exc is not None:
        data["error_type"] = type(exc).__name__
    return data


class RiskLevel(StrEnum):
    """Risk classification for tool operations."""

    LOW = "low"  # Read-only operations
    MEDIUM = "medium"  # Network access, non-destructive writes
    HIGH = "high"  # Process management, system changes, destructive ops


@dataclass
class ToolResult:
    """Result returned by a tool execution."""

    success: bool
    output: str
    error: str = ""
    truncated: bool = False


class Tool(ABC):
    """Base class for all tools."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this tool."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this tool does."""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema describing accepted parameters."""
        ...

    @property
    def risk_level(self) -> RiskLevel:
        """Default risk level for this tool."""
        return RiskLevel.LOW

    @property
    def requires_confirmation(self) -> bool:
        """Whether this tool requires user confirmation before execution."""
        return self.risk_level == RiskLevel.HIGH

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with the given arguments."""
        ...

    def to_tool_definition(self) -> ToolDefinition:
        """Convert to LLM ToolDefinition format."""
        from turing.llm.base import ToolDefinition

        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


class ToolRegistry:
    """Registry of available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool in the registry.

        Raises ValueError if a tool with the same name is already registered.
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        logger.info("tool_registered", tool_name=tool.name, risk_level=tool.risk_level.value)

    def get(self, name: str) -> Tool | None:
        """Retrieve a tool by name, or None if not found."""
        return self._tools.get(name)

    def get_all(self) -> list[Tool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def get_definitions(self) -> list[ToolDefinition]:
        """Return LLM-compatible ToolDefinition objects for all registered tools."""

        return [tool.to_tool_definition() for tool in self._tools.values()]

    @traced("tool.dispatch", payload=_tool_dispatch_payload)
    async def execute(self, tool_name: str, /, **kwargs: Any) -> ToolResult:
        """Execute a tool by name with the given arguments.

        ``tool_name`` is positional-only so a tool whose own parameter list
        contains a ``name`` field (the example plugin's ``hello`` tool, for
        instance) doesn't collide with the dispatch parameter — see
        regression test ``tests/test_tools/test_registry_kwargs_collision``.

        Returns a ToolResult with an error if the tool is not found or
        execution fails.
        """
        tool = self.get(tool_name)
        if tool is None:
            return ToolResult(success=False, output="", error=f"Tool '{tool_name}' not found")
        try:
            return await tool.execute(**kwargs)
        except Exception as exc:
            logger.error("tool_execution_error", tool_name=tool_name, error=str(exc))
            return ToolResult(success=False, output="", error=f"Execution failed: {exc}")
