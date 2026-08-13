"""Shared contracts for the autonomous research agent (loop 1).

Every other module in :mod:`turing.research` codes against the types defined
here. They encode the invariants from
``research/briefs/2026-08-12-autonomous-research-agent.md`` so the ones an
autonomous, unattended solver has an incentive to erode are visible in
constructors and enums. Visibility is not the same as enforcement: some
checks below are real, others remain call-site conventions.

The four invariants this module is built around:

1. **The verifier is frozen.** A project is a ``(goal, verifier)`` pair; the
   verifier is human-supplied or human-approved *before work begins*. See
   :class:`Verifier` for what the freeze actually checks — ordinary writes
   are refused; same-process ``object.__setattr__`` is not, and process
   isolation is open.
2. **The agent may not quit.** There is no "give up" member of
   :class:`AttemptState`. A project the solver judges hopeless is supposed
   to raise an :class:`EscalationRequest`. :attr:`AttemptState.ABANDONED`
   requires a non-empty ``escalation_id``, not an operator
   :class:`EscalationDecision`: a leftover id is enough, so
   ``ESCALATED.pause().evolve(ABANDONED)`` still reaches the terminal state.
   Solver and runner call sites gate on an operator verdict; requiring that
   verdict on the type is a parked invariant.
3. **The operator decides, it does not advise.** :class:`EscalationDecision`
   carries a three-valued verdict and (for ``EXTEND_CAP``) a numeric budget.
   There is no free-text channel back into the run. See the class docstring for
   why that would be fatal to the measurement.
4. **Scores are continuous and never averaged across problem types.**
   :class:`RoundRecord` has no aggregate scalar; it exposes per-``(type,
   split)`` cells only.

**Scope: loop 1 only.** The self-editing loop (loop 2) is not implemented.
Where loop 2 will attach, the docstring says so explicitly — chiefly
:meth:`RoundRecord.self_edit_visible_scores`, which is the *only* sanctioned
way for round results to reach a self-edit summary and which drops held-out
results by construction. Nothing here implements scaffold self-modification,
cheat detection, or rollback.
"""

from __future__ import annotations

import contextlib
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

