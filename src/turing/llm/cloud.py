"""Anthropic Claude LLM provider with retry logic."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from turing.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    Role,
    ToolCall,
    ToolDefinition,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = structlog.get_logger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # seconds


class ClaudeProvider(LLMProvider):
    """LLM provider backed by the Anthropic Claude API."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514") -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    # ── public interface ────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Send a completion request to the Anthropic API with retry."""
        api_messages = self._convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        response = await self._call_with_retry(kwargs)
        return self._parse_response(response)

    async def stream(
        self,
        messages: list[Message],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Stream text deltas from the Anthropic API."""
        api_messages = self._convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system

        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text

    async def health_check(self) -> bool:
        """Verify API connectivity with a minimal request."""
        try:
            response = await self._client.messages.create(
                model=self._model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return response is not None
        except Exception:
            logger.warning("claude_health_check_failed", exc_info=True)
            return False

    # ── retry logic ────────────────────────────────────────────────────

    async def _call_with_retry(self, kwargs: dict[str, Any]) -> Any:
        """Call the Anthropic API with exponential back-off on transient errors."""
        import anthropic

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            # The back-off exists to space out the *next* attempt, so there is
            # nothing to wait for once the last one has failed. Sleeping there
            # anyway just delayed the exception — on the default settings that
            # was a 4s pause added to every exhausted retry, and the router's
            # local→cloud fail-open path pays it while a user waits.
            is_last_attempt = attempt == _MAX_RETRIES - 1
            delay = 0.0 if is_last_attempt else _BASE_DELAY * (2**attempt)
            try:
                return await self._client.messages.create(**kwargs)
            except anthropic.RateLimitError as exc:
                last_exc = exc
                logger.warning(
                    "claude_rate_limited",
                    attempt=attempt + 1,
                    delay=delay,
                )
                if delay:
                    await asyncio.sleep(delay)
            except anthropic.APIConnectionError as exc:
                last_exc = exc
                logger.warning(
                    "claude_connection_error",
                    attempt=attempt + 1,
                    delay=delay,
                )
                if delay:
                    await asyncio.sleep(delay)
            except anthropic.APIStatusError as exc:
                # Non-retryable API errors (auth, bad request, etc.)
                logger.error(
                    "claude_api_error",
                    status_code=exc.status_code,
                    message=str(exc),
                )
                raise

        assert last_exc is not None
        raise last_exc

    # ── format conversion helpers ──────────────────────────────────────

    @staticmethod
    def _convert_messages(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal Message objects to Anthropic API format."""
        api_msgs: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == Role.SYSTEM:
                # System messages are passed via the top-level `system` param;
                # skip them in the messages list.
                continue

            if msg.role == Role.TOOL:
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id or "",
                    "content": msg.content,
                }
                # Every tool_result answering one assistant turn belongs in a
                # single user message. Emitting one message per result splits
                # the reply to a parallel tool call across several turns, which
                # teaches the model to stop issuing parallel calls — so results
                # for a run of tool messages are coalesced here.
                if (
                    api_msgs
                    and api_msgs[-1]["role"] == "user"
                    and isinstance(api_msgs[-1]["content"], list)
                    and api_msgs[-1]["content"][0].get("type") == "tool_result"
                ):
                    api_msgs[-1]["content"].append(block)
                else:
                    api_msgs.append({"role": "user", "content": [block]})
                continue

            if msg.role == Role.ASSISTANT and msg.tool_calls:
                content_blocks: list[dict[str, Any]] = []
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                api_msgs.append(
                    {
                        "role": "assistant",
                        "content": content_blocks,
                    }
                )
                continue

            api_msgs.append(
                {
                    "role": msg.role.value,
                    "content": msg.content,
                }
            )
        return api_msgs

    @staticmethod
    def _convert_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert internal ToolDefinition objects to Anthropic tool format."""
        api_tools: list[dict[str, Any]] = []
        for tool in tools:
            api_tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
            )
        return api_tools

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse an Anthropic API response into an LLMResponse."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        usage = {}
        if hasattr(response, "usage") and response.usage is not None:
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }

        return LLMResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            model=response.model,
            usage=usage,
            stop_reason=response.stop_reason or "",
        )
