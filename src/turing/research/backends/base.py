"""Shared machinery every backend gets for free.

A backend author implements one method — :meth:`BaseBackend._invoke` — and
inherits accounting, checkpointing, and structured logging. That is deliberate:
accounting is the runaway brake's odometer, and a second backend that
re-implemented it would be a second chance to get it wrong. Put the counting in
one place and let implementations worry only about talking to their provider.

The estimation policy lives here too. When a provider does not report usage,
the fallback is a conservative estimate, never zero — see
:mod:`turing.research.backends.accounting` for why that direction is the only
safe one.
"""

from __future__ import annotations

import contextlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NoReturn

import structlog

from turing.research.backends.accounting import (
    MESSAGE_TOKEN_OVERHEAD,
    TOOL_TOKEN_OVERHEAD,
    Usage,
    UsageLedger,
    estimate_tokens,
)
from turing.research.backends.errors import BackendError, BackendResumeError
from turing.research.backends.protocol import (
    GenerationRequest,
    GenerationResponse,
    ResumeToken,
    StopReason,
    ToolCall,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.research.backends.protocol import BackendIdentity

logger = structlog.get_logger(__name__)


def estimate_request_tokens(request: GenerationRequest) -> int:
    """Conservatively estimate the input cost of ``request``.

    Counts system text, every message (with a per-message framing surcharge),
    every tool call's name and serialised arguments, every tool result, and
    every tool definition's schema. Deliberately runs high: this figure is only
    used when the provider told us nothing, and the alternative to over-
    estimating is a cap that under-counts.
    """
    total = estimate_tokens(request.system)
    for message in request.messages:
        total += MESSAGE_TOKEN_OVERHEAD
        total += estimate_tokens(message.text)
        for call in message.tool_calls:
            total += estimate_tokens(call.name) + estimate_tokens(call.arguments_json())
        for result in message.tool_results:
            total += MESSAGE_TOKEN_OVERHEAD + estimate_tokens(result.content)
    for tool in request.tools:
        total += TOOL_TOKEN_OVERHEAD
        total += estimate_tokens(tool.name)
        total += estimate_tokens(tool.description)
        total += estimate_tokens(tool.schema_json())
    return total


def estimate_turn_output_tokens(text: str, tool_calls: tuple[ToolCall, ...]) -> int:
    """Conservatively estimate what a returned turn cost to generate."""
    total = estimate_tokens(text)
    for call in tool_calls:
        total += estimate_tokens(call.name) + estimate_tokens(call.arguments_json())
    return total


@dataclass(frozen=True, slots=True)
class RawTurn:
    """What an implementation got back, before accounting.

    ``usage`` is the provider's own figure when it reported one. Leave it
    ``None`` — or leave a side of it at zero — and the base class substitutes a
    conservative estimate for the side that is missing. Zero is treated as
    absent on purpose: no real call consumes nothing, so a zero is a reporting
    gap, and believing it would quietly stop the cap from advancing.

    ``model`` is the model the provider says served the turn. Leave it empty
    when the runtime does not report one and the base class fills in the model
    this engine assigns to the requested tier.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason = StopReason.END_TURN
    usage: Usage | None = None
    model: str = ""
    extra_usage: Usage | None = field(default=None)
    """Consumption incurred outside the returned turn — retried attempts, for
    instance — that would otherwise go uncounted."""
    provider_calls: int = 1
    """Requests actually shipped to produce this turn, retries included."""


class BaseBackend(ABC):
    """Accounting, checkpointing, and logging around a provider call.

    Satisfies :class:`~turing.research.backends.protocol.ModelBackend`.
    Subclasses implement :meth:`_invoke` and nothing else is required.
    """

    def __init__(
        self,
        *,
        identity: BackendIdentity,
        clock: Callable[[], float] = time.monotonic,
        resume_secret: str | None = None,
    ) -> None:
        self._identity = identity
        self._clock = clock
        self._ledger = UsageLedger()
        self._resume_secret = resume_secret

    # ── protocol surface ────────────────────────────────────────────────

    @property
    def identity(self) -> BackendIdentity:
        """Which engine this is, for the round record."""
        return self._identity

    @property
    def ledger(self) -> UsageLedger:
        """What this backend has spent. The cap reads it."""
        return self._ledger

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Run one turn, accounting for it whether or not it succeeds.

        A failed call still burns a step and an estimated input charge. The
        tokens were sent; a cap that forgives failures is a cap a stuck loop
        can spin against forever.
        """
        started = self._clock()
        try:
            turn = await self._invoke(request)
        except BackendError as exc:
            charged = self._record_failure(
                request, exc.usage, self._clock() - started, provider_calls=exc.provider_calls
            )
            _raise_carrying_charge(exc, charged)
        except BaseException as exc:
            # Includes cancellation — a window closing mid-call still spent
            # the input it sent, and dropping that is the under-count this
            # whole module exists to prevent.
            charged = self._record_failure(request, None, self._clock() - started)
            _raise_carrying_charge(exc, charged)

        elapsed = max(0.0, self._clock() - started)
        usage = self._resolve_usage(request, turn)
        self._ledger.record(
            tier=request.tier,
            usage=usage,
            wall_clock_seconds=elapsed,
            provider_calls=max(1, turn.provider_calls),
        )

        model = turn.model or self._identity.model_for_tier(request.tier)
        logger.debug(
            "research_backend_call",
            backend=self._identity.backend,
            tier=request.tier.value,
            model=model,
            request_id=request.request_id,
            stop_reason=turn.stop_reason.value,
            tokens=usage.total_tokens,
            estimated=usage.estimated,
            provider_calls=turn.provider_calls,
            duration_s=round(elapsed, 4),
        )
        return GenerationResponse(
            tier=request.tier,
            model=model,
            text=turn.text,
            tool_calls=turn.tool_calls,
            stop_reason=turn.stop_reason,
            usage=usage,
        )

    def checkpoint(self) -> str:
        """Serialise resumable state into an attempt's ``resume_token``.

        Sealed with this backend's resume secret when it has one; see
        :class:`~turing.research.backends.protocol.ResumeToken` on keyed versus
        unkeyed integrity.
        """
        return ResumeToken(
            backend=self._identity.backend,
            orchestrator_model=self._identity.orchestrator_model,
            substep_model=self._identity.substep_model,
            ledger=self._ledger.snapshot(),
        ).encode(secret=self._resume_secret)

    def restore(self, token: str) -> None:
        """Reinstate state from a token, refusing a mismatched engine.

        The mismatch check is not pedantry. A round's delta is only
        attributable if one engine produced it, so resuming an attempt onto a
        different engine has to fail loudly rather than quietly contaminate the
        round it belongs to.

        The token's seal is verified first: an altered ledger is refused rather
        than read, because reading it would hand back budget already burned.
        """
        decoded = ResumeToken.decode(token, secret=self._resume_secret)
        if not decoded.identity_matches(self._identity):
            raise BackendResumeError(
                "resume token was minted by a different engine "
                f"({decoded.backend}/{decoded.orchestrator_model}/{decoded.substep_model}) "
                f"than this backend "
                f"({self._identity.backend}/{self._identity.orchestrator_model}/"
                f"{self._identity.substep_model})"
            )
        self._ledger.restore(decoded.ledger)

    async def aclose(self) -> None:
        """Release any transport held by this backend. No-op by default."""
        return None

    # ── implementation hook ─────────────────────────────────────────────

    @abstractmethod
    async def _invoke(self, request: GenerationRequest) -> RawTurn:
        """Talk to the provider. Raise a
        :class:`~turing.research.backends.errors.BackendError` on failure,
        attaching whatever usage is known to have been spent."""

    # ── accounting helpers ──────────────────────────────────────────────

    def _resolve_usage(self, request: GenerationRequest, turn: RawTurn) -> Usage:
        """Provider figures where it gave any, a conservative estimate elsewhere.

        The judgement is made **per side** — the three input classes together,
        and output — rather than over the total. A partial report is the normal
        shape for a runtime that counts generated tokens but not prompt tokens,
        and treating any one non-zero field as "the provider told us" would let
        a single reported output token stand in for an entire transcript's
        prompt. The prompt is the larger number by one to two orders of
        magnitude in an agentic loop, so that is not a rounding error: it is the
        cap failing to advance.
        """
        reported = turn.usage or Usage()
        input_side = (
            reported.input_tokens + reported.cache_creation_tokens + reported.cache_read_tokens
        )
        estimated_sides: list[str] = []

        if input_side:
            input_tokens = reported.input_tokens
            cache_creation_tokens = reported.cache_creation_tokens
            cache_read_tokens = reported.cache_read_tokens
        else:
            input_tokens = estimate_request_tokens(request)
            cache_creation_tokens = 0
            cache_read_tokens = 0
            estimated_sides.append("input")

        if reported.output_tokens:
            output_tokens = reported.output_tokens
        else:
            output_tokens = estimate_turn_output_tokens(turn.text, turn.tool_calls)
            estimated_sides.append("output")

        resolved = Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            estimated=bool(estimated_sides),
        )
        if estimated_sides:
            logger.warning(
                "research_backend_usage_estimated",
                backend=self._identity.backend,
                tier=request.tier.value,
                request_id=request.request_id,
                estimated_sides=estimated_sides,
                estimated_tokens=resolved.total_tokens,
            )
        if turn.extra_usage is not None:
            resolved = resolved + turn.extra_usage
        return resolved

    def _record_failure(
        self,
        request: GenerationRequest,
        usage: Usage | None,
        elapsed: float,
        *,
        provider_calls: int = 1,
    ) -> Usage:
        """Charge a failed call: one step, plus what it is known to have sent.

        Output is charged at zero here rather than at the ceiling. A failure
        that produced no output really did produce none, and the step count is
        the brake that catches a loop of failing calls.

        Returns the usage actually recorded so the error can carry it. A
        ``usage=None`` or empty ``Usage()`` on the way in means unknown, not
        free; after this records an estimate, the caller must put that figure
        on the error or the runner will treat a paid call as zero.
        """
        charged = usage
        if charged is None or charged.total_tokens <= 0:
            charged = Usage(input_tokens=estimate_request_tokens(request), estimated=True)
        self._ledger.record(
            tier=request.tier,
            usage=charged,
            wall_clock_seconds=max(0.0, elapsed),
            provider_calls=max(1, provider_calls),
        )
        logger.warning(
            "research_backend_call_failed",
            backend=self._identity.backend,
            tier=request.tier.value,
            request_id=request.request_id,
            charged_tokens=charged.total_tokens,
            estimated=charged.estimated,
            provider_calls=provider_calls,
        )
        return charged