__all__ = [
    "HARNESS_FAILURE_KEY",
    "SCORE_SCALE_LEADERBOARD_PERCENTILE",
    "SCORE_SCALE_SPEEDUP",
    "TERMINAL_ATTEMPT_STATES",
    "Attempt",
    "AttemptState",
    "Cap",
    "CapConsumption",
    "CapDimension",
    "CapExtension",
    "CapExtensionError",
    "ContractViolationError",
    "EngineIdentity",
    "EscalationDecision",
    "EscalationProtocolError",
    "EscalationReason",
    "EscalationRequest",
    "EscalationVerdict",
    "EvalSetMismatchError",
    "FrozenVerifierError",
    "Problem",
    "ProblemType",
    "RoundCost",
    "RoundDelta",
    "RoundRecord",
    "Split",
    "TypeScore",
    "VerificationResult",
    "Verifier",
    "round_cells",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ContractViolationError(Exception):
    """A research contract was constructed or used in a way the design forbids.

    These are programming errors, not runtime conditions. A solver hitting one
    means a rule from the brief was broken in code, so they are loud rather
    than recoverable.
    """


class FrozenVerifierError(ContractViolationError):
    """A verifier that is not actually immutable was bound to a problem.

    The single most load-bearing guarantee in the system: the agent may never
    edit, relax, or regenerate the bar it is graded on.
    """


class CapExtensionError(ContractViolationError):
    """An illegal change to a per-project cap was attempted."""


class EscalationProtocolError(ContractViolationError):
    """An escalation request or decision violated the fixed vocabulary."""


class EvalSetMismatchError(ContractViolationError):
    """Two rounds measured on different eval sets were compared.

    ``driving-functions.md`` forbids this outright: if the eval set changes the
    trajectory restarts, and a curve drawn across the change is fiction.
    """


# --------------------------------------------------------------------------- #
# Corpus taxonomy
# --------------------------------------------------------------------------- #


class ProblemType(str, Enum):  # noqa: UP042
    """Family a problem belongs to.

    Extensible: new families are added as members here. Adding one is a
    measurement decision, not a detail — per-type scores are reported
    separately and **never averaged across types**, because a round that helps
    one family and hurts another would otherwise read as flat. Experiment 1
    ships ``SPEEDUP`` (6 problems) and ``KAGGLE`` (5); genuine research
    questions are deferred to experiment 2 and will add a member then.
    """

    SPEEDUP = "speedup"
    KAGGLE = "kaggle"


class Split(str, Enum):  # noqa: UP042
    """Whether a problem's results may inform the scaffold.

    Every problem is *scored and reported* regardless of split. The split
    controls one thing only: ``HELD_OUT`` results must never reach the loop-2
    self-edit summary. Without that, the agent reads round *N*'s results on the
    whole corpus, edits its skills, and is re-scored on the same corpus — at
    which point "got better at ML research" is indistinguishable from
    "memorised eleven problems". The practice-vs-held-out gap is the direct
    evidence of memorisation.
    """

    PRACTICE = "practice"
    HELD_OUT = "held_out"


#: Conventional values for :attr:`VerificationResult.score_scale`. Free-form by
#: design — each problem scores on its own scale and the scales are never
#: pooled — but a shared spelling keeps the trajectory readable.
SCORE_SCALE_SPEEDUP = "speedup_ratio"
SCORE_SCALE_LEADERBOARD_PERCENTILE = "leaderboard_percentile"


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


#: Set to ``1.0`` in :attr:`VerificationResult.raw_measurements` when the
#: verifier could not do its job — missing pinned reference, dead benchmark,
#: unreadable artifact. The loop reads this flag and escalates
#: (:attr:`EscalationReason.HARNESS_FAILURE`) rather than recording a
#: capability failure the agent could not have prevented.
HARNESS_FAILURE_KEY = "harness_failure"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What a verifier reports about one finished workspace.

    ``score`` is **continuous, not binary**, and always oriented so that higher
    is better — a verifier that natively measures a runtime normalises it into
    a speedup ratio. With ~11 problems and no parallel sweep, a binary pass
    rate has almost no resolution; a continuous score recovers most of the
    statistical power given up when the sweep was dropped.

    ``passed_correctness`` is deliberately a *separate* boolean rather than a
    score of zero. A fast-but-wrong solution and a slow-but-right one are
    different failures with different fixes, and collapsing them into one
    number destroys the distinction. Callers ranking solutions must gate on
    ``passed_correctness`` before reading ``score``.

    A result with :data:`HARNESS_FAILURE_KEY` set is not a grade: the
    instrument broke. :attr:`harness_failed` is how the loop tells that apart
    from a workspace that was graded and found wanting.
    """

    problem_id: str
    verifier_id: str
    score: float
    passed_correctness: bool
    score_scale: str
    raw_measurements: Mapping[str, float] = field(default_factory=dict)
    detail: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.score):
            raise ContractViolationError(f"score must be finite, got {self.score!r}")
        # Provenance must survive the trip: a caller holding this result should
        # not be able to rewrite the measurements it was derived from.
        object.__setattr__(self, "raw_measurements", MappingProxyType(dict(self.raw_measurements)))

    @property
    def harness_failed(self) -> bool:
        """True when this result is an instrument failure, not a grade."""
        return float(self.raw_measurements.get(HARNESS_FAILURE_KEY, 0.0)) != 0.0


@dataclass(frozen=True)
class Verifier(ABC):
    """The frozen bar a problem is graded against.

    **Invariant: ordinary writes cannot mutate a verifier.** The verifier is
    supplied by the operator, or proposed by the agent and approved by the
    operator *before work begins*; from that moment it is frozen against
    attribute assignment. This is the load-bearing property of the whole
    design — it is what removes reward hacking at the criterion level, and
    every downstream number is void without it.

    Three mechanisms enforce it, in increasing order of strength:

    * This is a ``frozen`` dataclass, so attribute assignment raises
      ``FrozenInstanceError`` on instances of it *and of every subclass*,
      including subclasses that are not themselves dataclasses.
    * Python itself refuses ``@dataclass`` subclasses that are not frozen
      (``cannot inherit non-frozen dataclass from a frozen one``), and
      :meth:`__init_subclass__` below refuses subclasses that try to reinstate
      writability by overriding ``__setattr__``/``__delattr__``.
    * :class:`Problem` probes each verifier behaviourally at bind time, so a
      circumvention route not anticipated here still fails loudly.

    What this does **not** defend against is a determined
    ``object.__setattr__`` from inside the same process — no Python type system
    can. The real boundary is process isolation: keeping the harness, scorer
    and eval data physically unreachable from the agent's write surface
    (brief § Gates, open question Q11 / ADR 0011 R2). That isolation is
    **open, not enforced**. These mechanisms make accidental mutation
    impossible and deliberate mutation conspicuous; they do not make
    mutation impossible.

    Subclasses live in :mod:`turing.research.problems` and are the *only* place
    where scoring logic belongs. ``verify`` is async because verification runs
    real subprocesses (timing harnesses, ``pytest``, scoring scripts).
    """

    verifier_id: str
    problem_id: str
    description: str
    score_scale: str

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # ``@dataclass(slots=True)`` re-creates the class object, which fires
        # __init_subclass__ a second time on a namespace that already contains
        # the dataclass-generated frozen __setattr__. Detect that pass by the
        # presence of processed dataclass metadata and let it through: nothing
        # new was declared.
        if "__dataclass_fields__" in cls.__dict__:
            return
        for hook in ("__setattr__", "__delattr__"):
            if hook in cls.__dict__:
                raise FrozenVerifierError(
                    f"{cls.__qualname__} overrides {hook}; a verifier must stay immutable "
                    "so the solver cannot relax the bar it is graded on"
                )

    @abstractmethod
    async def verify(self, workspace: Path) -> VerificationResult:
        """Grade a finished workspace and report where it landed.

        Args:
            workspace: Directory the solver worked in. Treated as read-only
                input; a verifier that writes into the solver's workspace can
                leak the answer back to it.

        Returns:
            A :class:`VerificationResult` whose ``problem_id`` and
            ``verifier_id`` match this verifier's own.
        """


def _reject_if_mutable(verifier: Verifier) -> None:
    """Fail unless ``verifier`` actually rejects attribute assignment.

    A behavioural probe rather than a structural inspection: it catches every
    route to a writable verifier, including ones the class hierarchy checks
    above do not anticipate.
    """
    probe = "_turing_freeze_probe"
    try:
        setattr(verifier, probe, True)
    except Exception:
        return
    with contextlib.suppress(Exception):
        object.__delattr__(verifier, probe)
    raise FrozenVerifierError(
        f"{type(verifier).__qualname__} accepted attribute assignment; a verifier must be "
        "immutable so the solver cannot edit, relax, or regenerate its own bar"
    )


# --------------------------------------------------------------------------- #
# Problems
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Problem:
    """One unit of work: a ``(goal, verifier)`` pair.

    The brief calls this a *project*; the corpus calls it a *problem*. Same
    unit — one goal, one frozen verifier, one workspace, one cap.

    ``workspace_template`` is the directory copied into the agent's fresh
    working directory before an attempt starts. Copying rather than working in
    place is what makes attempts independent and re-runnable at a new seed,
    and what keeps the source repos the speedup problems came from unmodified.
    ``workspace_excludes`` are basename patterns for
    :func:`shutil.ignore_patterns` applied at that copy. They have to live on
    the problem, not only on the family spec: the loop and the solver copy
    from this object, and they never call the adapter's materialise method.
    """

    id: str
    problem_type: ProblemType
    goal: str
    workspace_template: Path
    verifier: Verifier
    split: Split
    default_cap: Cap | None = None
    tags: tuple[str, ...] = ()
    workspace_excludes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise ContractViolationError("problem id must be non-empty")
        _reject_if_mutable(self.verifier)
        if self.verifier.problem_id != self.id:
            raise ContractViolationError(
                f"verifier {self.verifier.verifier_id!r} grades problem "
                f"{self.verifier.problem_id!r}, not {self.id!r}"
            )

    @property
    def verifier_id(self) -> str:
        return self.verifier.verifier_id

    @property
    def is_held_out(self) -> bool:
        """True when this problem's results must not reach a self-edit summary."""
        return self.split is Split.HELD_OUT

    @property
    def is_practice(self) -> bool:
        return self.split is Split.PRACTICE


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #


class CapDimension(str, Enum):  # noqa: UP042
    """Which axis of a :class:`Cap` was exhausted."""

    STEPS = "steps"
    TOKENS = "tokens"
    WALL_CLOCK = "wall_clock"


@dataclass(frozen=True, slots=True)
class CapConsumption:
    """How much of a cap an attempt has burned so far.

    Accumulates across resumes: an attempt interrupted when the subscription
    window closes carries its consumption through the checkpoint, so an
    interruption costs the remainder of the attempt rather than the attempt.
    """

    steps: int = 0
    tokens: int = 0
    wall_clock_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.steps < 0 or self.tokens < 0 or self.wall_clock_seconds < 0:
            raise ContractViolationError("consumption cannot be negative")

    def __add__(self, other: CapConsumption) -> CapConsumption:
        return CapConsumption(
            steps=self.steps + other.steps,
            tokens=self.tokens + other.tokens,
            wall_clock_seconds=self.wall_clock_seconds + other.wall_clock_seconds,
        )


@dataclass(frozen=True, slots=True)
class CapExtension:
    """Extra budget granted by an operator ``EXTEND_CAP`` decision.

    Purely numeric. It is the one thing an operator may hand back to a running
    project beyond a three-valued verdict, and it carries no guidance about
    *how* to spend it — see :class:`EscalationDecision`.
    """

    extra_steps: int = 0
    extra_tokens: int = 0
    extra_wall_clock_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.extra_steps < 0 or self.extra_tokens < 0 or self.extra_wall_clock_seconds < 0:
            raise CapExtensionError(
                "a cap extension may only add budget; negative extensions would let the "
                "runaway-loop brake be tightened out from under a running attempt"
            )
        if not (self.extra_steps or self.extra_tokens or self.extra_wall_clock_seconds):
            raise CapExtensionError(
                "an EXTEND_CAP decision that grants nothing is indistinguishable from "
                "CONTINUE; say CONTINUE instead"
            )


@dataclass(frozen=True, slots=True)
class Cap:
    """The per-project budget: steps, tokens, wall-clock.

    Replaces the retired dollar ``BudgetGate`` as the runaway-loop brake. It is
    a *safety* device, not a scoring device: "failed within cap" is a
    first-class, logged outcome, and without it solve rate is undefined because
    a project that never terminates is neither a pass nor a fail.

    A cap only ever grows, and only via :meth:`extend` from an operator
    decision. ``extension_count`` is lineage, not decoration — a project that
    reached its score after three extensions is not comparable to one that did
    it inside the original budget, and the round record needs to be able to say
    so.
    """

    max_steps: int
    max_tokens: int
    max_wall_clock_seconds: float
    extension_count: int = 0

    def __post_init__(self) -> None:
        if self.max_steps <= 0 or self.max_tokens <= 0 or self.max_wall_clock_seconds <= 0:
            raise ContractViolationError("every cap dimension must be positive")
        if self.extension_count < 0:
            raise ContractViolationError("extension_count cannot be negative")

    def exceeded(self, consumed: CapConsumption) -> tuple[CapDimension, ...]:
        """Which dimensions this consumption has run past, in declaration order."""
        hit: list[CapDimension] = []
        if consumed.steps >= self.max_steps:
            hit.append(CapDimension.STEPS)
        if consumed.tokens >= self.max_tokens:
            hit.append(CapDimension.TOKENS)
        if consumed.wall_clock_seconds >= self.max_wall_clock_seconds:
            hit.append(CapDimension.WALL_CLOCK)
        return tuple(hit)

    def is_exhausted(self, consumed: CapConsumption) -> bool:
        return bool(self.exceeded(consumed))

    def remaining(self, consumed: CapConsumption) -> CapConsumption:
        """Headroom left, clamped at zero on every axis."""
        return CapConsumption(
            steps=max(0, self.max_steps - consumed.steps),
            tokens=max(0, self.max_tokens - consumed.tokens),
            wall_clock_seconds=max(0.0, self.max_wall_clock_seconds - consumed.wall_clock_seconds),
        )

    def extend(self, extension: CapExtension) -> Cap:
        """Return a larger cap. The only sanctioned way a cap ever changes."""
        return Cap(
            max_steps=self.max_steps + extension.extra_steps,
            max_tokens=self.max_tokens + extension.extra_tokens,
            max_wall_clock_seconds=(
                self.max_wall_clock_seconds + extension.extra_wall_clock_seconds
            ),
            extension_count=self.extension_count + 1,
        )


# --------------------------------------------------------------------------- #
# Attempts
# --------------------------------------------------------------------------- #


class AttemptState(str, Enum):  # noqa: UP042
    """Lifecycle of one attempt at one problem.

    Note what is absent: there is no state meaning "I have decided this is
    hopeless". The intended path is an :class:`EscalationRequest` into
    :attr:`ESCALATED`, then an operator :attr:`EscalationVerdict.ABANDON` into
    :attr:`ABANDONED`. The type does not require that verdict — only a
    non-empty ``escalation_id``. A leftover id is enough, including
    ``ESCALATED.pause().evolve(ABANDONED)``. Call sites in the solver and
    runner gate on the verdict; that parked check is what would make
    escalations-per-round a genuine measure of human-gate load.
    """

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    VERIFYING = "verifying"
    PASSED = "passed"
    FAILED_WITHIN_CAP = "failed_within_cap"
    ESCALATED = "escalated"
    ABANDONED = "abandoned"


#: States from which an attempt never moves again.
TERMINAL_ATTEMPT_STATES: frozenset[AttemptState] = frozenset(
    {
        AttemptState.PASSED,
        AttemptState.FAILED_WITHIN_CAP,
        AttemptState.ABANDONED,
    }
)


def _require_abandoned_has_escalation_id(state: AttemptState, escalation_id: str | None) -> None:
    if state is AttemptState.ABANDONED and not escalation_id:
        raise ContractViolationError("ABANDONED requires an escalation_id; the agent may not quit")


def _escalation_id_for_state(state: AttemptState, escalation_id: str | None) -> str | None:
    """RUNNING drops a leftover id so ``evolve(ABANDONED)`` from RUNNING fails.

    Other states keep one. ``ABANDONED`` checks for an id, not a verdict, so
    ``ESCALATED.pause().evolve(ABANDONED)`` still works.
    """
    if state is AttemptState.RUNNING:
        return None
    return escalation_id


@dataclass(frozen=True, slots=True)
class Attempt:
    """An immutable checkpoint of one attempt at one problem.

    Attempts are checkpoints rather than mutable objects on purpose. The
    operator's Claude subscription closes at unpredictable points, and the
    requirement is that an interruption costs the *remainder* of an attempt,
    not the attempt. Every state change produces a new ``Attempt`` with
    ``checkpoint_seq`` incremented, so persisting the latest one and reloading
    it is the whole of resume.

    ``resume_token`` is the seam for that: an opaque, backend-defined handle
    (for the Claude backend, whatever identifies the in-progress conversation)
    that :mod:`turing.research.backends` writes and reads. Contracts do not
    interpret it.

    ``seed`` is recorded per attempt because the agent is stochastic *within* a
    project as well as across seeds, and the noise floor has to separate the
    two sources of spread.

    Construction and :meth:`evolve` both refuse ``ABANDONED`` without a
    non-empty ``escalation_id``. That is an id check, not a verdict check: a
    leftover id from ``ESCALATED`` is enough, including via
    ``pause().evolve(ABANDONED)``. ``RUNNING`` never carries a leftover id
    (construction, :meth:`evolve`, and :meth:`resume` clear one) so that
    path cannot skip even the id check. Requiring an operator
    :class:`EscalationDecision` is a parked invariant, honoured at call
    sites.
    """

    attempt_id: str
    problem_id: str
    round_id: str
    seed: int
    workspace_path: Path
    cap: Cap
    consumed: CapConsumption = field(default_factory=CapConsumption)
    state: AttemptState = AttemptState.PENDING
    step_index: int = 0
    checkpoint_seq: int = 0
    resume_token: str | None = None
    started_at_ms: int | None = None
    updated_at_ms: int | None = None
    result: VerificationResult | None = None
    escalation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "escalation_id", _escalation_id_for_state(self.state, self.escalation_id)
        )
        _require_abandoned_has_escalation_id(self.state, self.escalation_id)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_ATTEMPT_STATES

    @property
    def is_resumable(self) -> bool:
        """True when a fresh process may pick this attempt back up.

        ``VERIFYING`` is included because a crash can land after that state
        was written and before verify returned. Resume re-runs verify from
        the journalled ``APPLIED`` iteration; losing the attempt would
        violate "interruption costs the remainder, not the attempt".
        """
        return self.state in (
            AttemptState.PENDING,
            AttemptState.RUNNING,
            AttemptState.PAUSED,
            AttemptState.VERIFYING,
        )

    @property
    def cap_exhausted(self) -> bool:
        return self.cap.is_exhausted(self.consumed)

    @property
    def exceeded_cap_dimensions(self) -> tuple[CapDimension, ...]:
        return self.cap.exceeded(self.consumed)

    @property
    def remaining(self) -> CapConsumption:
        return self.cap.remaining(self.consumed)

    def evolve(self, *, now_ms: int, **changes: Any) -> Attempt:
        """Return the next checkpoint, advancing ``checkpoint_seq`` by one.

        ``cap`` and ``consumed`` are not replaceable here. Cap growth is
        :meth:`apply_cap_extension`; consumption is additive via
        :meth:`record_consumption`. ``ABANDONED`` requires an
        ``escalation_id`` — an id, not an operator verdict — so a leftover
        id on a non-RUNNING state is enough. A transition to ``RUNNING``
        clears a leftover ``escalation_id``, matching CONTINUE/EXTEND_CAP.
        Pause from ``ESCALATED`` keeps the id.
        """
        blocked = {"cap", "consumed"} & changes.keys()
        if blocked:
            routes = {"cap": "apply_cap_extension", "consumed": "record_consumption"}
            detail = ", ".join(f"{name} via {routes[name]}" for name in sorted(blocked))
            raise ContractViolationError("evolve cannot replace cap or consumed; use " + detail)
        new_state = changes.get("state", self.state)
        new_escalation_id = _escalation_id_for_state(
            new_state, changes.get("escalation_id", self.escalation_id)
        )
        if new_state is AttemptState.RUNNING:
            changes["escalation_id"] = new_escalation_id
        _require_abandoned_has_escalation_id(new_state, new_escalation_id)
        changes.setdefault("checkpoint_seq", self.checkpoint_seq + 1)
        return replace(self, updated_at_ms=now_ms, **changes)

    def record_consumption(self, delta: CapConsumption, *, now_ms: int) -> Attempt:
        """Add burned budget to this attempt's running total."""
        return replace(
            self,
            consumed=self.consumed + delta,
            checkpoint_seq=self.checkpoint_seq + 1,
            updated_at_ms=now_ms,
        )

    def pause(self, *, now_ms: int, resume_token: str | None = None) -> Attempt:
        """Checkpoint an interrupted attempt so a later process can resume it."""
        if self.is_terminal:
            raise ContractViolationError(f"cannot pause a {self.state.value} attempt")
        return self.evolve(
            now_ms=now_ms,
            state=AttemptState.PAUSED,
            resume_token=self.resume_token if resume_token is None else resume_token,
        )

    def resume(self, *, now_ms: int) -> Attempt:
        """Pick a checkpointed attempt back up."""
        if not self.is_resumable:
            raise ContractViolationError(f"cannot resume a {self.state.value} attempt")
        return self.evolve(now_ms=now_ms, state=AttemptState.RUNNING, escalation_id=None)

    def apply_cap_extension(self, extension: CapExtension, *, now_ms: int) -> Attempt:
        """Grow this attempt's cap. Reachable only from an operator decision."""
        return replace(
            self,
            cap=self.cap.extend(extension),
            checkpoint_seq=self.checkpoint_seq + 1,
            updated_at_ms=now_ms,
        )


