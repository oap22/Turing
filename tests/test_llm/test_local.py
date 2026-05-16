"""Tests for the Ollama local LLM provider."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turing.llm.base import Message, Role, ToolCall, ToolDefinition
from turing.llm.local import OllamaProvider


@pytest.fixture
def provider() -> OllamaProvider:
    """OllamaProvider with a mocked async ollama client."""
    with patch("ollama.AsyncClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.chat = AsyncMock()
        mock_client.list = AsyncMock()
        mock_cls.return_value = mock_client
        p = OllamaProvider(host="http://test:11434", model="gemma3:1b")
    # expose for tests
    p._client = mock_client  # type: ignore[assignment]
    return p


# ── request shaping ──────────────────────────────────────────────────


class TestRequestShaping:
    @pytest.mark.asyncio
    async def test_complete_sends_model_and_options(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(
            return_value={"message": {"content": "hi"}, "model": "gemma3:1b"}
        )
        await provider.complete(
            [Message(role=Role.USER, content="hello")],
            max_tokens=256,
            temperature=0.3,
        )
        kwargs = provider._client.chat.call_args.kwargs
        assert kwargs["model"] == "gemma3:1b"
        assert kwargs["options"]["num_predict"] == 256
        assert kwargs["options"]["temperature"] == 0.3
        assert "tools" not in kwargs

    @pytest.mark.asyncio
    async def test_system_prompt_prepended(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(return_value={"message": {"content": ""}})
        await provider.complete(
            [Message(role=Role.USER, content="hi")],
            system="You are helpful.",
        )
        msgs = provider._client.chat.call_args.kwargs["messages"]
        assert msgs[0] == {"role": "system", "content": "You are helpful."}
        assert msgs[1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_tools_passed_when_provided(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(return_value={"message": {"content": ""}})
        tool = ToolDefinition(
            name="disk_usage",
            description="Check disk",
            parameters={"type": "object", "properties": {}},
        )
        await provider.complete([Message(role=Role.USER, content="hi")], tools=[tool])
        kwargs = provider._client.chat.call_args.kwargs
        assert kwargs["tools"][0]["type"] == "function"
        assert kwargs["tools"][0]["function"]["name"] == "disk_usage"

    @pytest.mark.asyncio
    async def test_assistant_with_tool_calls_serialised(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(return_value={"message": {"content": ""}})
        msgs = [
            Message(
                role=Role.ASSISTANT,
                content="calling",
                tool_calls=[ToolCall(id="x", name="t", arguments={"a": 1})],
            ),
            Message(role=Role.TOOL, content="result", tool_call_id="x"),
        ]
        await provider.complete(msgs)
        sent = provider._client.chat.call_args.kwargs["messages"]
        assert sent[0]["role"] == "assistant"
        assert sent[0]["tool_calls"][0]["function"]["name"] == "t"
        assert sent[1] == {"role": "tool", "content": "result"}

    @pytest.mark.asyncio
    async def test_system_role_message_passed_through(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(return_value={"message": {"content": ""}})
        await provider.complete(
            [
                Message(role=Role.SYSTEM, content="sys-msg"),
                Message(role=Role.USER, content="hi"),
            ]
        )
        sent = provider._client.chat.call_args.kwargs["messages"]
        assert {"role": "system", "content": "sys-msg"} in sent


# ── response parsing ─────────────────────────────────────────────────


class TestResponseParsing:
    @pytest.mark.asyncio
    async def test_plain_text_response(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(
            return_value={
                "message": {"content": "hello there"},
                "model": "gemma3:1b",
                "prompt_eval_count": 12,
                "eval_count": 5,
                "done_reason": "stop",
            }
        )
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.content == "hello there"
        assert resp.model == "gemma3:1b"
        assert resp.usage == {"input_tokens": 12, "output_tokens": 5}
        assert resp.stop_reason == "stop"
        assert resp.tool_calls == []

    @pytest.mark.asyncio
    async def test_tool_call_response_dict_arguments(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(
            return_value={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "f", "arguments": {"x": 1}}},
                    ],
                }
            }
        )
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "f"
        assert resp.tool_calls[0].arguments == {"x": 1}
        assert resp.tool_calls[0].id == "ollama_0"

    @pytest.mark.asyncio
    async def test_tool_call_response_string_arguments_parsed_as_json(
        self, provider: OllamaProvider
    ) -> None:
        provider._client.chat = AsyncMock(
            return_value={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "f", "arguments": '{"y": 2}'}},
                    ],
                }
            }
        )
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.tool_calls[0].arguments == {"y": 2}

    @pytest.mark.asyncio
    async def test_tool_call_malformed_json_arguments_kept_as_raw(
        self, provider: OllamaProvider
    ) -> None:
        provider._client.chat = AsyncMock(
            return_value={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "f", "arguments": "not-json{"}},
                    ],
                }
            }
        )
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.tool_calls[0].arguments == {"raw": "not-json{"}

    @pytest.mark.asyncio
    async def test_object_style_response(self, provider: OllamaProvider) -> None:
        """Some ollama-client versions return objects with attributes."""
        response_obj = SimpleNamespace(
            message=SimpleNamespace(content="hey", tool_calls=None),
            model="gemma3:1b",
            done_reason="stop",
        )
        provider._client.chat = AsyncMock(return_value=response_obj)
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.content == "hey"

    @pytest.mark.asyncio
    async def test_missing_fields_default_to_empty(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(return_value={})
        resp = await provider.complete([Message(role=Role.USER, content="hi")])
        assert resp.content == ""
        assert resp.usage == {}
        assert resp.stop_reason == ""


# ── error mapping ────────────────────────────────────────────────────


class TestErrorMapping:
    @pytest.mark.asyncio
    async def test_complete_raises_on_client_failure(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(side_effect=ConnectionError("offline"))
        with pytest.raises(ConnectionError, match="offline"):
            await provider.complete([Message(role=Role.USER, content="hi")])

    @pytest.mark.asyncio
    async def test_complete_propagates_timeout(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(side_effect=TimeoutError("slow"))
        with pytest.raises(TimeoutError):
            await provider.complete([Message(role=Role.USER, content="hi")])


# ── streaming ────────────────────────────────────────────────────────


class TestStream:
    @pytest.mark.asyncio
    async def test_stream_yields_content_chunks(self, provider: OllamaProvider) -> None:
        async def fake_stream() -> object:
            for c in ["a", "b", ""]:
                yield {"message": {"content": c}}

        provider._client.chat = AsyncMock(return_value=fake_stream())
        out: list[str] = []
        async for piece in provider.stream([Message(role=Role.USER, content="hi")]):
            out.append(piece)
        assert out == ["a", "b"]

    @pytest.mark.asyncio
    async def test_stream_propagates_error(self, provider: OllamaProvider) -> None:
        provider._client.chat = AsyncMock(side_effect=ConnectionError("nope"))
        with pytest.raises(ConnectionError):
            async for _ in provider.stream([Message(role=Role.USER, content="hi")]):
                pass


# ── health check ─────────────────────────────────────────────────────


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_ok(self, provider: OllamaProvider) -> None:
        provider._client.list = AsyncMock(return_value={"models": [{"name": "g"}]})
        assert await provider.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_failure_returns_false(self, provider: OllamaProvider) -> None:
        provider._client.list = AsyncMock(side_effect=ConnectionError("down"))
        assert await provider.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_non_dict_result(self, provider: OllamaProvider) -> None:
        provider._client.list = AsyncMock(return_value=SimpleNamespace(models=[]))
        assert await provider.health_check() is True
