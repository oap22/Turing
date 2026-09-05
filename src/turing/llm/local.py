"""Ollama local LLM provider."""

from __future__ import annotations

from collections import defaultdict, deque
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

_TIMEOUT_SECONDS = 120


def _consume_tool_name(
    pending_tool_names: dict[str, deque[str]],
    tool_call_id: str | None,
) -> str:
    """Pop the function name for one internal tool result occurrence."""
    if tool_call_id:
        names = pending_tool_names.get(tool_call_id)
        if names:
            name = names.popleft()
            if not names:
                pending_tool_names.pop(tool_call_id, None)
            return name
    # A malformed/out-of-order transcript can omit the ID.  Preserve the
    # message rather than guessing a function name; Ollama will return a
    # useful API validation error to the caller.
    return ""


class OllamaProvider(LLMProvider):
    """LLM provider backed by a local Ollama instance."""

    def __init__(self, host: str = "http://localhost:11434", model: str = "gemma3:1b") -> None:
        import ollama

        self._client = ollama.AsyncClient(host=host, timeout=_TIMEOUT_SECONDS)
        self._model = model
        self._host = host

    # ── public interface ────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[Message],
        system: str = "",
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Send a chat completion request to Ollama."""
        api_messages = self._convert_messages(messages, system)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }

        # Ollama has limited tool support — only some models handle it.
        # Convert tools if provided, but fall back to plain text if the
        # model doesn't return tool calls.
        ollama_tools: list[dict[str, Any]] | None = None
        if tools:
            ollama_tools = self._convert_tools(tools)
            kwargs["tools"] = ollama_tools

        try:
            response = await self._client.chat(**kwargs)
            return self._parse_response(response)
        except Exception:
            logger.error("ollama_completion_failed", model=self._model, exc_info=True)
            raise

    async def stream(
        self,
        messages: list[Message],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Stream text deltas from Ollama."""
        api_messages = self._convert_messages(messages, system)

        try:
            response_stream = await self._client.chat(
                model=self._model,
                messages=api_messages,
                stream=True,
                options={
                    "num_predict": max_tokens,
                    "temperature": temperature,
                },
            )
            async for chunk in response_stream:
                content = chunk.get("message", {}).get("content", "")
                if content:
                    yield content
        except Exception:
            logger.error("ollama_stream_failed", model=self._model, exc_info=True)
            raise

    async def health_check(self) -> bool:
        """Check Ollama connectivity by listing available models."""
        try:
            result = await self._client.list()
            logger.debug(
                "ollama_health_ok",
                model_count=len(result.get("models", [])) if isinstance(result, dict) else 0,
            )
            return True
        except Exception:
            logger.warning("ollama_health_check_failed", host=self._host, exc_info=True)
            return False

    # ── format conversion helpers ──────────────────────────────────────

    @staticmethod
    def _convert_messages(messages: list[Message], system: str = "") -> list[dict[str, Any]]:
        """Convert internal Message objects to Ollama chat format."""
        api_msgs: list[dict[str, Any]] = []

        # Ollama's chat API correlates a tool result by ``tool_name`` rather
        # than OpenAI's ``tool_call_id``.  Ollama responses do not provide a
        # stable call ID, so _parse_response creates an occurrence-scoped
        # synthetic ID (``ollama_0``, ...).  Keep FIFO queues per ID here: a
        # later assistant turn can legitimately reuse ollama_0.
        pending_tool_names: dict[str, deque[str]] = defaultdict(deque)

        if system:
            api_msgs.append({"role": "system", "content": system})

        for msg in messages:
            if msg.role == Role.SYSTEM:
                api_msgs.append({"role": "system", "content": msg.content})
            elif msg.role == Role.TOOL:
                # Ollama expects tool results as a tool-role message with the
                # called function's name.  Resolve the internal ID against
                # preceding assistant batches, preserving repeated IDs across
                # multiple rounds and parallel calls.
                tool_name = _consume_tool_name(pending_tool_names, msg.tool_call_id)
                tool_entry: dict[str, Any] = {"role": "tool", "content": msg.content}
                if tool_name:
                    tool_entry["tool_name"] = tool_name
                api_msgs.append(tool_entry)
            elif msg.role == Role.ASSISTANT and msg.tool_calls:
                # Include tool_calls in the assistant message for models that support it
                tool_calls_payload = [
                    {
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
                assistant_entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": tool_calls_payload,
                }
                api_msgs.append(assistant_entry)
                for tc in msg.tool_calls:
                    pending_tool_names[tc.id].append(tc.name)
            else:
                api_msgs.append({"role": msg.role.value, "content": msg.content})

        return api_msgs


    @staticmethod
    def _convert_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert ToolDefinitions to Ollama's OpenAI-style tool format."""
        ollama_tools: list[dict[str, Any]] = []
        for tool in tools:
            ollama_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return ollama_tools

    @staticmethod
    def _parse_response(response: Any) -> LLMResponse:
        """Parse an Ollama chat response into an LLMResponse."""
        message = (
            response.get("message", {})
            if isinstance(response, dict)
            else getattr(response, "message", {})
        )
        if not isinstance(message, dict):
            # ollama client may return an object with attributes
            message = {
                "content": getattr(message, "content", "") or "",
                "tool_calls": getattr(message, "tool_calls", None),
            }

        content = message.get("content", "") or ""
        tool_calls: list[ToolCall] = []

        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            for i, tc in enumerate(raw_tool_calls):
                func = (
                    tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", {})
                )
                if not isinstance(func, dict):
                    func = {
                        "name": getattr(func, "name", ""),
                        "arguments": getattr(func, "arguments", {}),
                    }
                name = func.get("name", "")
                arguments = func.get("arguments", {})
                if isinstance(arguments, str):
                    import json

                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw": arguments}
                tool_calls.append(
                    ToolCall(
                        id=f"ollama_{i}",
                        name=name,
                        arguments=arguments,
                    )
                )

        # Extract usage from Ollama response
        usage: dict[str, int] = {}
        if isinstance(response, dict):
            if "prompt_eval_count" in response:
                usage["input_tokens"] = response["prompt_eval_count"]
            if "eval_count" in response:
                usage["output_tokens"] = response["eval_count"]

        model_name = ""
        if isinstance(response, dict):
            model_name = response.get("model", "")
        elif hasattr(response, "model"):
            model_name = response.model or ""

        stop_reason = ""
        if isinstance(response, dict):
            done_reason = response.get("done_reason", "")
            if done_reason:
                stop_reason = done_reason
        elif hasattr(response, "done_reason"):
            stop_reason = response.done_reason or ""

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            model=model_name,
            usage=usage,
            stop_reason=stop_reason,
        )