# --------------------------------------------------------------------------- #
# Escalation
# --------------------------------------------------------------------------- #


class EscalationReason(str, Enum):  # noqa: UP042
    """Why the loop stopped and asked a human."""

    CAP_EXHAUSTED = "cap_exhausted"
    NO_VIABLE_APPROACH = "no_viable_approach"
    HARNESS_FAILURE = "harness_failure"
    VERIFIER_UNRUNNABLE = "verifier_unrunnable"
    REPEATED_REGRESSION = "repeated_regression"


class EscalationVerdict(str, Enum):  # noqa: UP042
    """The operator's fixed reply vocabulary. Exactly three values, forever."""

    CONTINUE = "continue"
    ABANDON = "abandon"
    EXTEND_CAP = "extend_cap"


@dataclass(frozen=True, slots=True)
class EscalationRequest:
    """The agent asking a human to decide, because it may not decide itself.

    Information flows *outward* freely here — the operator needs enough context
    to choose. ``summary`` is the agent's own account of where it got stuck,
    and ``best_result`` is the best verified score so far. The asymmetry is
    deliberate: rich request, three-valued reply. See
    :class:`EscalationDecision`.

    Each request counts once toward the round's human-gate load, which is
    driving function #4 and the number the self-improvement claim lives or dies
    on.
    """

    request_id: str
    problem_id: str
    attempt_id: str
    round_id: str
    reason: EscalationReason
    summary: str
    cap: Cap
    consumed: CapConsumption
    created_at_ms: int
    best_result: VerificationResult | None = None


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    """The operator's reply: a decision, never advice.

    **There is no free-text field, and adding one would invalidate the
    experiment.** Guidance like "try gradient boosting on that one" makes the
    operator the improvement mechanism: the round's gain then reflects the
    human's ML knowledge rather than the scaffold's, the next round's delta is
    confounded, and human-gate load stops being a measure of autonomy. It is
    the ``skillify`` confound arriving through another door. The only content
    permitted alongside the verdict is :class:`CapExtension` — pure numbers,
    carrying no information about *how* to spend them.

    ``slots=True`` is load-bearing rather than cosmetic: it means an advice
    string cannot be smuggled in by attaching an attribute at runtime either.
    """

    request_id: str
    verdict: EscalationVerdict
    decided_at_ms: int
    cap_extension: CapExtension | None = None

    def __post_init__(self) -> None:
        if self.verdict is EscalationVerdict.EXTEND_CAP and self.cap_extension is None:
            raise EscalationProtocolError("EXTEND_CAP requires a cap_extension")
        if self.verdict is not EscalationVerdict.EXTEND_CAP and self.cap_extension is not None:
            raise EscalationProtocolError(
                f"{self.verdict.value} must not carry a cap_extension; a budget change "
                "is only ever an EXTEND_CAP decision"
            )

    def apply_to(self, cap: Cap) -> Cap:
        """Resolve this decision against a cap.

        ``CONTINUE`` leaves the cap alone, ``EXTEND_CAP`` grows it, and
        ``ABANDON`` has no cap answer at all — the caller must terminate the
        attempt instead of asking what budget remains.
        """
        if self.verdict is EscalationVerdict.ABANDON:
            raise EscalationProtocolError("ABANDON ends the attempt; it does not yield a cap")
        if self.verdict is EscalationVerdict.EXTEND_CAP:
            assert self.cap_extension is not None  # guaranteed by __post_init__
            return cap.extend(self.cap_extension)
        return cap


