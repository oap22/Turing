"""Regression test for #67: tool with a `name` parameter must dispatch."""

from __future__ import annotations

from typing import Any

import pytest

from turing.tools.base import RiskLevel, Tool, ToolRegistry, ToolResult


class _GreeterTool(Tool):
    """Stand-in for the example plugin's hello tool — its sole parameter is `name`."""

    @property
    def name(self) -> str:
        return "hello"

    @property
    def description(self) -> str:
        return "say hi"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> ToolResult:
        target = kwargs.get("name", "world")
        return ToolResult(success=True, output=f"hi {target}")


@pytest.mark.asyncio
async def test_dispatching_a_tool_whose_parameter_is_named_name() -> None:
    registry = ToolRegistry()
    registry.register(_GreeterTool())
    # Pre-fix this raised
    #   TypeError: ToolRegistry.execute() got multiple values for argument 'name'
    result = await registry.execute("hello", name="gang")
    assert result.success
    assert result.output == "hi gang"


@pytest.mark.asyncio
async def test_dispatching_with_arguments_dict_unpacking() -> None:
    """The executor calls ``await registry.execute(tool_name, **arguments)``."""
    registry = ToolRegistry()
    registry.register(_GreeterTool())
    arguments: dict[str, Any] = {"name": "fleet"}
    result = await registry.execute("hello", **arguments)
    assert result.success
    assert "fleet" in result.output
