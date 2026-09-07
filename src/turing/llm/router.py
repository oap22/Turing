"""LLM router that dispatches requests based on complexity and configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import structlog

from turing.llm.classifier import Complexity, ComplexityClassifier
from turing.telemetry import redact, traced

if TYPE_CHECKING:
    from turing.llm.base import (
        LLMProvider,
        LLMResponse,
        Message,
        ToolDefinition,
    )

logger = structlog.get_logger(__name__)

RoutingMode = Literal["cloud_only", "local_only", "auto"]


def warn_if_local_only_disables_tools(
    routing_mode: str,
    tool_count: int,
    local_tools_enabled: bool = False,
) -> bool:
    """Emit a startup WARN when ``local_only`` routing makes tools unreachable.

    Local Ollama models have varying tool compliance. Unless explicitly opted
    in, local routing strips the tool catalog and a tool-requiring request may
    be answered as prose. See issue #158.

    Returns True when the warning was emitted, False otherwise — handy for
    tests and so callers can react if they want to.
    """
    if routing_mode == "local_only" and tool_count > 0 and not local_tools_enabled:
        logger.warning(
            "llm.local_only_disables_tools",
            tool_count=tool_count,
            hint=(
                "set TURING_OLLAMA_TOOLS_ENABLED=true for a capable local model, "
                "or use TURING_LLM_ROUTING_MODE=auto (or cloud)"
            ),
        )
        return True
    return False


def _llm_event_payload(
    kind: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    result: Any,
    exc: BaseException | None,
) -> dict[str, Any]:
    """Build the telemetry payload for ``LLMRouter._invoke_provider`` events."""
    # _invoke_provider(self, provider, label, messages, system, tools, max_tokens, temperature)
    self_obj = args[0] if args else None
    provider = args[1] if len(args) > 1 else kwargs.get("provider")
    label = args[2] if len(args) > 2 else kwargs.get("label", "unknown")
    messages = args[3] if len(args) > 3 else kwargs.get("messages", [])

    max_bytes = 2048
    if self_obj is not None:
        max_bytes = getattr(self_obj, "_sample_max_bytes", 2048)

    data: dict[str, Any] = {
        "provider": label,
        "model": getattr(provider, "model", "unknown"),
    }
    if kind == "start":
        joined = "\n".join(getattr(m, "content", "") for m in messages)
        data["prompt_sample"] = redact(joined, max_bytes=max_bytes)
    elif kind == "end" and result is not None:
        data["response_sample"] = redact(getattr(result, "content", ""), max_bytes=max_bytes)
        data["stop_reason"] = getattr(result, "stop_reason", "")
    return data


class LLMRouter:
    """Routes LLM requests to the appropriate provider.

    Routing strategies
    ------------------
    * **cloud_only** — always use the cloud provider.
    * **local_only** — always use the local provider.
    * **auto** — classify the message complexity and pick the best
      provider.  Falls back from local to cloud on errors or timeouts.
      The classifier is given visibility into whether tools are attached;
      if it judges the message tool-shaped or otherwise complex the
      request goes to cloud (with tools).  Otherwise it is served locally
      with the tool catalog stripped, since local Ollama models have
      limited tool-use support and would not invoke them reliably anyway.
    """

    def __init__(
        self,
        cloud_provider: LLMProvider,
        local_provider: LLMProvider,
        classifier: ComplexityClassifier | None = None,
        routing_mode: RoutingMode = "auto",
        prompt_sample_max_bytes: int = 2048,
        local_tools_enabled: bool = False,
    ) -> None:
        self._cloud = cloud_provider
        self._local = local_provider
        self._classifier = classifier or ComplexityClassifier()
        self._routing_mode: RoutingMode = routing_mode
        self._sample_max_bytes = prompt_sample_max_bytes
        self._local_tools_enabled = local_tools_enabled

    # ── public API ─────────────────────────────────────────────────────

    async def route(
        self,
        messages: list[Message],
        system: str = "",
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Route the request to the appropriate LLM provider and return the response."""
        provider, reason = self._select_provider(messages, tools)
        label = _provider_label(provider, self._cloud, self._local)
        # Local Ollama tool use is opt-in.  The opt-in applies to local_only,
        # classifier-selected local calls, and cloud->local auth fallbacks.
        effective_tools = tools if provider is self._cloud or self._local_tools_enabled else None
        logger.info(
            "llm_route_decision",
            provider=label,
            reason=reason,
            routing_mode=self._routing_mode,
            tools_attached=bool(effective_tools),
        )

        try:
            primary: LLMResponse = await self._invoke_provider(
                provider, label, messages, system, effective_tools, max_tokens, temperature
            )
            return primary
        except Exception as exc:
            # If the selected provider was local (auto mode), fall back to cloud
            # — and restore the original tool catalog for the cloud retry.
            if provider is self._local and self._routing_mode == "auto":
                logger.warning(
                    "llm_local_failed_falling_back_to_cloud",
                    exc_info=True,
                )
                fallback: LLMResponse = await self._invoke_provider(
                    self._cloud, "cloud", messages, system, tools, max_tokens, temperature
                )
                return fallback
            # If cloud failed because credentials are missing/invalid, fail open
            # to local. The dev simulator boots nodes without an Anthropic key
            # and crashing the turn is worse than serving a local response.
            if (
                provider is self._cloud
                and self._routing_mode == "auto"
                and _is_cloud_unavailable(exc)
            ):
                logger.warning(
                    "llm_cloud_unavailable_failing_open_to_local",
                    exc_info=True,
                )
                local_fallback: LLMResponse = await self._invoke_provider(
                    self._local,
                    "local",
                    messages,
                    system,
                    tools if self._local_tools_enabled else None,
                    max_tokens,
                    temperature,
                )
                return local_fallback
            raise

    @traced("llm.complete", payload=_llm_event_payload)
    async def _invoke_provider(
        self,
        provider: LLMProvider,
        label: str,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        """Single instrumentation point for any provider.complete dispatch."""
        return await provider.complete(
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    # ── provider selection ─────────────────────────────────────────────

    def _select_provider(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
    ) -> tuple[LLMProvider, str]:
        """Pick a provider and return (provider, reason) tuple."""
        if self._routing_mode == "cloud_only":
            return self._cloud, "routing_mode=cloud_only"

        if self._routing_mode == "local_only":
            return self._local, "routing_mode=local_only"

        # auto mode — let the classifier decide, even when tools are
        # attached.  Tool presence is fed in as a signal so tool-shaped
        # phrasing biases toward cloud, but pure chat with a tool catalog
        # attached must still route locally (issue #160).
        last_user_message = self._extract_last_user_message(messages)
        complexity = self._classifier.classify(
            last_user_message,
            has_tool_definitions=bool(tools),
        )

        if complexity == Complexity.COMPLEX:
            return self._cloud, f"complexity={complexity.value}"

        return self._local, f"complexity={complexity.value}"

    @staticmethod
    def _extract_last_user_message(messages: list[Message]) -> str:
        """Return the content of the most recent user message."""
        for msg in reversed(messages):
            if msg.role.value == "user":
                return msg.content
        return ""


def _is_cloud_unavailable(exc: BaseException) -> bool:
    """Return True if ``exc`` indicates the cloud provider is unreachable
    because of missing credentials or auth failure.

    We try to import anthropic lazily so the router stays usable in tests
    that stub the cloud provider without the SDK installed.
    """
    try:
        import anthropic
    except ImportError:  # pragma: no cover - anthropic is a hard dep in prod
        return False

    # Some SDK versions surface a missing key as a plain TypeError/ValueError
    # at construction time; the router caller (provider init) will raise
    # before we get here, so we only need to handle runtime auth errors.
    return isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError)


def _provider_label(
    provider: LLMProvider,
    cloud: LLMProvider,
    local: LLMProvider,
) -> str:
    """Return a human-readable label for the chosen provider."""
    if provider is cloud:
        return "cloud"
    if provider is local:
        return "local"
    return "unknown"