# --------------------------------------------------------------------------- #
# Round records — the four driving-function numbers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EngineIdentity:
    """Which engine produced a round, pinned for lineage.

    ``driving-functions.md`` requires every round to record what it was
    measured with. The brief pins one engine family (Claude only) precisely so
    that a round cannot shift its local/cloud mix and produce a delta unrelated
    to the scaffold — but the tiering (orchestrator vs. mundane sub-steps) can
    still move, so it is recorded. ``scaffold_git_sha`` is the ``turing-skills``
    commit: under the "skills *are* the scaffold" decision, that SHA *is* the
    scaffold version, a diff is the answer to "what changed", and ``git
    revert`` is rollback.
    """

    backend: str
    orchestrator_model: str
    substep_model: str | None
    scaffold_git_sha: str


@dataclass(frozen=True, slots=True)
class RoundCost:
    """Driving function #3's numerator.

    Wall-clock here is **machine time**: solver steps, verification, workspace
    copy. Time spent suspended on an operator decision is excluded — that wait
    is driving function #4 (human-gate load). Folding it into this number
    would make cost-per-point improve whenever the operator was interrupted
    less, which is the confound #4 exists to detect.

    There is deliberately no dollar field: dollar metering was retired, and
    reintroducing it here would quietly resurrect the ``BudgetGate`` semantics
    the cap replaced.
    """

    wall_clock_seconds: float
    tokens: int
    attempts: int


