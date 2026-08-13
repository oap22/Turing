"""A scripted backend for tests. Deterministic, offline, no provider involved.

Every test in this package and downstream of it drives a backend through this
class. It runs the *real* accounting path — it subclasses
:class:`~turing.research.backends.base.BaseBackend` like any other backend — so
a test that asserts on cap consumption is asserting on the same code the Claude
backend uses, not on a parallel implementation that could drift from it.

Determinism comes from two places: the script, and
:class:`DeterministicClock`, which advances by a fixed step per reading so
wall-clock consumption is an exact number rather than a flaky one.

Running past the end of the script raises
:class:`~turing.research.backends.errors.ScriptExhaustedError` rather than
looping or returning something empty. A test that makes more calls than it
scripted has found a bug, and the loudest possible failure is the useful one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from turing.research.backends.base import BaseBackend, RawTurn
from turing.research.backends.errors import ScriptExhaustedError
from turing.research.backends.protocol import BackendIdentity, StopReason

if TYPE_CHECKING:
    from collections.abc import Sequence

    from turing.research.backends.accounting import Usage
    from turing.research.backends.protocol import GenerationRequest, ToolCall

BACKEND_NAME = "fake"

DEFAULT_IDENTITY = BackendIdentity(
    backend=BACKEND_NAME,
    orchestrator_model="fake-orchestrator",
    substep_model="fake-substep",
)


class DeterministicClock:
    """A monotonic clock that advances a fixed amount per reading.

    :meth:`~turing.research.backends.base.BaseBackend.generate` reads the clock
    twice per call, so a ``step`` of 0.5 makes every call cost exactly 0.5
    seconds of wall clock — an exact assertion instead of a tolerance.
    """

    __slots__ = ("_now", "step")

    def __init__(self, *, start: float = 0.0, step: float = 0.5) -> None:
        self.step = step
        self._now = start

    def __call__(self) -> float:
        now = self._now
        self._now += self.step
        return now


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    """One scripted response, or one scripted failure.

    Set ``error`` to make the call raise instead of returning; the accounting
    layer then charges it as a failed call, which is how failure-path budget
    tests are written.

    Leaving ``usage`` unset exercises the conservative-estimation fallback.
    Set it to assert on provider-reported figures instead.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason | None = None
    usage: Usage | None = None
    model: str = ""
    error: BaseException | None = None

    def resolved_stop_reason(self) -> StopReason:
        """Explicit stop reason if given, else inferred from tool calls."""
        if self.stop_reason is not None:
            return self.stop_reason
        return StopReason.TOOL_CALLS if self.tool_calls else StopReason.END_TURN


class FakeBackend(BaseBackend):
    """Replays a fixed script of turns, recording every request it was given.

    ``calls`` is the recorded transcript of requests. Assert on it to check
    that mundane work actually routed to the cheap tier — the tiering split
    only buys anything if call sites really use it.
    """

    def __init__(
        self,
        script: Sequence[ScriptedTurn] = (),
        *,
        identity: BackendIdentity = DEFAULT_IDENTITY,
        clock: DeterministicClock | None = None,
        loop: bool = False,
    ) -> None:
        super().__init__(identity=identity, clock=clock or DeterministicClock())
        self._script = tuple(script)
        self._loop = loop
        self._index = 0
        self.calls: list[GenerationRequest] = []

    @classmethod
    def replying(
        cls,
        *texts: str,
        identity: BackendIdentity = DEFAULT_IDENTITY,
        clock: DeterministicClock | None = None,
    ) -> FakeBackend:
        """Shorthand for a script of plain text replies."""
        return cls(
            [ScriptedTurn(text=text) for text in texts],
            identity=identity,
            clock=clock,
        )

    @property
    def remaining(self) -> int:
        """Scripted turns not yet consumed. Negative-proof; ``0`` when looping."""
        if self._loop:
            return 0
        return max(0, len(self._script) - self._index)

    async def _invoke(self, request: GenerationRequest) -> RawTurn:
        self.calls.append(request)
        turn = self._next_turn()
        if turn.error is not None:
            raise turn.error
        return RawTurn(
            text=turn.text,
            tool_calls=turn.tool_calls,
            stop_reason=turn.resolved_stop_reason(),
            usage=turn.usage,
            model=turn.model or self._identity.model_for_tier(request.tier),
        )

    def _next_turn(self) -> ScriptedTurn:
        if not self._script:
            raise ScriptExhaustedError("fake backend was given no scripted turns")
        if self._index >= len(self._script):
            if not self._loop:
                raise ScriptExhaustedError(
                    f"fake backend exhausted its script after {self._index} calls; "
                    "the code under test made more model calls than the test scripted"
                )
            self._index = 0
        turn = self._script[self._index]
        self._index += 1
        return turn
