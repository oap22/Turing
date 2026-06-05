"""Tests for :class:`turing.plugins.registry.PluginRegistry`.

The registry aggregates loaded plugins and the tools they expose. These tests
drive register/unregister/lookup, duplicate + missing-plugin error paths, and
the tool/system-prompt aggregation surface using small fake plugins — no real
plugin loading (that path is covered by ``test_loader.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from turing.plugins.base import Plugin
from turing.plugins.registry import PluginRegistry
from turing.tools.base import Tool, ToolResult


class _FakeTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"fake tool {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, output="ok")


class _FakePlugin(Plugin):
    def __init__(
        self,
        name: str,
        *,
        tools: list[Any] | None = None,
        prompt_addition: str = "",
        version: str = "0.1.0",
    ) -> None:
        self._name = name
        self._tools = tools if tools is not None else []
        self._prompt_addition = prompt_addition
        self._version = version

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"fake plugin {self._name}"

    @property
    def version(self) -> str:
        return self._version

    def get_tools(self) -> list[Any]:
        return self._tools

    def get_system_prompt_addition(self) -> str:
        return self._prompt_addition


class TestRegister:
    def test_register_then_get_returns_same_instance(self) -> None:
        registry = PluginRegistry()
        plugin = _FakePlugin("weather")
        registry.register(plugin)
        assert registry.get("weather") is plugin

    def test_register_increments_plugin_count(self) -> None:
        registry = PluginRegistry()
        assert registry.plugin_count == 0
        registry.register(_FakePlugin("a"))
        registry.register(_FakePlugin("b"))
        assert registry.plugin_count == 2

    def test_duplicate_registration_raises_value_error(self) -> None:
        registry = PluginRegistry()
        registry.register(_FakePlugin("dup"))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(_FakePlugin("dup"))


class TestUnregister:
    def test_unregister_returns_removed_plugin(self) -> None:
        registry = PluginRegistry()
        plugin = _FakePlugin("temp")
        registry.register(plugin)
        removed = registry.unregister("temp")
        assert removed is plugin
        assert registry.get("temp") is None
        assert registry.plugin_count == 0

    def test_unregister_missing_returns_none(self) -> None:
        registry = PluginRegistry()
        assert registry.unregister("nope") is None


class TestLookup:
    def test_get_missing_returns_none(self) -> None:
        registry = PluginRegistry()
        assert registry.get("missing") is None

    def test_get_all_returns_every_registered_plugin(self) -> None:
        registry = PluginRegistry()
        a, b = _FakePlugin("a"), _FakePlugin("b")
        registry.register(a)
        registry.register(b)
        assert set(registry.get_all()) == {a, b}


class TestToolAggregation:
    def test_get_all_tools_collects_across_plugins(self) -> None:
        registry = PluginRegistry()
        registry.register(_FakePlugin("p1", tools=[_FakeTool("t1")]))
        registry.register(_FakePlugin("p2", tools=[_FakeTool("t2"), _FakeTool("t3")]))
        names = {t.name for t in registry.get_all_tools()}
        assert names == {"t1", "t2", "t3"}

    def test_get_all_tools_skips_non_tool_objects(self) -> None:
        registry = PluginRegistry()
        # A plugin that returns a bogus, non-Tool object must be skipped, not crash.
        registry.register(_FakePlugin("bad", tools=[_FakeTool("good"), "not-a-tool"]))
        tools = registry.get_all_tools()
        assert [t.name for t in tools] == ["good"]

    def test_get_all_tools_empty_when_no_plugins(self) -> None:
        assert PluginRegistry().get_all_tools() == []


class TestSystemPromptAdditions:
    def test_concatenates_non_empty_additions(self) -> None:
        registry = PluginRegistry()
        registry.register(_FakePlugin("p1", prompt_addition="alpha"))
        registry.register(_FakePlugin("p2", prompt_addition=""))
        registry.register(_FakePlugin("p3", prompt_addition="beta"))
        assert registry.get_system_prompt_additions() == "alpha\n\nbeta"

    def test_empty_string_when_no_additions(self) -> None:
        registry = PluginRegistry()
        registry.register(_FakePlugin("p1"))
        assert registry.get_system_prompt_additions() == ""


class TestListPlugins:
    def test_summary_includes_metadata_and_tool_names(self) -> None:
        registry = PluginRegistry()
        registry.register(_FakePlugin("weather", tools=[_FakeTool("forecast")], version="2.0.0"))
        summary = registry.list_plugins()
        assert summary == [
            {
                "name": "weather",
                "version": "2.0.0",
                "description": "fake plugin weather",
                "tools": ["forecast"],
            }
        ]

    def test_summary_filters_non_tool_objects(self) -> None:
        registry = PluginRegistry()
        registry.register(_FakePlugin("p", tools=[_FakeTool("real"), 123]))
        assert registry.list_plugins()[0]["tools"] == ["real"]

    def test_empty_registry_lists_nothing(self) -> None:
        assert PluginRegistry().list_plugins() == []