@dataclass(frozen=True, slots=True)
class TypeScore:
    """Driving function #1 for one ``(problem_type, split)`` cell.

    A cell, not a corpus-wide number. Scores from different problem types live
    on different scales and are never pooled; scores from different splits are
    kept apart so held-out results can be withheld from the loop-2 self-edit
    summary without losing them from the report.
    """

    problem_type: ProblemType
    split: Split
    scores: Mapping[str, float]
    correctness_passes: int

    def __post_init__(self) -> None:
        if not self.scores:
            raise ContractViolationError("a TypeScore cell with no problems measures nothing")
        if not 0 <= self.correctness_passes <= len(self.scores):
            raise ContractViolationError(
                f"correctness_passes={self.correctness_passes} out of range for "
                f"{len(self.scores)} problems"
            )
        object.__setattr__(self, "scores", MappingProxyType(dict(self.scores)))

    @property
    def n(self) -> int:
        return len(self.scores)

    @property
    def mean_score(self) -> float:
        """Mean *within* this cell. Never averaged with another cell's mean."""
        return sum(self.scores.values()) / len(self.scores)

    @property
    def correctness_pass_rate(self) -> float:
        return self.correctness_passes / len(self.scores)


@dataclass(frozen=True, slots=True)
class RoundDelta:
    """Driving functions #2 and #3 for one ``(problem_type, split)`` cell.

    ``marginal_gain`` is only meaningful in units of ``noise_floor``, which is
    why the two travel together: "round 4 improved by 0.8" is an
    uninterpretable statement on its own, and the noise floor has first claim
    on an opportunistic budget for exactly that reason.

    ``cost_per_unit_gain`` is ``None`` when the gain is not positive — the
    division is undefined and a sentinel number would be read as a measurement.
    """

    problem_type: ProblemType
    split: Split
    marginal_gain: float
    noise_floor: float
    cost_per_unit_gain: float | None = None

    def __post_init__(self) -> None:
        if self.noise_floor < 0:
            raise ContractViolationError("noise floor cannot be negative")

    @property
    def beats_noise_floor(self) -> bool:
        """Saturation is ``marginal_gain`` dropping below the seed-noise floor."""
        return self.marginal_gain > self.noise_floor

    @property
    def gain_in_noise_units(self) -> float | None:
        if self.noise_floor == 0:
            return None
        return self.marginal_gain / self.noise_floor


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """One row of the loop's trajectory — the four numbers, plus lineage.

    Driving functions, in order: :attr:`type_scores` (primary, continuous, per
    cell), :attr:`deltas` (marginal round gain and cost per unit gain, both in
    units of the noise floor), :attr:`cost`, and :attr:`escalation_count`
    (human-gate load).

    **There is no aggregate score attribute and there must never be one.**
    Averaging a speedup ratio against a leaderboard percentile produces a
    number that moves for reasons nobody can attribute, and a round that helps
    one family while hurting another reads as flat. Use
    :meth:`primary_scores`, which returns the cells.

    Lineage is mandatory, not optional metadata: ``parent_round_id`` because
    accumulate-vs-replace and retrain-from-base-vs-stack are different
    experiments that get confused constantly when lineage is implicit, and
    ``eval_set_hash`` because rounds measured on different eval sets may not be
    compared at all — see :meth:`assert_comparable_to`.
    """

    round_index: int
    run_id: str
    parent_round_id: str | None
    eval_set_hash: str
    engine: EngineIdentity
    type_scores: tuple[TypeScore, ...]
    deltas: tuple[RoundDelta, ...]
    cost: RoundCost
    escalation_count: int
    created_at_ms: int
    gates: Mapping[str, bool] = field(default_factory=dict)
    verdict: str = ""

    def __post_init__(self) -> None:
        if self.round_index < 0:
            raise ContractViolationError("round_index cannot be negative")
        if self.round_index == 0 and self.parent_round_id is not None:
            raise ContractViolationError("round 0 is the baseline and has no parent")
        if self.round_index > 0 and self.parent_round_id is None:
            raise ContractViolationError(
                f"round {self.round_index} must record its parent; a trajectory without "
                "lineage cannot distinguish accumulate from replace"
            )
        if self.escalation_count < 0:
            raise ContractViolationError("escalation_count cannot be negative")
        if not self.eval_set_hash:
            raise ContractViolationError(
                "eval_set_hash is required; silent eval drift fakes curves"
            )
        object.__setattr__(self, "type_scores", tuple(self.type_scores))
        object.__setattr__(self, "deltas", tuple(self.deltas))
        object.__setattr__(self, "gates", MappingProxyType(dict(self.gates)))
        self._reject_duplicate_cells(self.type_scores)
        self._reject_duplicate_cells(self.deltas)

    @staticmethod
    def _reject_duplicate_cells(cells: Iterable[TypeScore | RoundDelta]) -> None:
        seen: set[tuple[ProblemType, Split]] = set()
        for cell in cells:
            key = (cell.problem_type, cell.split)
            if key in seen:
                raise ContractViolationError(
                    f"duplicate cell for {key[0].value}/{key[1].value}; each "
                    "(type, split) is reported exactly once"
                )
            seen.add(key)

    def primary_scores(self) -> Mapping[tuple[ProblemType, Split], float]:
        """Driving function #1, as cells. There is no corpus-wide mean by design."""
        return MappingProxyType(
            {(ts.problem_type, ts.split): ts.mean_score for ts in self.type_scores}
        )

    def score_for(self, problem_type: ProblemType, split: Split) -> TypeScore | None:
        for ts in self.type_scores:
            if ts.problem_type is problem_type and ts.split is split:
                return ts
        return None

    def delta_for(self, problem_type: ProblemType, split: Split) -> RoundDelta | None:
        for d in self.deltas:
            if d.problem_type is problem_type and d.split is split:
                return d
        return None

    def scores_for_split(self, split: Split) -> tuple[TypeScore, ...]:
        return tuple(ts for ts in self.type_scores if ts.split is split)

    def self_edit_visible_scores(self) -> tuple[TypeScore, ...]:
        """The only cells a loop-2 self-edit summary may read: practice only.

        **Loop-2 seam. Not used in loop 1** — nothing self-edits yet — but the
        filter lives here so the summariser cannot be written without it. Held-
        out cells are dropped by construction: if the agent could see them, it
        would edit its skills against the same problems it is re-scored on and
        "got better at ML research" would be indistinguishable from "memorised
        the corpus". The practice-vs-held-out gap is the evidence of
        memorisation, and it only exists if the held-out side stays unseen.
        """
        return self.scores_for_split(Split.PRACTICE)

    def assert_comparable_to(self, other: RoundRecord) -> None:
        """Raise unless two rounds may sit on the same axes.

        Rounds measured on different eval sets are not comparable at any level
        of care; if the eval set changes, the trajectory restarts.
        """
        if self.eval_set_hash != other.eval_set_hash:
            raise EvalSetMismatchError(
                f"round {self.run_id} was measured on eval set {self.eval_set_hash!r} and "
                f"round {other.run_id} on {other.eval_set_hash!r}; the trajectory restarts "
                "when the eval set changes"
            )

    def differs_in_engine_from(self, other: RoundRecord) -> bool:
        """True when engine identity moved between rounds — a reportable confound."""
        return self.engine != other.engine


def round_cells(records: Sequence[RoundRecord]) -> tuple[tuple[ProblemType, Split], ...]:
    """Every ``(type, split)`` cell appearing across ``records``, in first-seen order.

    Convenience for trajectory rendering; deliberately returns cells rather
    than any pooled figure.
    """
    seen: list[tuple[ProblemType, Split]] = []
    for record in records:
        for ts in record.type_scores:
            key = (ts.problem_type, ts.split)
            if key not in seen:
                seen.append(key)
    return tuple(seen)
