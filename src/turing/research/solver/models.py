"""Value types for one within-project attempt.

Everything here is a frozen dataclass, for the same reason
:class:`turing.research.contracts.Attempt` is: an attempt is a chain of
checkpoints, and a mutable record is one that can be half-written when the
subscription window closes.

The types split into three groups:

* **What the model proposes** — :class:`FileEdit`, :class:`Proposal`, and the
  read-only :class:`ProposalContext` it is derived from.
* **What one iteration did** — :class:`IterationRecord` and its
  :class:`IterationPhase`, which together are the journal that makes resume
  exact rather than approximate.
* **How the loop decides** — :class:`SolverPolicy`, :class:`ProgressSummary`,
  and the :class:`SolverOutcome` a run returns.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from turing.research.contracts import AttemptState
from turing.research.solver.errors import CheckpointError, ProposalError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from turing.research.contracts import (
        Attempt,
        CapConsumption,
        EscalationRequest,
        ProblemType,
        VerificationResult,
    )

__all__ = [
    "CommandOutcome",
    "FileEdit",
    "IterationPhase",
    "IterationRecord",
    "IterationSummary",
    "ProgressSummary",
    "Proposal",
    "ProposalContext",
    "SolverOutcome",
    "SolverPolicy",
    "better_result",
    "summarise_progress",
]


# --------------------------------------------------------------------------- #
# What the model proposes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FileEdit:
    """One file the model wants written, given in full.

    Full contents rather than a patch on purpose: a full-content write is
    **idempotent**, and idempotent application is what makes "resume must not
    re-apply the last change" a property of the data rather than a property of
    the control flow. A crash between "wrote file A" and "wrote file B" is
    repaired by simply applying the whole proposal again.

    ``relative_path`` is relative to the attempt's workspace and is validated
    at apply time, not here — the check needs the concrete workspace root to
    resolve symlinks against. See
    :meth:`turing.research.solver.workspace.Workspace.resolve`.
    """

    relative_path: str
    content: str

    def __post_init__(self) -> None:
        if not self.relative_path.strip():
            raise ProposalError("a file edit needs a non-empty relative path")


@dataclass(frozen=True, slots=True)
class Proposal:
    """One iteration's worth of change, as returned by a model backend.

    ``no_viable_approach`` is the model saying it sees no way forward. It is
    **advisory and cannot end the attempt**: the solver turns it into an
    :class:`~turing.research.contracts.EscalationRequest` with reason
    ``NO_VIABLE_APPROACH`` and suspends. There is no field here, and no
    combination of fields, that reaches a terminal state — the agent may not
    quit a project it judges hopeless, it escalates.

    ``tokens`` is the backend's own usage report for the call that produced
    this proposal. The solver adds it to
    :attr:`turing.research.contracts.Attempt.consumed` in the same transaction
    that journals the proposal, which is what makes token accounting
    exactly-once across a crash.
    """

    proposal_id: str
    rationale: str = ""
    edits: tuple[FileEdit, ...] = ()
    commands: tuple[str, ...] = ()
    tokens: int = 0
    no_viable_approach: bool = False

    def __post_init__(self) -> None:
        if not self.proposal_id:
            raise ProposalError("proposal_id must be non-empty; it is the journal's identity")
        if self.tokens < 0:
            raise ProposalError("token usage cannot be negative")
        object.__setattr__(self, "edits", tuple(self.edits))
        object.__setattr__(self, "commands", tuple(self.commands))
        if self.no_viable_approach and (self.edits or self.commands):
            raise ProposalError(
                "a no_viable_approach proposal must not also change the workspace; "
                "the workspace state at escalation time would be ambiguous"
            )
        seen: set[str] = set()
        for edit in self.edits:
            if edit.relative_path in seen:
                raise ProposalError(
                    f"duplicate edit for {edit.relative_path!r}; a proposal must be "
                    "idempotent, and two writes to one path are order-dependent"
                )
            seen.add(edit.relative_path)

    def digest(self) -> str:
        """Stable hash of the change this proposal makes.

        Used for logging and for asserting in tests that a resumed iteration
        re-applies *the same* change rather than a freshly generated one.
        """
        h = hashlib.sha256()
        for edit in self.edits:
            h.update(edit.relative_path.encode("utf-8"))
            h.update(b"\x00")
            h.update(edit.content.encode("utf-8"))
            h.update(b"\x00")
        for command in self.commands:
            h.update(command.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()


@dataclass(frozen=True, slots=True)
class IterationSummary:
    """One past iteration, as the model is allowed to see it."""

    iteration_index: int
    rationale: str
    score: float | None
    passed_correctness: bool | None
    detail: str = ""
    command_exit_codes: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ProposalContext:
    """Everything a backend is given to propose the next change.

    **Note what is absent: the** :class:`~turing.research.contracts.Problem`
    **and therefore the** :class:`~turing.research.contracts.Verifier`. The
    context carries the goal, the workspace, and the *results* of verification
    — never a handle to the object that produces them. Contracts already make
    a verifier immutable in three ways; withholding the reference entirely
    means the strongest of those defences never has to fire. A test asserts
    this class grows no field that reintroduces it.

    ``remaining`` is here so the backend can size its own call. The solver
    guarantees it never *starts* an iteration with an exhausted cap, but it
    cannot know a response's token count before the response exists — bounding
    a single call is the backend's job, and this is the number it needs.
    """

    problem_id: str
    problem_type: ProblemType
    goal: str
    score_scale: str
    workspace: Path
    iteration_index: int
    seed: int
    remaining: CapConsumption
    history: tuple[IterationSummary, ...] = ()
    last_result: VerificationResult | None = None
    best_result: VerificationResult | None = None


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Result of one command a proposal asked to run inside the workspace."""

    command: str
    exit_code: int
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_seconds: float = 0.0


