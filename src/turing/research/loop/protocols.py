"""Structural interfaces the round runner drives, plus the clock it reads.

The round runner is the experiment's *instrument*. It deliberately owns the
three things the thing being measured must not own:

* **The cap.** ``SolverStep`` cannot report a step count — the runner charges
  exactly one step per step it drives. A solver that reports nothing still
  burns budget, so the runaway-loop brake stays real even for a buggy or
  adversarial solver.
* **The verifier.** The runner calls :meth:`Verifier.verify`; the solver never
  holds a reference to it. Harness integrity (brief § Gates) says the scorer
  must be unreachable from the agent's write surface, and the cheapest way to
  approach that in-process is to never hand it over.
* **The decision to stop.** ``SolverStep.escalate`` lets a solver *ask* for a
  human; there is no field by which it can quit. See
  :class:`~turing.research.contracts.AttemptState`.

Everything here is a :class:`typing.Protocol` so the loop package has no import
dependency on :mod:`turing.research.solver` or
:mod:`turing.research.backends` — the round runner is testable against a fake
solver, which is how its own correctness is established.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from turing.research.contracts import (
    CapConsumption,
    ContractViolationError,
    ProblemType,
    Split,
)

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import (
        Attempt,
        EscalationDecision,
        EscalationReason,
        EscalationRequest,
        Problem,
    )

__all__ = [
    "Clock",
    "EscalationChannel",
    "Solver",
    "SolverStep",
    "SolverTask",
    "SystemClock",
    "WorkspaceProvider",
]


@dataclass(frozen=True, slots=True)
class SolverStep:
    """What one unit of solver work consumed, and what it wants next.

    ``tokens`` are self-reported because only the backend can know them.
    **Steps are not self-reported**: the runner charges one step per call, so
    a solver that under-reports (or reports nothing) can still be stopped.
    **Wall-clock is not self-reported either**: the runner measures it on its
    own clock around the step and the verifier. ``wall_clock_seconds`` here is
    leftover backend telemetry; the cap does not read it.

    That asymmetry is the whole reason this is a record of a *step* rather
    than a ``CapConsumption``.

    ``escalate`` is the only exit a solver has, and it is a request, not a
    decision — the runner turns it into an :class:`EscalationRequest` and
    suspends until an operator replies with ``continue`` / ``abandon`` /
    ``extend_cap``.
    """

    tokens: int = 0
    wall_clock_seconds: float = 0.0
    resume_token: str | None = None
    note: str = ""
    escalate: EscalationReason | None = None
    made_progress: bool = True

    def __post_init__(self) -> None:
        if self.tokens < 0 or self.wall_clock_seconds < 0:
            raise ContractViolationError("a solver step cannot consume negative budget")

    @property
    def consumption(self) -> CapConsumption:
        """Budget to charge for this step — always at least one step."""
        return CapConsumption(
            steps=1,
            tokens=self.tokens,
            wall_clock_seconds=self.wall_clock_seconds,
        )


@dataclass(frozen=True, slots=True)
class SolverTask:
    """The slice of a problem a solver is allowed to see.

    No verifier, no workspace template, no default cap. The runner owns those.
    ``attempt.workspace_path`` is the working directory; ``attempt.result`` is
    read-only feedback from the last official verify. Every field is a
    primitive (plain ``str``, ``ProblemType``, ``Split``, or a tuple of plain
    ``str``) so a verifier cannot ride in through ``tags`` or ``score_scale``.
    """

    id: str
    problem_type: ProblemType
    goal: str
    split: Split
    score_scale: str
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_plain_str(self.id, field="id")
        _require_plain_str(self.goal, field="goal")
        _require_plain_str(self.score_scale, field="score_scale")
        if type(self.problem_type) is not ProblemType:
            raise ContractViolationError(
                f"SolverTask.problem_type must be ProblemType, got "
                f"{type(self.problem_type).__qualname__}"
            )
        if type(self.split) is not Split:
            raise ContractViolationError(
                f"SolverTask.split must be Split, got {type(self.split).__qualname__}"
            )
        object.__setattr__(self, "tags", _require_plain_tags(self.tags))

    @classmethod
    def from_problem(cls, problem: Problem) -> SolverTask:
        return cls(
            id=problem.id,
            problem_type=problem.problem_type,
            goal=problem.goal,
            split=problem.split,
            score_scale=problem.verifier.score_scale,
            tags=problem.tags,
        )


def _require_plain_str(value: object, *, field: str) -> str:
    """``str`` subclasses can grow a ``verify`` method; only a plain str is a name."""
    if type(value) is not str:
        raise ContractViolationError(
            f"SolverTask.{field} must be a plain str, got {type(value).__qualname__}; "
            "a non-string field is how a verifier handle reaches the solver"
        )
    return value


def _require_plain_tags(tags: object) -> tuple[str, ...]:
    if isinstance(tags, str) or not isinstance(tags, (tuple, list)):
        raise ContractViolationError(
            f"SolverTask.tags must be a sequence of plain str, got {type(tags).__qualname__}; "
            "a verifier in tags is an off-budget verify"
        )
    return tuple(_require_plain_str(tag, field="tags") for tag in tags)


class Solver(Protocol):
    """One unit of forward progress inside a single attempt's workspace.

    Implemented in :mod:`turing.research.solver`. The runner calls
    :meth:`step` repeatedly until the verifier passes, the cap trips, or an
    escalation resolves; between calls it verifies the workspace and
    checkpoints the attempt, so an interrupted run costs the remainder of the
    attempt rather than the attempt.
    """

    async def step(self, task: SolverTask, attempt: Attempt) -> SolverStep:
        """Advance the solution in ``attempt.workspace_path`` by one step.

        ``attempt`` carries the current checkpoint, including ``consumed`` /
        ``remaining`` budget and ``result`` — the best official verification so
        far. That result is *read-only feedback*: the brief's solver iterates
        against the frozen verifier, but it may never edit, relax, or
        regenerate it. ``task`` is the problem without the verifier.
        """
        ...


class WorkspaceProvider(Protocol):
    """Materialises a problem's fresh working directory for one attempt."""

    async def materialise(self, problem: Problem, *, attempt_id: str) -> Path:
        """Return a fresh directory seeded from ``problem.workspace_template``."""
        ...


class EscalationChannel(Protocol):
    """Delivers an escalation to the operator and suspends until they decide."""

    async def request_decision(self, request: EscalationRequest) -> EscalationDecision:
        """Push ``request`` to the operator and block until a decision arrives.

        Implementations must never invent a decision (a timeout that defaults
        to ``continue`` would silently erase a human-gate-load event, which is
        driving function #4), and must reject any reply carrying free-form
        advice.
        """
        ...


class Clock(Protocol):
    """Wall-clock and monotonic time, injected so tests can be deterministic."""

    def now_ms(self) -> int:
        """Unix epoch milliseconds, for timestamps written into artifacts."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, for measuring durations."""
        ...


class SystemClock:
    """The real clock. Default for every component in this package."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def monotonic(self) -> float:
        return time.monotonic()
