"""Plugin registry for managing loaded plugins and their tools."""

from __future__ import annotations

from typing import Any

import structlog

from turing.plugins.base import Plugin
from turing.tools.base import Tool

logger = structlog.get_logger("turing.plugins.registry")


class PluginRegistry:
    """Central registry for all loaded plugins.

    Provides methods to register, unregister, and query plugins.  Also
    aggregates all tools provided by registered plugins.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        """Register a plugin.

        Raises ValueError if a plugin with the same name is already registered.
        """
        if plugin.name in self._plugins:
            raise ValueError(f"Plugin '{plugin.name}' is already registered")
        self._plugins[plugin.name] = plugin
        logger.info(
            "plugin_registered",
            name=plugin.name,
            version=plugin.version,
            description=plugin.description,
        )

    def unregister(self, name: str) -> Plugin | None:
        """Unregister a plugin by name.

        Returns the removed Plugin instance, or None if not found.
        """
        plugin = self._plugins.pop(name, None)
        if plugin is not None:
            logger.info("plugin_unregistered", name=name)
        return plugin

    def get(self, name: str) -> Plugin | None:
        """Get a plugin by name."""
        return self._plugins.get(name)

    def get_all(self) -> list[Plugin]:
        """Return all registered plugins."""
        return list(self._plugins.values())

    def get_all_tools(self) -> list[Tool]:
        """Collect and return all tools from all registered plugins."""
        tools: list[Tool] = []
        for plugin in self._plugins.values():
            plugin_tools = plugin.get_tools()
            for tool in plugin_tools:
                if isinstance(tool, Tool):
                    tools.append(tool)
                else:
                    logger.warning(
                        "invalid_plugin_tool",
                        plugin=plugin.name,
                        tool_type=type(tool).__name__,
                    )
        return tools

    def get_system_prompt_additions(self) -> str:
        """Concatenate system prompt additions from all plugins."""
        additions: list[str] = []
        for plugin in self._plugins.values():
            addition = plugin.get_system_prompt_addition()
            if addition:
                additions.append(addition)
        return "\n\n".join(additions)

    @property
    def plugin_count(self) -> int:
        """Return the number of registered plugins."""
        return len(self._plugins)

    def list_plugins(self) -> list[dict[str, Any]]:
        """Return summary information for all registered plugins."""
        return [
            {
                "name": p.name,
                "version": p.version,
                "description": p.description,
                "tools": [t.name for t in p.get_tools() if isinstance(t, Tool)],
            }
            for p in self._plugins.values()
        ]
