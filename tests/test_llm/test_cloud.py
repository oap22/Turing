"""Tests for the Anthropic Claude cloud LLM provider."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from turing.llm.base import Message, Role, ToolCall, ToolDefinition
from turing.llm.cloud import ClaudeProvider

# ── helpers ──────────────────────────────────────────────────────────


def _make_response(
    text: str = "hi",
    *,
    tool_use: list[dict] | None = None,
    model: str = "claude-sonnet-4-20250514",
    stop_reason: str = "end_turn",
    usage: dict | None = None,
) -> SimpleNamespace:
    """Build a mock anthropic response object."""
    blocks: list[SimpleNamespace] = []
    if text:
        blocks.append(SimpleNamespace(type="text", text=text))
    for tu in tool_use or []:
        blocks.append(
            SimpleNamespace(
                type="tool_use",
                id=tu["id"],
                name=tu["name"],
                input=tu.get("input", {}),
            )
        )
    usage_obj: SimpleNamespace | None
    if usage is None:
        usage_obj = SimpleNamespace(input_tokens=10, output_tokens=4)
    elif usage == "none":
        usage_obj = None
    else:
        usage_obj = SimpleNamespace(**usage)
    return SimpleNamespace(
        content=blocks,
        model=model,
        usage=usage_obj,
        stop_reason=stop_reason,
    )


def _rate_limit_error() -> anthropic.RateLimitError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError(
        "rate limited",
        response=httpx.Response(429, request=req),
        body=None,
    )


def _connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


def _api_status_error(status: int = 401) -> anthropic.APIStatusError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "bad",
        response=httpx.Response(status, request=req),
        body=None,
    )


@pytest.fixture
def provider() -> ClaudeProvider:
    """ClaudeProvider whose anthropic client is fully mocked."""
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock()
        mock_client.messages.stream = MagicMock()
        mock_cls.return_value = mock_client
        p = ClaudeProvider(api_key="sk-test", model="claude-sonnet-4-20250514")
    p._client = mock_client  # type: ignore[assignment]
    return p


# ── request shaping ──────────────────────────────────────────────────


class TestRequestShaping:
    @pytest.mark.asyncio
    async def test_basic_request_shape(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        await provider.complete(
            [Message(role=Role.USER, content="hi")],
            max_tokens=128,
            temperature=0.5,
        )
        kw = provider._client.messages.create.call_args.kwargs
        assert kw["model"] == "claude-sonnet-4-20250514"
        assert kw["max_tokens"] == 128
        assert kw["temperature"] == 0.5
        assert kw["messages"] == [{"role": "user", "content": "hi"}]
        assert "system" not in kw
        assert "tools" not in kw

    @pytest.mark.asyncio
    async def test_system_prompt_added_when_provided(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        await provider.complete([Message(role=Role.USER, content="hi")], system="You are kind.")
        kw = provider._client.messages.create.call_args.kwargs
        assert kw["system"] == "You are kind."

    @pytest.mark.asyncio
    async def test_tools_converted_to_input_schema(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        tool = ToolDefinition(
            name="run_command",
            description="run a shell command",
            parameters={"type": "object", "properties": {"cmd": {"type": "string"}}},
        )
        await provider.complete([Message(role=Role.USER, content="hi")], tools=[tool])
        kw = provider._client.messages.create.call_args.kwargs
        assert kw["tools"] == [
            {
                "name": "run_command",
                "description": "run a shell command",
                "input_schema": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                },
            }
        ]

    @pytest.mark.asyncio
    async def test_system_role_messages_stripped_from_messages_list(
        self, provider: ClaudeProvider
    ) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        await provider.complete(
            [
                Message(role=Role.SYSTEM, content="ignored"),
                Message(role=Role.USER, content="hi"),
            ]
        )
        kw = provider._client.messages.create.call_args.kwargs
        # System message should NOT appear in messages list (passed via `system` kw).
        roles = [m["role"] for m in kw["messages"]]
        assert "system" not in roles

    @pytest.mark.asyncio
    async def test_tool_result_message_uses_tool_result_block(
        self, provider: ClaudeProvider
    ) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        await provider.complete(
            [
                Message(role=Role.USER, content="hi"),
                Message(role=Role.TOOL, content="result", tool_call_id="call_1"),
            ]
        )
        kw = provider._client.messages.create.call_args.kwargs
        tool_msg = kw["messages"][1]
        assert tool_msg["role"] == "user"
        assert tool_msg["content"][0]["type"] == "tool_result"
        assert tool_msg["content"][0]["tool_use_id"] == "call_1"
        assert tool_msg["content"][0]["content"] == "result"

    @pytest.mark.asyncio
    async def test_parallel_tool_results_coalesce_into_one_user_turn(
        self, provider: ClaudeProvider
    ) -> None:
        """All results answering one assistant turn ride in a single message.

        The API expects every tool_result for a parallel tool call in one user
        turn; splitting them across turns teaches the model to stop issuing
        parallel calls.
        """
        calls = [ToolCall(id=f"call_{i}", name="t", arguments={}) for i in range(3)]
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        await provider.complete(
            [
                Message(role=Role.USER, content="run three"),
                Message(role=Role.ASSISTANT, content="", tool_calls=calls),
                *[Message(role=Role.TOOL, content="ok", tool_call_id=c.id) for c in calls],
                Message(role=Role.USER, content="follow-up"),
            ]
        )
        msgs = provider._client.messages.create.call_args.kwargs["messages"]

        results = [m for m in msgs if isinstance(m["content"], list) and m["content"]]
        tool_turns = [m for m in results if m["content"][0].get("type") == "tool_result"]
        assert len(tool_turns) == 1, "the three results belong to one user turn"
        assert [b["tool_use_id"] for b in tool_turns[0]["content"]] == [c.id for c in calls]

        # One tool_result per tool_use, and the plain follow-up stays separate.
        assistant = next(m for m in msgs if m["role"] == "assistant")
        assert sum(1 for b in assistant["content"] if b["type"] == "tool_use") == 3
        assert msgs[-1]["content"] == "follow-up"

    @pytest.mark.asyncio
    async def test_tool_result_missing_id_defaults_to_empty(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        await provider.complete([Message(role=Role.TOOL, content="r", tool_call_id=None)])
        kw = provider._client.messages.create.call_args.kwargs
        assert kw["messages"][0]["content"][0]["tool_use_id"] == ""

    @pytest.mark.asyncio
    async def test_assistant_with_tool_calls_serialised_as_blocks(
        self, provider: ClaudeProvider
    ) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        msg = Message(
            role=Role.ASSISTANT,
            content="reasoning",
            tool_calls=[ToolCall(id="call_1", name="t", arguments={"a": 1})],
        )
        await provider.complete([msg])
        kw = provider._client.messages.create.call_args.kwargs
        blocks = kw["messages"][0]["content"]
        assert {"type": "text", "text": "reasoning"} in blocks
        assert any(
            b["type"] == "tool_use" and b["id"] == "call_1" and b["name"] == "t" for b in blocks
        )

    @pytest.mark.asyncio
    async def test_assistant_with_tool_calls_and_no_text(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        msg = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c", name="t", arguments={})],
        )
        await provider.complete([msg])
        kw = provider._client.messages.create.call_args.kwargs
        blocks = kw["messages"][0]["content"]
        # No text block should be emitted for empty content
        assert all(b["type"] != "text" for b in blocks)


# ── response parsing ─────────────────────────────────────────────────


class TestResponseParsing:
    @pytest.mark.asyncio
    async def test_text_response_parsed(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(
            return_value=_make_response("hello world", model="claude-x")
        )
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.content == "hello world"
        assert resp.model == "claude-x"
        assert resp.stop_reason == "end_turn"
        assert resp.usage == {"input_tokens": 10, "output_tokens": 4}
        assert resp.tool_calls == []

    @pytest.mark.asyncio
    async def test_multi_text_block_joined_with_newlines(self, provider: ClaudeProvider) -> None:
        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="a"),
                SimpleNamespace(type="text", text="b"),
            ],
            model="m",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
        )
        provider._client.messages.create = AsyncMock(return_value=response)
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.content == "a\nb"

    @pytest.mark.asyncio
    async def test_tool_use_block_parsed_to_tool_call(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(
            return_value=_make_response(
                "",
                tool_use=[{"id": "tu_1", "name": "fn", "input": {"x": 1}}],
            )
        )
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "tu_1"
        assert resp.tool_calls[0].name == "fn"
        assert resp.tool_calls[0].arguments == {"x": 1}

    @pytest.mark.asyncio
    async def test_tool_use_with_non_dict_input_defaults_to_empty(
        self, provider: ClaudeProvider
    ) -> None:
        response = SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", id="x", name="n", input="bad")],
            model="m",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            stop_reason="tool_use",
        )
        provider._client.messages.create = AsyncMock(return_value=response)
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.tool_calls[0].arguments == {}

    @pytest.mark.asyncio
    async def test_missing_usage_yields_empty_dict(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response(usage="none"))
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.usage == {}

    @pytest.mark.asyncio
    async def test_none_stop_reason_becomes_empty_string(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(
            return_value=_make_response(stop_reason=None)  # type: ignore[arg-type]
        )
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.stop_reason == ""


# ── retry / backoff / error mapping ──────────────────────────────────


class TestRetryAndErrors:
    @pytest.mark.asyncio
    async def test_rate_limit_retries_then_succeeds(
        self, provider: ClaudeProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []

        async def fake_sleep(d: float) -> None:
            sleeps.append(d)

        monkeypatch.setattr("turing.llm.cloud.asyncio.sleep", fake_sleep)
        ok = _make_response("recovered")
        provider._client.messages.create = AsyncMock(
            side_effect=[_rate_limit_error(), _rate_limit_error(), ok]
        )
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.content == "recovered"
        # Exponential backoff: 1.0, 2.0
        assert sleeps == [1.0, 2.0]
        assert provider._client.messages.create.await_count == 3

    @pytest.mark.asyncio
    async def test_connection_error_retries_then_raises_after_max(
        self, provider: ClaudeProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []

        async def fake_sleep(d: float) -> None:
            sleeps.append(d)

        monkeypatch.setattr("turing.llm.cloud.asyncio.sleep", fake_sleep)
        provider._client.messages.create = AsyncMock(side_effect=_connection_error())
        with pytest.raises(anthropic.APIConnectionError):
            await provider.complete([Message(role=Role.USER, content="hi")])
        assert provider._client.messages.create.await_count == 3
        # Back-off spaces out the *next* attempt, so the final failure raises
        # immediately instead of waiting out a delay nothing follows.
        assert sleeps == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_rate_limit_exhausted_does_not_sleep_after_last_attempt(
        self, provider: ClaudeProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []

        async def fake_sleep(d: float) -> None:
            sleeps.append(d)

        monkeypatch.setattr("turing.llm.cloud.asyncio.sleep", fake_sleep)
        provider._client.messages.create = AsyncMock(side_effect=_rate_limit_error())
        with pytest.raises(anthropic.RateLimitError):
            await provider.complete([Message(role=Role.USER, content="hi")])
        assert provider._client.messages.create.await_count == 3
        assert sleeps == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_api_status_error_not_retried(
        self, provider: ClaudeProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_sleep(_d: float) -> None:
            pass

        monkeypatch.setattr("turing.llm.cloud.asyncio.sleep", fake_sleep)
        provider._client.messages.create = AsyncMock(side_effect=_api_status_error(status=401))
        with pytest.raises(anthropic.APIStatusError):
            await provider.complete([Message(role=Role.USER, content="hi")])
        # Non-retryable: only one call
        assert provider._client.messages.create.await_count == 1

    @pytest.mark.asyncio
    async def test_timeout_propagates(
        self, provider: ClaudeProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_sleep(_d: float) -> None:
            pass

        monkeypatch.setattr("turing.llm.cloud.asyncio.sleep", fake_sleep)
        # Plain TimeoutError isn't one of the caught anthropic types; it should bubble.
        provider._client.messages.create = AsyncMock(side_effect=TimeoutError("slow"))
        with pytest.raises(TimeoutError):
            await provider.complete([Message(role=Role.USER, content="hi")])


# ── prompt-cache header behaviour ────────────────────────────────────


class TestPromptCacheBehaviour:
    """Document current prompt-cache behaviour.

    The provider does not currently set `cache_control` blocks or
    cache-related headers. These tests pin that contract so that any
    future change to enable prompt caching trips a deliberate edit.
    """

    @pytest.mark.asyncio
    async def test_no_cache_control_in_messages(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        await provider.complete([Message(role=Role.USER, content="hi")], system="sys")
        kw = provider._client.messages.create.call_args.kwargs
        # No cache_control attached to system or messages today.
        assert "cache_control" not in str(kw.get("system", ""))
        for m in kw["messages"]:
            assert "cache_control" not in str(m)

    @pytest.mark.asyncio
    async def test_no_extra_headers_for_caching(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        await provider.complete([Message(role=Role.USER, content="hi")])
        kw = provider._client.messages.create.call_args.kwargs
        assert "extra_headers" not in kw
        assert "headers" not in kw

    @pytest.mark.asyncio
    async def test_cache_usage_fields_do_not_break_parsing(self, provider: ClaudeProvider) -> None:
        """If the API returns cache_creation/read tokens they are ignored gracefully."""
        usage = SimpleNamespace(
            input_tokens=10,
            output_tokens=4,
            cache_creation_input_tokens=100,
            cache_read_input_tokens=200,
        )
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            model="m",
            usage=usage,
            stop_reason="end_turn",
        )
        provider._client.messages.create = AsyncMock(return_value=response)
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        # Only the canonical input/output fields are surfaced.
        assert resp.usage == {"input_tokens": 10, "output_tokens": 4}


# ── stream ───────────────────────────────────────────────────────────


class TestStream:
    @pytest.mark.asyncio
    async def test_stream_yields_text_deltas(self, provider: ClaudeProvider) -> None:
        class FakeStream:
            def __init__(self) -> None:
                self.text_stream = self._gen()

            async def _gen(self):  # type: ignore[no-untyped-def]
                for t in ["a", "b", "c"]:
                    yield t

            async def __aenter__(self) -> FakeStream:
                return self

            async def __aexit__(self, *a: object) -> None:
                return None

        provider._client.messages.stream = MagicMock(return_value=FakeStream())
        out: list[str] = []
        async for piece in provider.stream([Message(role=Role.USER, content="hi")], system="s"):
            out.append(piece)
        assert out == ["a", "b", "c"]
        kw = provider._client.messages.stream.call_args.kwargs
        assert kw["system"] == "s"


# ── health check ─────────────────────────────────────────────────────


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_ok(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(return_value=_make_response())
        assert await provider.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_failure_returns_false(self, provider: ClaudeProvider) -> None:
        provider._client.messages.create = AsyncMock(side_effect=_api_status_error(500))
        assert await provider.health_check() is False
