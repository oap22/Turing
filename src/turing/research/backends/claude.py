"""The Claude backend — the engine loop 1 actually runs on.

The brief pins one engine family deliberately: a round that shifted a
cloud/local mix would produce a delta unrelated to the scaffold, so there is no
mixing. What survives is the tier split — an expensive orchestrator and a cheap
sub-step model — which is cleanly comparable because both sides of it are
recorded per round.

Why this does not reuse ``turing.llm.cloud.ClaudeProvider``:

* ``LLMResponse.usage`` carries only ``input_tokens`` and ``output_tokens``.
  Cache-creation and cache-read tokens are dropped, and a long agentic run is
  mostly cache reads — the cap would under-count by most of its input. That
  alone disqualifies it, because under-counting disables the runaway brake.
* Its retry loop discards failed attempts entirely, so a call that burned three
  rate-limited round trips accounts as one.
* One model is bound per provider instance, so there is no tier to route to,
  and its rate-limit failure is indistinguishable from any other error — the
  solver needs "budget window closed, checkpoint and resume" to be a distinct
  outcome.

Those are fixable in that module, but it is the running assistant's LLM path
and is out of scope here; this backend talks to the SDK directly instead.

**The SDK's own retry layer is switched off** (``max_retries=0``) rather than
left at its default. It retries transparently inside a single ``create`` call,
so a rate-limited request that succeeded on the SDK's second internal attempt
would ship two requests and account for one — the same under-count this module
criticises above, arriving one layer lower. Retrying is done here instead,
where every attempt is charged, and the count of requests actually shipped
reaches the ledger as ``provider_calls``.

Credentials: an API key is optional. Given none, the SDK resolves whatever the
environment already has — an operator profile counts — which is what lets this
run on a subscription rather than on a metered key.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from turing.research.backends.accounting import Usage
from turing.research.backends.base import BaseBackend, RawTurn, estimate_request_tokens
from turing.research.backends.errors import (
    BackendCapacityError,
    BackendConfigurationError,
    BackendContextLengthError,
    BackendError,
    BackendProtocolError,
)
from turing.research.backends.protocol import BackendIdentity, StopReason, ToolCall
from turing.research.backends.tiering import TieringPolicy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from turing.research.backends.protocol import GenerationRequest, ModelMessage

logger = structlog.get_logger(__name__)

#: The orchestrating tier: planning, writing code, reading verifier output.
DEFAULT_ORCHESTRATOR_MODEL = "claude-opus-5"

#: The cheap tier. Mundane sub-steps route here deliberately; the brief's cost
#: argument is that pushing genuinely mundane work down is what buys back the
#: subscription spend that would otherwise all land on the orchestrator.
DEFAULT_SUBSTEP_MODEL = "claude-haiku-4-5"

BACKEND_NAME = "claude"

#: Retries performed by the SDK's own transport. Zero on purpose: a retry the
#: backend cannot see is a request the ledger cannot charge. See
#: :meth:`ClaudeBackend.from_settings`.
SDK_TRANSPORT_RETRIES = 0

#: Statuses worth another attempt. Everything else is a request problem that
#: retrying cannot fix.
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

#: Statuses that mean the configuration is wrong, not that the call was unlucky.
_CONFIGURATION_STATUS_CODES = frozenset({401, 403})

#: Statuses under which a context-length complaint is plausible. Anything else
#: carrying one of the markers below is some other failure quoting them.
_REQUEST_STATUS_CODES = frozenset({400, 413, 422})

#: Substrings that identify a context-length rejection. Matched on the error
#: text because providers do not give this its own status code; kept here in
#: the provider-specific module rather than in the neutral seam.
_CONTEXT_LENGTH_MARKERS = (
    "prompt is too long",
    "context window",
    "context limit",
    "context_length",
    "maximum context",
    "too many tokens",
)

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_CALLS,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
    "pause_turn": StopReason.PAUSE,
    "model_context_window_exceeded": StopReason.CONTEXT_OVERFLOW,
}


def is_context_length_error(exc: BaseException) -> bool:
    """True when ``exc`` says the request did not fit the context window.

    Worth separating from other request problems because the recovery differs:
    compact the transcript and try again, rather than give up on the turn.
    Matched on the message text, which is heuristic — providers do not give
    this its own status code — so it only ever *narrows* an error that was
    already fatal, and a miss costs the old behaviour rather than a new one.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status not in _REQUEST_STATUS_CODES:
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_LENGTH_MARKERS)


