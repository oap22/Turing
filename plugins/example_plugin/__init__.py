"""Example plugin demonstrating the Turing plugin system.

This plugin provides a simple "hello" tool that greets users.
"""

from __future__ import annotations

from typing import Any

from turing.plugins.base import Plugin
from turing.tools.base import RiskLevel, Tool, ToolResult


class HelloTool(Tool):
    """A simple greeting tool provided by the example plugin."""

    @property
    def name(self) -> str:
        return "hello"

    @property
    def description(self) -> str:
        return "Say hello to someone or get a friendly greeting"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the person to greet (default: World)",
                },
            },
            "required": [],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name", "World")
        greeting = f"Hello, {name}! I'm Turing, your friendly AI assistant."
        return ToolResult(success=True, output=greeting)


class ExamplePlugin(Plugin):
    """Example plugin demonstrating the plugin system."""

    @property
    def name(self) -> str:
        return "example"

    @property
    def description(self) -> str:
        return "Example plugin demonstrating the plugin system"

    @property
    def version(self) -> str:
        return "0.1.0"

    async def setup(self, config: Any) -> None:
        """Initialize the example plugin."""
        self._hello_tool = HelloTool()

    async def teardown(self) -> None:
        """Clean up the example plugin."""
        pass

    def get_tools(self) -> list[Tool]:
        """Return the hello tool."""
        return [HelloTool()]

    def get_system_prompt_addition(self) -> str:
        """Add a note about the hello tool to the system prompt."""
        return "You have access to the 'hello' tool from the example plugin for greeting users."
