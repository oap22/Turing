"""Ollama provider: keep_alive / num_ctx pass-through, model listing, warm-up."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turing.llm.base import Message, Role
from turing.llm.local import OllamaProvider


def _provider(**kwargs: object) -> OllamaProvider:
    with patch("ollama.AsyncClient") as mock_cls:
        client = MagicMock()
        client.chat = AsyncMock(return_value={"message": {"content": "hi"}})
        client.list = AsyncMock()
        client.generate = AsyncMock()
        mock_cls.return_value = client
        p = OllamaProvider(host="http://test:11434", model="qwen2.5:7b", **kwargs)  # type: ignore[arg-type]
    p._client = client  # type: ignore[assignment]
    return p


class TestKeepAliveAndContext:
    @pytest.mark.asyncio
    async def test_defaults_send_neither(self) -> None:
        p = _provider()
        await p.complete([Message(role=Role.USER, content="x")])
        kwargs = p._client.chat.call_args.kwargs
        assert "keep_alive" not in kwargs
        assert "num_ctx" not in kwargs["options"]

    @pytest.mark.asyncio
    async def test_complete_passes_keep_alive_and_num_ctx(self) -> None:
        p = _provider(keep_alive="30m", num_ctx=8192)
        await p.complete([Message(role=Role.USER, content="x")], max_tokens=64)
        kwargs = p._client.chat.call_args.kwargs
        assert kwargs["keep_alive"] == "30m"
        assert kwargs["options"] == {"num_predict": 64, "temperature": 0.7, "num_ctx": 8192}

    @pytest.mark.asyncio
    async def test_stream_passes_keep_alive_and_num_ctx(self) -> None:
        p = _provider(keep_alive="-1", num_ctx=4096)

        async def _gen():  # type: ignore[no-untyped-def]
            yield {"message": {"content": "a"}}

        p._client.chat = AsyncMock(return_value=_gen())
        out = [c async for c in p.stream([Message(role=Role.USER, content="x")])]
        assert out == ["a"]
        kwargs = p._client.chat.call_args.kwargs
        assert kwargs["keep_alive"] == "-1"
        assert kwargs["options"]["num_ctx"] == 4096

    def test_exposes_model_and_host(self) -> None:
        p = _provider()
        assert p.model == "qwen2.5:7b"
        assert p.host == "http://test:11434"


class TestListModels:
    @pytest.mark.asyncio
    async def test_dict_shape(self) -> None:
        p = _provider()
        p._client.list = AsyncMock(
            return_value={"models": [{"model": "qwen2.5:7b"}, {"name": "gemma3:1b"}, {}]}
        )
        assert await p.list_models() == ["qwen2.5:7b", "gemma3:1b"]

    @pytest.mark.asyncio
    async def test_object_shape(self) -> None:
        # Newer ollama clients return typed objects with attributes.
        p = _provider()
        p._client.list = AsyncMock(
            return_value=SimpleNamespace(
                models=[
                    SimpleNamespace(model="qwen2.5:7b", name=None),
                    SimpleNamespace(model=None, name="x:1b"),
                ]
            )
        )
        assert await p.list_models() == ["qwen2.5:7b", "x:1b"]

    @pytest.mark.asyncio
    async def test_errors_propagate(self) -> None:
        p = _provider()
        p._client.list = AsyncMock(side_effect=ConnectionError("down"))
        with pytest.raises(ConnectionError):
            await p.list_models()


class TestWarmup:
    @pytest.mark.asyncio
    async def test_warmup_preloads_with_empty_prompt_and_keep_alive(self) -> None:
        p = _provider(keep_alive="1h")
        assert await p.warmup() is True
        p._client.generate.assert_awaited_once_with(model="qwen2.5:7b", prompt="", keep_alive="1h")

    @pytest.mark.asyncio
    async def test_warmup_failure_is_logged_not_raised(self) -> None:
        p = _provider()
        p._client.generate = AsyncMock(side_effect=ConnectionError("no ollama"))
        assert await p.warmup() is False