def is_retryable_error(exc: BaseException) -> bool:
    """True when ``exc`` describes a transient failure worth retrying.

    Classified by ``status_code`` where the exception carries one, and by
    connection/timeout shape otherwise. Deliberately duck-typed rather than
    matched against SDK exception classes: it keeps the vendor import out of
    the hot path, and it lets tests drive the retry logic with plain fakes
    instead of hand-constructing SDK error objects.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS_CODES
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    name = type(exc).__name__
    return name.endswith(("ConnectionError", "TimeoutError"))


class BackendSettings(BaseSettings):
    """Backend configuration, all via ``TURING_RESEARCH_`` environment vars.

    The API key also accepts the assistant's existing ``TURING_ANTHROPIC_API_KEY``
    so one credential serves both, and may be left empty entirely — see the
    module docstring on subscription credentials.
    """

    model_config = SettingsConfigDict(
        env_prefix="TURING_RESEARCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TURING_RESEARCH_ANTHROPIC_API_KEY",
            "TURING_ANTHROPIC_API_KEY",
        ),
        description="API key; empty means fall back to ambient SDK credentials",
    )
    orchestrator_model: str = Field(
        default=DEFAULT_ORCHESTRATOR_MODEL,
        description="Model serving the orchestrator tier",
    )
    substep_model: str = Field(
        default=DEFAULT_SUBSTEP_MODEL,
        description="Model serving the cheap sub-step tier",
    )
    orchestrator_max_tokens: int = Field(
        default=16000,
        gt=0,
        description="Default output ceiling for the orchestrator tier",
    )
    substep_max_tokens: int = Field(
        default=4096,
        gt=0,
        description="Default output ceiling for the sub-step tier",
    )
    orchestrator_effort: str | None = Field(
        default=None,
        description="Reasoning-effort hint for the orchestrator tier; unset omits it",
    )
    substep_effort: str | None = Field(
        default=None,
        description="Reasoning-effort hint for the sub-step tier; unset omits it. "
        "Not every cheap-tier model accepts one, so the default is to say nothing",
    )
    max_retries: int = Field(default=3, ge=1, description="Attempts per model call")
    retry_base_delay_seconds: float = Field(
        default=1.0,
        ge=0.0,
        description="Base delay for exponential back-off between attempts",
    )
    resume_secret: str = Field(
        default="",
        description="Secret sealing resume tokens; empty leaves them integrity-checked "
        "but unauthenticated, which is sufficient only while nothing with write access "
        "to a checkpoint benefits from a larger budget",
    )

    def tiering_policy(self) -> TieringPolicy:
        """The tier→model mapping these settings describe."""
        return TieringPolicy(
            orchestrator_model=self.orchestrator_model,
            substep_model=self.substep_model,
            orchestrator_max_tokens=self.orchestrator_max_tokens,
            substep_max_tokens=self.substep_max_tokens,
            orchestrator_effort=self.orchestrator_effort,
            substep_effort=self.substep_effort,
        )


class ClaudeBackend(BaseBackend):
    """Two-tier Claude backend over an injected async SDK client.

    The client is injected rather than constructed so tests can drive every
    path — retries, capacity exhaustion, missing usage — without a network
    call. :meth:`from_settings` is the production constructor.

    **Known limitation, recorded rather than hidden.** Reasoning blocks are
    dropped rather than carried back into the transcript. The provider expects
    them returned across a tool-use turn when extended reasoning is active, and
    ``*_effort`` on the tiering policy is the knob that turns that on, so an
    effort-configured run that also uses tools is outside what this backend has
    been built for. Carrying them would need the neutral message type to hold
    provider-opaque blocks, which is a seam change rather than a backend change
    — deliberately not made here for one provider's benefit. Until it is,
    leaving ``*_effort`` unset is the supported configuration, and a turn that
    arrives as reasoning alone is rejected loudly by :meth:`_parse_response`
    rather than silently flattened.
    """

    def __init__(
        self,
        *,
        client: Any,
        policy: TieringPolicy,
        clock: Callable[[], float] = time.monotonic,
        max_retries: int = 3,
        retry_base_delay_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        resume_secret: str | None = None,
    ) -> None:
        if max_retries < 1:
            raise BackendConfigurationError("max_retries must be at least 1")
        super().__init__(
            identity=BackendIdentity(
                backend=BACKEND_NAME,
                orchestrator_model=policy.orchestrator_model,
                substep_model=policy.substep_model,
            ),
            clock=clock,
            resume_secret=resume_secret,
        )
        self._client = client
        self._policy = policy
        self._max_retries = max_retries
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._sleep = sleep

    @classmethod
    def from_settings(
        cls,
        settings: BackendSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> ClaudeBackend:
        """Build a backend and its SDK client from configuration.

        The SDK's transport-level retrying is disabled here. Left at its
        default the client retries inside a single ``create`` call, so a
        request that succeeded on its second internal attempt would ship two
        requests and reach the ledger as one. Retrying belongs in
        :meth:`_invoke`, where every attempt is charged.
        """
        import anthropic  # local import: keeps the SDK off the import path of contracts

        client: Any
        if settings.anthropic_api_key:
            client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key, max_retries=SDK_TRANSPORT_RETRIES
            )
        else:
            client = anthropic.AsyncAnthropic(max_retries=SDK_TRANSPORT_RETRIES)
        return cls(
            client=client,
            policy=settings.tiering_policy(),
            clock=clock,
            max_retries=settings.max_retries,
            retry_base_delay_seconds=settings.retry_base_delay_seconds,
            resume_secret=settings.resume_secret or None,
        )

    @property
    def policy(self) -> TieringPolicy:
        """The tier→model mapping in force."""
        return self._policy

    async def aclose(self) -> None:
        """Close the SDK client if it exposes a closer."""
        closer = getattr(self._client, "close", None)
        if closer is None:
            return
        result = closer()
        if hasattr(result, "__await__"):
            await result

    # ── provider call ───────────────────────────────────────────────────

    async def _invoke(self, request: GenerationRequest) -> RawTurn:
        kwargs = self._build_kwargs(request)
        overhead = Usage()
        last_exc: BaseException | None = None

        for attempt in range(self._max_retries):
            shipped = attempt + 1
            try:
                raw = await self._client.messages.create(**kwargs)
            except Exception as exc:
                if not is_retryable_error(exc):
                    raise self._fatal_error(exc, request, overhead, shipped) from exc
                last_exc = exc
                # A rejected attempt still shipped its input. Charging an
                # estimate for it keeps a retry storm visible to the cap.
                overhead = overhead + Usage(
                    input_tokens=estimate_request_tokens(request), estimated=True
                )
                if attempt < self._max_retries - 1:
                    delay = self._retry_base_delay_seconds * (2**attempt)
                    logger.warning(
                        "research_claude_retry",
                        tier=request.tier.value,
                        model=kwargs["model"],
                        request_id=request.request_id,
                        attempt=attempt + 1,
                        delay_s=delay,
                        error=type(exc).__name__,
                    )
                    await self._sleep(delay)
                continue
            return self._parse_response(raw, overhead, shipped)

        raise BackendCapacityError(
            f"model call failed after {self._max_retries} attempts "
            f"({type(last_exc).__name__ if last_exc else 'unknown'}); "
            "checkpoint the attempt and resume when capacity returns",
            usage=overhead,
            provider_calls=self._max_retries,
        ) from last_exc

    def _fatal_error(
        self,
        exc: Exception,
        request: GenerationRequest,
        overhead: Usage,
        provider_calls: int,
    ) -> BackendError:
        """Wrap a non-retryable provider error, preserving what it spent.

        The charge is the *failing* attempt's own estimated input plus whatever
        earlier rejected attempts already accumulated. Charging only the
        accumulated overhead would drop one whole request whenever a fatal
        error followed a retry, because the accounting layer prefers usage
        attached to an error over its own estimate — so an attached figure that
        omits the failing call is worse than attaching nothing.
        """
        spent = overhead + Usage(input_tokens=estimate_request_tokens(request), estimated=True)
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status in _CONFIGURATION_STATUS_CODES:
            return BackendConfigurationError(
                f"provider rejected credentials (status {status}): {exc}",
                usage=spent,
                provider_calls=provider_calls,
            )
        if is_context_length_error(exc):
            return BackendContextLengthError(
                f"request did not fit the model's context window: {exc}",
                usage=spent,
                provider_calls=provider_calls,
            )
        return BackendProtocolError(
            f"provider call failed: {exc}", usage=spent, provider_calls=provider_calls
        )

    # ── request construction ────────────────────────────────────────────

    def _build_kwargs(self, request: GenerationRequest) -> dict[str, Any]:
        max_tokens = request.max_tokens or self._policy.max_tokens_for(request.tier)
        kwargs: dict[str, Any] = {
            "model": self._policy.model_for(request.tier),
            "max_tokens": max_tokens,
            "messages": self._convert_messages(request.messages),
        }
        if request.system:
            kwargs["system"] = request.system
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": dict(tool.input_schema),
                }
                for tool in request.tools
            ]
        if request.stop_sequences:
            kwargs["stop_sequences"] = list(request.stop_sequences)
        effort = self._policy.effort_for(request.tier)
        if effort:
            kwargs["output_config"] = {"effort": effort}
        return kwargs

    @staticmethod
    def _convert_messages(messages: tuple[ModelMessage, ...]) -> list[dict[str, Any]]:
        """Render the neutral transcript into the provider's message shape."""
        converted: list[dict[str, Any]] = []
        for message in messages:
            blocks: list[dict[str, Any]] = []
            # Tool results lead the turn: the provider requires them first.
            for result in message.tool_results:
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": result.call_id,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                )
            if message.text:
                blocks.append({"type": "text", "text": message.text})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": dict(call.arguments),
                    }
                )
            converted.append({"role": message.role.value, "content": blocks})
        return converted

    # ── response parsing ────────────────────────────────────────────────

    def _parse_response(self, raw: Any, overhead: Usage, provider_calls: int = 1) -> RawTurn:
        reported = self._extract_usage(getattr(raw, "usage", None))
        spent = (reported or Usage()) + overhead

        content = getattr(raw, "content", None)
        if content is None:
            raise BackendProtocolError(
                "provider response carried no content blocks",
                usage=spent if spent.total_tokens else None,
                provider_calls=provider_calls,
            )

        texts: list[str] = []
        calls: list[ToolCall] = []
        block_types: list[str] = []
        for block in content:
            block_type = str(getattr(block, "type", ""))
            block_types.append(block_type)
            if block_type == "text":
                text = str(getattr(block, "text", ""))
                if text:
                    texts.append(text)
            elif block_type == "tool_use":
                arguments = getattr(block, "input", None)
                calls.append(
                    ToolCall(
                        call_id=str(getattr(block, "id", "")),
                        name=str(getattr(block, "name", "")),
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )

        if not texts and not calls:
            # A turn made only of block types this backend drops — reasoning
            # blocks, provider-side tool activity, or a type that did not exist
            # when this was written — parses "successfully" into something no
            # transcript can hold. Failing here, where the call is, beats
            # returning it and failing a line later in the caller's loop on an
            # error that names neither the call nor the reason.
            raise BackendProtocolError(
                "provider returned a turn carrying neither text nor tool calls "
                f"(block types: {sorted(set(block_types))}); it cannot be appended "
                "to a transcript",
                usage=spent if spent.total_tokens else None,
                provider_calls=provider_calls,
            )

        return RawTurn(
            text="\n".join(texts),
            tool_calls=tuple(calls),
            stop_reason=self._map_stop_reason(getattr(raw, "stop_reason", None)),
            usage=reported,
            model=str(getattr(raw, "model", "") or ""),
            extra_usage=overhead if overhead.total_tokens else None,
            provider_calls=provider_calls,
        )

    @staticmethod
    def _map_stop_reason(raw: Any) -> StopReason:
        if not isinstance(raw, str):
            return StopReason.OTHER
        return _STOP_REASONS.get(raw, StopReason.OTHER)

    @staticmethod
    def _extract_usage(raw: Any) -> Usage | None:
        """Read every token class the provider reports.

        Cache classes are summed alongside plain input rather than ignored;
        dropping them is the single largest under-count available in a long
        cached run.
        """
        if raw is None:
            return None

        def _count(name: str) -> int:
            value = getattr(raw, name, 0)
            return int(value) if isinstance(value, int) else 0

        return Usage(
            input_tokens=_count("input_tokens"),
            output_tokens=_count("output_tokens"),
            cache_creation_tokens=_count("cache_creation_input_tokens"),
            cache_read_tokens=_count("cache_read_input_tokens"),
        )


__all__ = [
    "BACKEND_NAME",
    "DEFAULT_ORCHESTRATOR_MODEL",
    "DEFAULT_SUBSTEP_MODEL",
    "RETRYABLE_STATUS_CODES",
    "SDK_TRANSPORT_RETRIES",
    "BackendSettings",
    "ClaudeBackend",
    "is_context_length_error",
    "is_retryable_error",
]
