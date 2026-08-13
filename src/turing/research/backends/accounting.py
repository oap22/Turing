"""Token, step, and wall-clock accounting for the per-project cap.

This module is the runaway brake's odometer. The cap
(:class:`~turing.research.contracts.Cap`) replaced dollar metering as the only
thing standing between a stuck solver and an unbounded loop, and it can only
trip on numbers this module produces. **Under-counting here silently disables
it** — the loop keeps running and every "failed within cap" outcome becomes a
lie — so every judgement call below is resolved in the direction of counting
*more*:

* All four token classes are summed, not just the two obvious ones. A cached
  read is cheaper, not free, and a provider that reports cache classes
  separately would otherwise have most of its input silently dropped.
* A call that fails still costs a step, and still costs an estimated input
  charge. The tokens were sent.
* Missing reported usage is treated as *absent*, not as zero, and replaced with
  a conservative estimate flagged ``estimated``. That judgement is made per
  *side* — input and output separately — because a runtime that reports
  generation tokens but not prompt tokens is a common shape, and believing its
  silence about the prompt would drop the larger of the two numbers.
* The character-per-token divisors are deliberately below real tokenizer
  ratios, and are per character class, because one divisor tuned on English
  prose under-counts every other payload this agent actually reads.

The ledger is append-only: totals never decrease, and a snapshot survives a
checkpoint so a resumed attempt continues from what it already spent rather
than starting its budget over.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from turing.research.backends.errors import BackendAccountingError
from turing.research.backends.tiering import ModelTier
from turing.research.contracts import CapConsumption

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Characters assumed per token for ordinary Latin-script text and code.
#:
#: Real English prose runs near 3.5–4 characters per token and source code
#: lower. Dividing by a *smaller* number yields a *larger* estimate, which is
#: the safe direction: an over-estimate trips the brake early, an under-estimate
#: lets a stuck project run past its cap.
CONSERVATIVE_CHARS_PER_TOKEN = 2.5

#: Characters assumed per token for ASCII digits.
#:
#: Tokenizers group digits in runs of one to three, so numeric payloads — CSV
#: dumps, metric tables, timing logs, which are exactly what a research agent
#: reads — land near two characters per token, well under the prose ratio.
#: Measured against a real BPE vocabulary, numeric CSV rows came out at 0.73 of
#: their true count under a single 3.0 divisor.
DIGIT_CHARS_PER_TOKEN = 2.0

#: Characters assumed per token for anything outside ASCII.
#:
#: Non-Latin scripts cost roughly a token per character, sometimes more, in
#: every vocabulary worth estimating against; the same measurement put CJK log
#: lines at 0.48 of their true count under a single prose-tuned divisor. One
#: token per character is the floor that makes the estimate an over-count
#: again.
NON_ASCII_CHARS_PER_TOKEN = 1.0

#: Flat surcharge per message, covering role framing and delimiters that carry
#: no characters of their own but do cost tokens.
MESSAGE_TOKEN_OVERHEAD = 8

#: Flat surcharge per tool definition, covering schema framing.
TOOL_TOKEN_OVERHEAD = 16

_ASCII_DIGITS = "0123456789"


def estimate_tokens(text: str) -> int:
    """Conservatively estimate the token cost of ``text``.

    Rounds up, and weights three character classes separately: ASCII digits,
    everything else in ASCII, and everything outside it. A single divisor tuned
    on English prose is an *under*-count on the payloads this agent actually
    reads — numeric dumps and non-Latin logs — and an under-count is the one
    direction that disables the cap.

    Known residual, recorded rather than papered over: high-entropy ASCII with
    no word structure (base64 blobs, hashes, minified bundles) still tokenizes
    below the ASCII divisor, so a transcript that is mostly base64 can be
    under-estimated. Bounding that case would need a divisor around 1.5, which
    would over-charge ordinary prose by more than two-fold and cut the usable
    token cap accordingly. Two things keep it survivable: every backend that
    reports usage never reaches this path at all, and the step and wall-clock
    dimensions of the cap are unaffected by it.
    """
    if not text:
        return 0
    ascii_chars = len(text.encode("ascii", "ignore"))
    non_ascii_chars = len(text) - ascii_chars
    digits = sum(text.count(digit) for digit in _ASCII_DIGITS)
    other_ascii = ascii_chars - digits
    return math.ceil(
        other_ascii / CONSERVATIVE_CHARS_PER_TOKEN
        + digits / DIGIT_CHARS_PER_TOKEN
        + non_ascii_chars / NON_ASCII_CHARS_PER_TOKEN
    )


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens consumed by one model call.

    Cache classes are tracked separately from plain input because providers
    report them separately, but :attr:`total_tokens` sums all four. They are
    all tokens the request paid to move.

    ``estimated`` marks a figure the backend inferred rather than received.
    It propagates through addition, so a ledger can report how much of its
    total is inferred — a number worth watching, because a run whose accounting
    is mostly estimated is a run whose cap is mostly guesswork.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    estimated: bool = False

    def __post_init__(self) -> None:
        if (
            self.input_tokens < 0
            or self.output_tokens < 0
            or self.cache_creation_tokens < 0
            or self.cache_read_tokens < 0
        ):
            raise BackendAccountingError("token counts cannot be negative")

    @property
    def total_tokens(self) -> int:
        """Every token class summed. This is what the cap spends."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            estimated=self.estimated or other.estimated,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation, for checkpointing."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "estimated": self.estimated,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Usage:
        """Rebuild from :meth:`to_dict` output."""
        try:
            return cls(
                input_tokens=int(raw["input_tokens"]),
                output_tokens=int(raw["output_tokens"]),
                cache_creation_tokens=int(raw["cache_creation_tokens"]),
                cache_read_tokens=int(raw["cache_read_tokens"]),
                estimated=bool(raw["estimated"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendAccountingError(f"malformed usage record: {exc}") from exc


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """An immutable copy of a ledger, safe to persist and reload.

    This is what makes an interruption cost the remainder of an attempt rather
    than the attempt: the consumption already burned rides through the
    checkpoint instead of being forgotten and re-granted.
    """

    steps: int
    wall_clock_seconds: float
    per_tier: Mapping[ModelTier, Usage]
    estimated_steps: int = 0
    provider_calls: int = 0

    def __post_init__(self) -> None:
        if self.steps < 0 or self.estimated_steps < 0 or self.wall_clock_seconds < 0:
            raise BackendAccountingError("snapshot counters cannot be negative")
        if self.provider_calls < 0:
            raise BackendAccountingError("snapshot counters cannot be negative")
        if self.estimated_steps > self.steps:
            raise BackendAccountingError("estimated steps cannot exceed total steps")
        object.__setattr__(self, "per_tier", dict(self.per_tier))

    @property
    def total_usage(self) -> Usage:
        """Usage summed across tiers."""
        total = Usage()
        for usage in self.per_tier.values():
            total = total + usage
        return total

    @property
    def consumption(self) -> CapConsumption:
        """This snapshot as the contracts-level budget type.

        ``wall_clock_seconds`` carries the same restricted meaning as
        :attr:`UsageLedger.wall_clock_seconds` — time inside model calls only.
        Read that property's docstring before wiring this into an attempt's
        wall-clock dimension.
        """
        return CapConsumption(
            steps=self.steps,
            tokens=self.total_usage.total_tokens,
            wall_clock_seconds=self.wall_clock_seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation, for checkpointing."""
        return {
            "steps": self.steps,
            "estimated_steps": self.estimated_steps,
            "provider_calls": self.provider_calls,
            "wall_clock_seconds": self.wall_clock_seconds,
            "per_tier": {tier.value: usage.to_dict() for tier, usage in self.per_tier.items()},
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LedgerSnapshot:
        """Rebuild from :meth:`to_dict` output.

        A record with no ``provider_calls`` key is read as one request per step
        rather than as zero: an older checkpoint is missing the field, not
        evidence that no requests were made.
        """
        try:
            per_tier_raw = raw["per_tier"]
            per_tier = {
                ModelTier(tier): Usage.from_dict(usage) for tier, usage in per_tier_raw.items()
            }
            steps = int(raw["steps"])
            return cls(
                steps=steps,
                wall_clock_seconds=float(raw["wall_clock_seconds"]),
                per_tier=per_tier,
                estimated_steps=int(raw.get("estimated_steps", 0)),
                provider_calls=int(raw.get("provider_calls", steps)),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise BackendAccountingError(f"malformed ledger snapshot: {exc}") from exc


class UsageLedger:
    """Append-only record of what a backend has spent.

    Not thread-safe, and deliberately not locked: :meth:`record` never awaits,
    so concurrent coroutines on one event loop cannot interleave inside it.
    Sharing one ledger across OS threads is not supported.

    The per-tier breakdown is not decoration. The brief's cost argument rests
    on mundane work actually being routed to the cheap tier, and this is the
    number that says whether it was.
    """

    __slots__ = (
        "_estimated_steps",
        "_per_tier",
        "_provider_calls",
        "_steps",
        "_wall_clock_seconds",
    )

    def __init__(self) -> None:
        self._steps = 0
        self._estimated_steps = 0
        self._provider_calls = 0
        self._wall_clock_seconds = 0.0
        self._per_tier: dict[ModelTier, Usage] = {}

    def record(
        self,
        *,
        tier: ModelTier,
        usage: Usage,
        wall_clock_seconds: float,
        steps: int = 1,
        provider_calls: int = 1,
    ) -> None:
        """Add one call's consumption. Totals only ever grow.

        ``steps`` counts turns the solver took; ``provider_calls`` counts
        requests actually shipped, which is larger whenever a turn was retried.
        The two are separate on purpose — see :attr:`steps`.
        """
        if steps < 0:
            raise BackendAccountingError("cannot record negative steps")
        if provider_calls < 0:
            raise BackendAccountingError("cannot record negative provider calls")
        if wall_clock_seconds < 0:
            raise BackendAccountingError("cannot record negative wall clock")
        self._steps += steps
        if usage.estimated:
            self._estimated_steps += steps
        self._provider_calls += provider_calls
        self._wall_clock_seconds += wall_clock_seconds
        self._per_tier[tier] = self._per_tier.get(tier, Usage()) + usage

    @property
    def steps(self) -> int:
        """Turns taken, successful or not — one per call to ``generate``.

        A turn that was retried internally is still one step, because a step is
        the unit the cap's step dimension counts and the solver advances one
        step per turn. The request-level number is :attr:`provider_calls`, and
        the gap between the two is what makes a retry storm visible instead of
        hidden inside a single step.
        """
        return self._steps

    @property
    def estimated_steps(self) -> int:
        """Calls whose token cost was inferred rather than reported."""
        return self._estimated_steps

    @property
    def provider_calls(self) -> int:
        """Requests actually shipped, retries included.

        Diagnostic rather than a cap dimension: it says whether a run's steps
        cost one request each or three. A backend whose transport retries
        beneath it reports too few here, which is why implementations disable
        transport-level retrying and do their retrying where it can be counted.
        """
        return self._provider_calls

    @property
    def wall_clock_seconds(self) -> float:
        """Seconds spent inside model calls.

        **Not the attempt's wall clock.** For this agent most elapsed time goes
        into training scripts and test suites, not into waiting on a model, so
        this figure is a small and varying fraction of real elapsed time. The
        attempt's wall-clock dimension belongs to whoever owns the clock around
        the whole iteration; feeding this in as a substitute would silently
        disable that dimension of the cap.
        """
        return self._wall_clock_seconds

    @property
    def tokens(self) -> int:
        """Every token class, every tier, summed."""
        return self.total_usage().total_tokens

    def usage_for(self, tier: ModelTier) -> Usage:
        """Usage attributed to one tier."""
        return self._per_tier.get(tier, Usage())

    def total_usage(self) -> Usage:
        """Usage summed across tiers."""
        total = Usage()
        for usage in self._per_tier.values():
            total = total + usage
        return total

    @property
    def consumption(self) -> CapConsumption:
        """The ledger's spend, shaped as the contracts-level budget type.

        Steps and tokens are complete: this is everything the backend burned on
        those two dimensions and the cap can be driven straight off them.
        **Wall clock is not** — it covers time inside model calls only, per
        :attr:`wall_clock_seconds`, so a caller that owns the clock around the
        whole iteration must keep charging its own elapsed time and must not
        replace it with this one.

        The integration pattern that works: charge the *difference* between two
        readings of this property for steps and tokens, and charge wall clock
        from the caller's own timer.
        """
        return CapConsumption(
            steps=self._steps,
            tokens=self.tokens,
            wall_clock_seconds=self._wall_clock_seconds,
        )

    def snapshot(self) -> LedgerSnapshot:
        """Take an immutable copy for checkpointing."""
        return LedgerSnapshot(
            steps=self._steps,
            wall_clock_seconds=self._wall_clock_seconds,
            per_tier=dict(self._per_tier),
            estimated_steps=self._estimated_steps,
            provider_calls=self._provider_calls,
        )

    def restore(self, snapshot: LedgerSnapshot) -> None:
        """Replace this ledger's contents with ``snapshot``.

        Only valid on a fresh ledger. Restoring over recorded consumption would
        discard it, which is exactly the under-count this module exists to
        prevent.
        """
        if self._steps or self._wall_clock_seconds or self._per_tier or self._provider_calls:
            raise BackendAccountingError(
                "cannot restore onto a ledger that has already recorded consumption; "
                "restoring would discard it and re-grant the budget it spent"
            )
        self._steps = snapshot.steps
        self._estimated_steps = snapshot.estimated_steps
        self._provider_calls = snapshot.provider_calls
        self._wall_clock_seconds = snapshot.wall_clock_seconds
        self._per_tier = {tier: replace(usage) for tier, usage in snapshot.per_tier.items()}
