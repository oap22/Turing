"""Tests for the LLM router."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from turing.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    Role,
    ToolDefinition,
)
from turing.llm.classifier import ComplexityClassifier
from turing.llm.router import LLMRouter, warn_if_local_only_disables_tools
from turing.telemetry.bus import Telemetry, TelemetryEvent

# ── fixtures ──────────────────────────────────────────────────────────


def _make_response(content: str, model: str = "test-model") -> LLMResponse:
    return LLMResponse(content=content, model=model)


def _user_msg(text: str) -> Message:
    return Message(role=Role.USER, content=text)


@pytest.fixture
def cloud_provider() -> AsyncMock:
    provider = AsyncMock(spec=LLMProvider)
    provider.complete = AsyncMock(return_value=_make_response("cloud response", "claude"))
    return provider


@pytest.fixture
def local_provider() -> AsyncMock:
    provider = AsyncMock(spec=LLMProvider)
    provider.complete = AsyncMock(return_value=_make_response("local response", "gemma"))
    return provider


@pytest.fixture
def classifier() -> ComplexityClassifier:
    return ComplexityClassifier()


# ── auto routing ─────────────────────────────────────────────────────


class TestAutoRouting:
    """Test automatic routing based on message complexity."""

    @pytest.mark.asyncio
    async def test_simple_message_routes_to_local(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        classifier: ComplexityClassifier,
    ) -> None:
        router = LLMRouter(cloud_provider, local_provider, classifier, "auto")
        response = await router.route([_user_msg("hello")])
        assert response.content == "local response"
        local_provider.complete.assert_awaited_once()
        cloud_provider.complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_complex_message_routes_to_cloud(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        classifier: ComplexityClassifier,
    ) -> None:
        router = LLMRouter(cloud_provider, local_provider, classifier, "auto")
        response = await router.route(
            [_user_msg("Analyze the memory usage patterns and explain the bottleneck")]
        )
        assert response.content == "cloud response"
        cloud_provider.complete.assert_awaited_once()
        local_provider.complete.assert_not_awaited()


# ── fallback ─────────────────────────────────────────────────────────


class TestFallback:
    """Test fallback from local to cloud when local fails."""

    @pytest.mark.asyncio
    async def test_local_failure_falls_back_to_cloud(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        classifier: ComplexityClassifier,
    ) -> None:
        local_provider.complete.side_effect = ConnectionError("Ollama offline")
        router = LLMRouter(cloud_provider, local_provider, classifier, "auto")
        response = await router.route([_user_msg("hello")])
        assert response.content == "cloud response"
        local_provider.complete.assert_awaited_once()
        cloud_provider.complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_local_timeout_falls_back_to_cloud(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        classifier: ComplexityClassifier,
    ) -> None:
        local_provider.complete.side_effect = TimeoutError("Ollama timed out")
        router = LLMRouter(cloud_provider, local_provider, classifier, "auto")
        response = await router.route([_user_msg("hi")])
        assert response.content == "cloud response"

    @pytest.mark.asyncio
    async def test_cloud_failure_in_auto_mode_raises(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        classifier: ComplexityClassifier,
    ) -> None:
        """When cloud is selected (complex msg) and fails, the error propagates."""
        cloud_provider.complete.side_effect = RuntimeError("API error")
        router = LLMRouter(cloud_provider, local_provider, classifier, "auto")
        with pytest.raises(RuntimeError, match="API error"):
            await router.route([_user_msg("Analyze and explain the entire system architecture")])


# ── explicit modes ───────────────────────────────────────────────────


class TestExplicitModes:
    """Test cloud_only and local_only routing modes."""

    @pytest.mark.asyncio
    async def test_cloud_only_always_uses_cloud(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
    ) -> None:
        router = LLMRouter(cloud_provider, local_provider, routing_mode="cloud_only")
        response = await router.route([_user_msg("hello")])
        assert response.content == "cloud response"
        cloud_provider.complete.assert_awaited_once()
        local_provider.complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_local_only_always_uses_local(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
    ) -> None:
        router = LLMRouter(cloud_provider, local_provider, routing_mode="local_only")
        response = await router.route([_user_msg("Analyze this complex problem in detail")])
        assert response.content == "local response"
        local_provider.complete.assert_awaited_once()
        cloud_provider.complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_local_only_does_not_fallback(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
    ) -> None:
        local_provider.complete.side_effect = ConnectionError("Ollama offline")
        router = LLMRouter(cloud_provider, local_provider, routing_mode="local_only")
        with pytest.raises(ConnectionError):
            await router.route([_user_msg("hello")])
        cloud_provider.complete.assert_not_awaited()


# ── tool forcing ─────────────────────────────────────────────────────


class TestToolForcing:
    """When tools are provided, cloud should always be selected in auto mode."""

    @pytest.mark.asyncio
    async def test_tools_force_cloud_in_auto_mode(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        classifier: ComplexityClassifier,
    ) -> None:
        tools = [
            ToolDefinition(
                name="run_command",
                description="Run a shell command",
                parameters={
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                },
            )
        ]
        router = LLMRouter(cloud_provider, local_provider, classifier, "auto")
        response = await router.route([_user_msg("hello")], tools=tools)
        assert response.content == "cloud response"
        cloud_provider.complete.assert_awaited_once()
        local_provider.complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tools_passed_through_to_provider(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
    ) -> None:
        tool = ToolDefinition(
            name="disk_usage",
            description="Check disk usage",
            parameters={"type": "object", "properties": {}},
        )
        router = LLMRouter(cloud_provider, local_provider, routing_mode="cloud_only")
        await router.route([_user_msg("check disk")], tools=[tool])
        call_kwargs = cloud_provider.complete.call_args
        assert call_kwargs.kwargs.get("tools") == [tool] or call_kwargs[1].get("tools") == [tool]


# ── misc ──────────────────────────────────────────────────────────────


class TestMisc:
    @pytest.mark.asyncio
    async def test_system_prompt_forwarded(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
    ) -> None:
        router = LLMRouter(cloud_provider, local_provider, routing_mode="cloud_only")
        await router.route([_user_msg("hi")], system="You are helpful.")
        call_kwargs = cloud_provider.complete.call_args
        assert "You are helpful." in (
            call_kwargs.kwargs.get("system", ""),
            call_kwargs[1].get("system", ""),
        )

    @pytest.mark.asyncio
    async def test_empty_messages_still_works(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        classifier: ComplexityClassifier,
    ) -> None:
        router = LLMRouter(cloud_provider, local_provider, classifier, "auto")
        # No user message means classifier gets empty string -> SIMPLE -> local
        response = await router.route([])
        assert response.content == "local response"


# ── telemetry ─────────────────────────────────────────────────────────


class TestTelemetry:
    """The router emits telemetry events for each LLM provider call."""

    @pytest.mark.asyncio
    async def test_emits_start_and_end_for_successful_call(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh = Telemetry()
        monkeypatch.setattr("turing.telemetry.bus._SINGLETON", fresh)
        captured: list[TelemetryEvent] = []
        fresh.add_sink(captured.append)

        router = LLMRouter(cloud_provider, local_provider, routing_mode="cloud_only")
        await router.route([_user_msg("hi there")])

        names = [e.name for e in captured]
        assert "llm.complete.start" in names
        assert "llm.complete.end" in names

    @pytest.mark.asyncio
    async def test_event_payload_includes_provider_and_model(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh = Telemetry()
        monkeypatch.setattr("turing.telemetry.bus._SINGLETON", fresh)
        captured: list[TelemetryEvent] = []
        fresh.add_sink(captured.append)

        router = LLMRouter(cloud_provider, local_provider, routing_mode="cloud_only")
        await router.route([_user_msg("hi")])

        end = next(e for e in captured if e.name == "llm.complete.end")
        assert end.payload.get("provider") == "cloud"
        assert "model" in end.payload

    @pytest.mark.asyncio
    async def test_event_payload_redacts_prompt_and_response(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh = Telemetry()
        monkeypatch.setattr("turing.telemetry.bus._SINGLETON", fresh)
        captured: list[TelemetryEvent] = []
        fresh.add_sink(captured.append)

        cloud_provider.complete = AsyncMock(
            return_value=_make_response("hi alice@example.com", "claude")
        )
        router = LLMRouter(cloud_provider, local_provider, routing_mode="cloud_only")
        await router.route([_user_msg("ping bob@example.org")])

        start = next(e for e in captured if e.name == "llm.complete.start")
        end = next(e for e in captured if e.name == "llm.complete.end")
        assert "bob@example.org" not in start.payload.get("prompt_sample", "")
        assert "alice@example.com" not in end.payload.get("response_sample", "")

    @pytest.mark.asyncio
    async def test_emits_error_event_on_provider_failure(
        self,
        cloud_provider: AsyncMock,
        local_provider: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fresh = Telemetry()
        monkeypatch.setattr("turing.telemetry.bus._SINGLETON", fresh)
        captured: list[TelemetryEvent] = []
        fresh.add_sink(captured.append)

        cloud_provider.complete.side_effect = RuntimeError("API blew up")
        router = LLMRouter(cloud_provider, local_provider, routing_mode="cloud_only")
        with pytest.raises(RuntimeError, match="API blew up"):
            await router.route([_user_msg("hi")])

        err = next(e for e in captured if e.name == "llm.complete.error")
        assert err.error == "RuntimeError"


class TestWarnIfLocalOnlyDisablesTools:
    """Issue #158 — local_only routing silently disables tool use."""

    def test_warns_when_local_only_with_tools(self) -> None:
        import structlog

        with structlog.testing.capture_logs() as cap:
            emitted = warn_if_local_only_disables_tools("local_only", tool_count=5)
        assert emitted is True
        warns = [e for e in cap if e.get("event") == "llm.local_only_disables_tools"]
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"
        assert warns[0]["tool_count"] == 5

    def test_silent_when_local_only_but_no_tools(self) -> None:
        import structlog

        with structlog.testing.capture_logs() as cap:
            emitted = warn_if_local_only_disables_tools("local_only", tool_count=0)
        assert emitted is False
        assert not [e for e in cap if e.get("event") == "llm.local_only_disables_tools"]

    def test_silent_when_auto_with_tools(self) -> None:
        import structlog

        with structlog.testing.capture_logs() as cap:
            emitted = warn_if_local_only_disables_tools("auto", tool_count=5)
        assert emitted is False
        assert not [e for e in cap if e.get("event") == "llm.local_only_disables_tools"]

    def test_silent_when_cloud_only_with_tools(self) -> None:
        import structlog

        with structlog.testing.capture_logs() as cap:
            emitted = warn_if_local_only_disables_tools("cloud_only", tool_count=5)
        assert emitted is False
        assert not [e for e in cap if e.get("event") == "llm.local_only_disables_tools"]
