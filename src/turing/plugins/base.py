"""Plugin interface for extending the Turing agent with additional capabilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Plugin(ABC):
    """Base class for all Turing plugins.

    Plugins can provide additional tools, system prompt additions, and
    custom setup/teardown logic.  They are loaded dynamically from the
    plugins directory or registered programmatically.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this plugin."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this plugin does."""
        ...

    @property
    def version(self) -> str:
        """Semantic version string for this plugin."""
        return "0.1.0"

    async def setup(self, config: Any) -> None:  # noqa: B027  # optional hook, not abstract
        """Called when the plugin is loaded.

        Override this method to perform initialization (e.g., connecting
        to external services, reading configuration, etc.).
        """
        ...

    async def teardown(self) -> None:  # noqa: B027  # optional hook, not abstract
        """Called when the plugin is unloaded.

        Override this method to perform cleanup (e.g., closing connections,
        flushing buffers, etc.).
        """
        ...

    def get_tools(self) -> list[Any]:
        """Return Tool instances provided by this plugin.

        Each returned object must be a ``turing.tools.base.Tool`` subclass
        instance.
        """
        return []

    def get_system_prompt_addition(self) -> str:
        """Return text to append to the agent's system prompt.

        This allows plugins to inject context about their capabilities
        into the LLM conversation.
        """
        return ""