def _tokens_runner_would_read(exc: BaseException) -> int | None:
    """Mirror of ``_spend_carried_on_error``: what the runner would meter.

    ``usage`` wins over ``tokens`` when both are present. That is why a
    stale read-only ``usage`` cannot be papered over by writing ``tokens``.
    """
    usage = getattr(exc, "usage", None)
    if usage is not None:
        accounted = getattr(usage, "total_tokens", 0)
        tokens = int(accounted) if accounted else 0
        return max(0, tokens)
    carried = getattr(exc, "tokens", None)
    if isinstance(carried, int) and carried > 0:
        return carried
    return None


def _carries_ledger_charge(exc: BaseException, charged: Usage) -> bool:
    seen = _tokens_runner_would_read(exc)
    return seen is not None and seen == charged.total_tokens


def _attach_charged_usage(exc: BaseException, charged: Usage) -> None:
    """Best-effort write of the ledger charge onto ``exc``.

    Mutation is not the authority — :func:`_raise_carrying_charge` wraps
    if this does not stick. Empty ``Usage()``, an explicit zero, or a
    foreign object's ``usage``/``tokens`` must not win over the ledger.
    Never a model-authored JSON field.
    """
    with contextlib.suppress(Exception):
        exc.usage = charged  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        exc.tokens = charged.total_tokens  # type: ignore[attr-defined]


def _raise_carrying_charge(exc: BaseException, charged: Usage) -> NoReturn:
    """Re-raise so the runner meters ``charged``, the ledger figure.

    After ``_record_failure`` that figure is what must ride out of
    ``generate``. Mutating ``exc`` is used when it sticks — a writable
    ``usage`` that already *is* the ledger charge is left alone, so a
    legitimate provider total is not replaced with a worse estimate.

    If ``usage`` is read-only, has a setter that rejects the charge, or
    otherwise still reads as a different figure, wrap in
    :class:`BackendError` with ``charged`` and ``exc`` as ``__cause__``.
    Bare ``BaseException`` (cancellation) is not wrapped.
    """
    if not _carries_ledger_charge(exc, charged):
        _attach_charged_usage(exc, charged)
    if _carries_ledger_charge(exc, charged):
        raise
    if not isinstance(exc, Exception):
        raise
    provider_calls = getattr(exc, "provider_calls", 1)
    if not isinstance(provider_calls, int) or provider_calls < 1:
        provider_calls = 1
    wrapped = BackendError(str(exc), usage=charged, provider_calls=provider_calls)
    raise wrapped from exc