# --------------------------------------------------------------------------- #
# What one iteration did
# --------------------------------------------------------------------------- #


class IterationPhase(str, Enum):  # noqa: UP042
    """How far through an iteration the journal got before the last checkpoint.

    This is the whole of resume. An interruption lands in exactly one of these
    phases, and each one has a single unambiguous continuation:

    * ``PROPOSED`` — the model has answered and its tokens are already
      charged. Re-apply the journalled proposal (idempotent) and verify. **The
      backend is not called again**, so an interruption here does not spend
      the same tokens twice.
    * ``APPLIED`` — the workspace is up to date. Verify only; do not re-apply.
    * ``VERIFIED`` — the iteration is closed. Start the next one.
    """

    PROPOSED = "proposed"
    APPLIED = "applied"
    VERIFIED = "verified"


_PHASE_ORDER: dict[IterationPhase, int] = {
    IterationPhase.PROPOSED: 0,
    IterationPhase.APPLIED: 1,
    IterationPhase.VERIFIED: 2,
}


@dataclass(frozen=True, slots=True)
class IterationRecord:
    """The journal entry for one iteration.

    Written three times — once per phase — always in the same transaction as
    the updated :class:`~turing.research.contracts.Attempt`. That pairing is
    the exactly-once guarantee: budget is charged if and only if the phase
    that spent it is durable.

    The full :class:`Proposal` is stored rather than a reference to it because
    resume must be able to finish applying a change without asking the model
    again.
    """

    attempt_id: str
    iteration_index: int
    phase: IterationPhase
    proposal: Proposal
    started_at_ms: int
    updated_at_ms: int
    applied_paths: tuple[str, ...] = ()
    command_outcomes: tuple[CommandOutcome, ...] = ()
    result: VerificationResult | None = None
    wall_clock_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.iteration_index < 0:
            raise CheckpointError("iteration_index cannot be negative")
        object.__setattr__(self, "applied_paths", tuple(self.applied_paths))
        object.__setattr__(self, "command_outcomes", tuple(self.command_outcomes))

    def advance(self, phase: IterationPhase, *, now_ms: int, **changes: Any) -> IterationRecord:
        """Move the journal forward one or more phases.

        Refuses to move backwards. A resume bug that replayed a phase would
        otherwise silently double-charge, which is the exact failure this
        module exists to prevent — so it is loud instead.
        """
        if _PHASE_ORDER[phase] <= _PHASE_ORDER[self.phase]:
            raise CheckpointError(
                f"iteration {self.iteration_index} cannot move from {self.phase.value} "
                f"to {phase.value}; the journal only ever advances"
            )
        return replace(self, phase=phase, updated_at_ms=now_ms, **changes)

    def summarise(self) -> IterationSummary:
        return IterationSummary(
            iteration_index=self.iteration_index,
            rationale=self.proposal.rationale,
            score=None if self.result is None else self.result.score,
            passed_correctness=None if self.result is None else self.result.passed_correctness,
            detail="" if self.result is None else self.result.detail,
            command_exit_codes=tuple(c.exit_code for c in self.command_outcomes),
        )


# --------------------------------------------------------------------------- #
# How the loop decides
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SolverPolicy:
    """The knobs on the refinement loop. Frozen per attempt.

    ``target_score`` is what "the verifier is satisfied" means for this
    problem, on that problem's own scale. **When it is ``None`` the attempt
    can never reach** :attr:`~turing.research.contracts.AttemptState.PASSED`
    — it refines until the cap trips, which is the honest reading of a
    continuous score: there is no point at which a speedup ratio is "done".
    Problems that want a pass/fail terminal state supply the number.

    ``patience`` and ``regression_patience`` are *escalation* triggers, never
    termination triggers. Running out of patience means the solver judges
    further refinement unproductive, and a project it judges hopeless goes to
    the operator — it does not stop.

    ``escalate_on_cap_exhausted`` defaults to ``True`` so that a tripped cap
    reaches a human who may grant ``extend_cap``. Setting it ``False`` makes a
    tripped cap land directly in
    :attr:`~turing.research.contracts.AttemptState.FAILED_WITHIN_CAP`, which
    the brief names a first-class logged outcome. That is the setting to use
    when measuring driving function #4: with ``True``, every attempt that runs
    out of budget contributes an escalation, and "human-gate load trending to
    zero" is then partly a statement about cap sizing rather than about
    autonomy.
    """

    target_score: float | None = None
    patience: int | None = None
    regression_patience: int | None = None
    escalate_on_cap_exhausted: bool = True
    require_correctness: bool = True

    def __post_init__(self) -> None:
        if self.patience is not None and self.patience < 1:
            raise ProposalError("patience must be at least one iteration")
        if self.regression_patience is not None and self.regression_patience < 1:
            raise ProposalError("regression_patience must be at least one iteration")

    def satisfied_by(self, result: VerificationResult | None) -> bool:
        """Whether this result clears the frozen bar.

        Correctness gates the score, never the other way round: contracts keep
        ``passed_correctness`` separate from ``score`` precisely so a
        fast-but-wrong solution cannot be ranked above a slow-but-right one.
        A harness-failure result is not a grade and cannot satisfy the bar,
        even if it claims correctness and a passing score.
        """
        grade = _usable_result(result)
        if grade is None:
            return False
        if self.require_correctness and not grade.passed_correctness:
            return False
        if self.target_score is None:
            return False
        return grade.score >= self.target_score


@dataclass(frozen=True, slots=True)
class ProgressSummary:
    """Trailing progress, recomputed from the journal rather than remembered.

    Recomputed on purpose: a resumed run must reach the same patience decision
    a run that was never interrupted would have reached, and the only state
    that survives a crash is the journal.
    """

    best: VerificationResult | None
    stagnant_iterations: int
    consecutive_regressions: int
    verified_iterations: int


def _usable_result(result: VerificationResult | None) -> VerificationResult | None:
    """Drop a harness-failure result — it is not a grade."""
    if result is None or result.harness_failed:
        return None
    return result


def _strictly_better(candidate: VerificationResult, current: VerificationResult) -> bool:
    if candidate.passed_correctness != current.passed_correctness:
        return candidate.passed_correctness
    return candidate.score > current.score


def better_result(
    current: VerificationResult | None, candidate: VerificationResult | None
) -> VerificationResult | None:
    """Pick the better of two results — correctness first, then score.

    Ties keep ``current``, so the earliest result achieving a score is the one
    reported. That matters for cost accounting: crediting a later, equal
    result would overstate what the extra iterations bought.

    A harness-failure result is not a grade and never wins, even as the first
    (or only) candidate. A later real grade replaces a tainted ``current``.
    """
    current = _usable_result(current)
    candidate = _usable_result(candidate)
    if candidate is None:
        return current
    if current is None:
        return candidate
    return candidate if _strictly_better(candidate, current) else current


def summarise_progress(results: Sequence[VerificationResult]) -> ProgressSummary:
    """Fold a run's verified results into the numbers the policy reads.

    Harness-failure results are skipped: they are not verifications, and
    counting them as stagnation or as ``best`` would treat a broken instrument
    as a grade.
    """
    best: VerificationResult | None = None
    stagnant = 0
    regressions = 0
    previous: VerificationResult | None = None
    grades = 0
    for result in results:
        grade = _usable_result(result)
        if grade is None:
            continue
        grades += 1
        if best is None or _strictly_better(grade, best):
            best = grade
            stagnant = 0
        else:
            stagnant += 1
        if previous is not None and grade.score < previous.score:
            regressions += 1
        else:
            regressions = 0
        previous = grade
    return ProgressSummary(
        best=best,
        stagnant_iterations=stagnant,
        consecutive_regressions=regressions,
        verified_iterations=grades,
    )


@dataclass(frozen=True, slots=True)
class SolverOutcome:
    """What one call to :meth:`turing.research.solver.Solver.run` produced.

    A run ends in one of four shapes, and the caller distinguishes them from
    :attr:`attempt`'s state rather than from a bespoke enum:

    * **terminal** — ``PASSED``, ``FAILED_WITHIN_CAP`` or ``ABANDONED``.
    * **suspended** — ``ESCALATED``: a human has been asked and has not
      answered. Call ``run`` again later; it re-polls the same request.
    * **paused** — ``PAUSED``: the window closed. Call ``run`` again; the
      journal resumes mid-iteration.
    * neither, only if a caller stops the loop by other means.
    """

    attempt: Attempt
    iterations_completed: int
    best_result: VerificationResult | None = None
    escalation: EscalationRequest | None = None

    @property
    def state(self) -> AttemptState:
        return self.attempt.state

    @property
    def passed(self) -> bool:
        return self.attempt.state is AttemptState.PASSED

    @property
    def suspended(self) -> bool:
        """Waiting on an operator decision. Not terminal, not resumable alone."""
        return self.attempt.state is AttemptState.ESCALATED

    @property
    def paused(self) -> bool:
        return self.attempt.state is AttemptState.PAUSED

    @property
    def terminal(self) -> bool:
        return self.attempt.is_terminal
