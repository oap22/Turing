"""``RoundRunner`` — one round of the corpus, fully unattended.

A round is: every problem in the corpus, one attempt each, verified by the
frozen verifier, reduced to per-``(type, split)`` cells, and written out as a
:class:`~turing.research.contracts.RoundRecord` plus a row in
``trajectory.json``. **This module is the experiment's instrument — if it is
wrong, every result is wrong**, so the invariants it enforces are listed here
and each one is a test.

**The runner owns what the solver must not.**

* *The cap.* One step is charged per :meth:`Solver.step` call regardless of
  what the solver reports, so the runaway brake works against a buggy solver.
  Wall-clock is measured on the runner's clock around the step *and* the
  verifier call — the solver's self-report is not what the cap reads, and
  harness time is not free. Time blocked on an operator decision is not
  charged (that wait is human-gate load, not budget). The cap is rechecked
  after every charge, before verification and before a pass or escalation is
  recorded, so an over-budget step cannot still land as ``PASSED``.
* *The verifier.* The runner calls it; the solver never holds it. The latest
  usable result is fed back through ``attempt.result`` as read-only context,
  which is the brief's "iterate against the frozen verifier" — reading the
  bar, never editing it. The reported score is that result, not a max over
  every verify: max-of-N on a timing harness grows with the cap at zero
  capability change.
* *Termination.* A solver can request an escalation; this call-site guard
  has no way to quit. :meth:`~turing.research.contracts.Attempt.evolve` can
  still reach ``ABANDONED`` with a leftover ``escalation_id``.

**Cap exhaustion terminates, it does not escalate** (unless
``escalate_on_cap_exhaustion`` is set). "Failed within cap" is a first-class,
logged outcome. Escalating every cap trip would pin human-gate load at one per
problem per round forever, and human-gate load — driving function #4 — is the
number the self-improvement claim lives or dies on. It must be free to fall to
zero.

**A binary state is not the measurement.** ``PASSED`` / ``FAILED_WITHIN_CAP``
records whether a binary bar was met, where one was declared. The reported
number is the continuous score, which exists either way.

**Scope: loop 1.** Nothing here self-edits. The single sanctioned path from a
round's results into a future self-edit summary is
:mod:`turing.research.loop.self_edit_seam`, which this module never calls.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

import structlog

from turing.research.contracts import (
    Attempt,
    AttemptState,
    CapConsumption,
    ContractViolationError,
    EscalationProtocolError,
    EscalationReason,
    EscalationRequest,
    EscalationVerdict,
    RoundCost,
    RoundRecord,
)
from turing.research.loop import integrity
from turing.research.loop.metrics import (
    DEFAULT_SCORE_FLOORS,
    CostBasis,
    ScoredProblem,
    assess_saturation,
    build_type_scores,
    compute_deltas,
    floors_by_cell,
    round_verdict,
)
from turing.research.loop.plots import PLOT_FILENAMES, render_attempt_plots, render_round_plot
from turing.research.loop.protocols import SolverTask, SystemClock
from turing.research.loop.results import (
    MetricsLine,
    MetricsWriter,
    Outcome,
    ProgressTracker,
    read_agent_diagnostics,
    read_metrics_points,
    usable_target,
    write_attempt_summary,
    write_round_summary,
    write_viewer_config,
)
from turing.research.loop.trajectory import AttemptLog, StepLog, cell_key
from turing.research.problems.adapter import bind_eval_set_hash

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from turing.research.contracts import (
        Cap,
        EngineIdentity,
        EscalationDecision,
        Problem,
        TypeScore,
        VerificationResult,
    )
    from turing.research.loop.metrics import NoiseFloor, SaturationAssessment
    from turing.research.loop.noise_floor import NoiseFloorReport
    from turing.research.loop.protocols import (
        Clock,
        EscalationChannel,
        Solver,
        WorkspaceProvider,
    )
    from turing.research.loop.trajectory import TrajectoryStore

logger = structlog.get_logger(__name__)

__all__ = [
    "AttemptFailure",
    "AttemptOutcome",
    "PassCriterion",
    "RoundConfig",
    "RoundOutcome",
    "RoundRunner",
]


@dataclass(frozen=True, slots=True)
class PassCriterion:
    """When a continuous score counts as "the verifier passed".

    Optional per problem. With **no** criterion an attempt runs until the cap
    and lands in ``FAILED_WITHIN_CAP`` holding its best score — which is the
    brief's solver shape ("build a solution, score it, revise, repeat until the
    cap") and is not a failure in the ordinary sense. Correctness alone is a
    poor default bar: a speedup problem's untouched baseline is already
    correct, so an attempt would "pass" before doing any work.
    """

    min_score: float | None = None
    require_correctness: bool = True

    def __post_init__(self) -> None:
        if self.min_score is None and not self.require_correctness:
            raise ContractViolationError(
                "a pass criterion that requires neither correctness nor a score is not a "
                "bar; omit the criterion instead and let the attempt run to its cap"
            )

    def satisfied_by(self, result: VerificationResult) -> bool:
        if self.require_correctness and not result.passed_correctness:
            return False
        if self.min_score is None:
            return self.require_correctness
        return result.score >= self.min_score


@dataclass(frozen=True, slots=True)
class RoundConfig:
    """Everything one round needs that is not the corpus.

    ``run_id`` doubles as the round id: attempts record it as their
    ``round_id`` and the next round records it as ``parent_round_id``, so
    lineage lives in one namespace.
    """

    round_index: int
    run_id: str
    parent_round_id: str | None
    #: Empty means derive from the corpus at :meth:`RoundRunner.run_round`.
    #: A non-empty value is verified against that fingerprint, never trusted.
    eval_set_hash: str
    engine: EngineIdentity
    seed: int
    default_cap: Cap
    cost_basis: CostBasis = CostBasis.WALL_CLOCK
    escalate_on_cap_exhaustion: bool = False
    #: Caps how many times a tripped cap may escalate when
    #: ``escalate_on_cap_exhaustion`` is set. It does **not** override an
    #: operator ``CONTINUE`` — that would be a self-quit, and it would
    #: mislabel the stop as ``FAILED_WITHIN_CAP`` with the cap untouched.
    max_escalations_per_attempt: int = 3
    pass_criteria: Mapping[str, PassCriterion] = field(default_factory=dict)
    #: What an unscored problem's cell entry is floored to, by score scale.
    #: Defaults to :data:`~turing.research.loop.metrics.DEFAULT_SCORE_FLOORS`
    #: (``speedup_ratio`` and ``leaderboard_percentile`` only); an override
    #: here *replaces* that table rather than extending it, unchanged from
    #: before a problem could declare its own floor.
    #:
    #: A scale absent from this table is not automatically refused: a problem
    #: whose verifier declares
    #: :attr:`~turing.research.contracts.Verifier.score_floor` still scores,
    #: because ``ScoredProblem.from_result`` falls back to that declaration
    #: when this table has no entry for the scale. This table's entries take
    #: precedence when both exist — an override here is an explicit operator
    #: choice for *this round* and must be able to shadow whatever a verifier
    #: declared. See
    #: :data:`~turing.research.loop.metrics.DEFAULT_SCORE_FLOORS` for the full
    #: precedence order.
    score_floors: Mapping[str, float] = field(default_factory=lambda: DEFAULT_SCORE_FLOORS)
    #: When False, skip per-step verification and run the frozen verifier once
    #: when the cap trips. Never verifying floors every cell and fabricates a
    #: parent-round delta.
    verify_every_step: bool = True

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
        if self.max_escalations_per_attempt < 1:
            raise ContractViolationError("an attempt must be allowed at least one escalation")
        object.__setattr__(self, "pass_criteria", MappingProxyType(dict(self.pass_criteria)))
        object.__setattr__(self, "score_floors", MappingProxyType(dict(self.score_floors)))

    def criterion_for(self, problem_id: str) -> PassCriterion | None:
        return self.pass_criteria.get(problem_id)


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """One problem's result, and how it got there."""

    problem: Problem
    attempt: Attempt
    best_result: VerificationResult | None
    steps: tuple[StepLog, ...]
    escalations: tuple[EscalationRequest, ...]
    decisions: tuple[EscalationDecision, ...]
    attempt_log_path: Path | None = None

    @property
    def escalation_count(self) -> int:
        return len(self.escalations)


@dataclass(frozen=True, slots=True)
class AttemptFailure:
    """One problem whose attempt raised instead of returning an outcome.

    Deliberately **not** an :class:`AttemptOutcome`, and deliberately carrying
    neither an :class:`~turing.research.contracts.Attempt` nor a score. An
    attempt that raised out of :meth:`RoundRunner.run_attempt` has no
    trustworthy terminal state and no result — the exception can come from
    anywhere between the first metrics line and ``write_attempt_log`` — so
    there is no honest value to put in either field. Anything with a score
    slot on it will eventually be read as a score; the type that cannot hold
    one cannot be misread. See :meth:`RoundRunner.run_attempts` for why these
    are kept out of the scored set entirely rather than floored into it.
    """

    problem_id: str
    #: ``"<ExcType>: <message>"``. A string, not the exception: a
    #: :class:`RoundOutcome` is a value the caller may hold, serialise or log
    #: long after the round, and keeping a live exception here would keep its
    #: whole traceback — and every frame's locals, including workspace paths
    #: and solver state — alive with it. The traceback that matters is already
    #: in the ``research.attempt.crashed`` log entry.
    error: str


@dataclass(frozen=True, slots=True)
class RoundOutcome:
    """The round record plus everything that produced it.

    ``attempts`` holds only the attempts that produced an outcome. Problems
    lost to a raising attempt are in ``failures`` and are absent from
    ``scored``, from ``record.type_scores`` and from ``record.cost`` — see
    :meth:`RoundRunner.run_attempts`.
    """

    record: RoundRecord
    attempts: tuple[AttemptOutcome, ...]
    scored: tuple[ScoredProblem, ...]
    assessments: tuple[SaturationAssessment, ...]
    trajectory_row: Mapping[str, Any] | None = None
    failures: tuple[AttemptFailure, ...] = ()


def _is_better(new: VerificationResult, old: VerificationResult | None) -> bool:
    """Whether ``new`` should replace the attempt's reported result.

    A harness-failure result is not a grade and must never become ``best``.
    Correctness dominates: a fast wrong answer never replaces a correct one.
    Among results with the same correctness, the **latest** workspace wins —
    not the higher score. Max-of-N over a timing harness grows with the cap
    at zero capability change, and ``extend_cap`` would couple an operator
    decision to the primary score.
    """
    if new.harness_failed:
        return False
    if old is None:
        return True
    if new.passed_correctness != old.passed_correctness:
        return new.passed_correctness
    return True


def _spend_carried_on_error(exc: BaseException) -> CapConsumption:
    """Accounted backend spend attached to a step that then raised.

    Tokens come from the error's accounted ``usage`` or ``tokens``, never
    from model-authored JSON. A proposal call that happened still costs a
    step even when parse/apply then failed.
    """
    usage = getattr(exc, "usage", None)
    if usage is not None:
        accounted = getattr(usage, "total_tokens", 0)
        tokens = int(accounted) if accounted else 0
        return CapConsumption(steps=1, tokens=max(0, tokens))
    carried = getattr(exc, "tokens", None)
    if isinstance(carried, int) and carried > 0:
        return CapConsumption(steps=1, tokens=carried)
    return CapConsumption()


#: The hash-chained trio :class:`~turing.research.loop.results.MetricsWriter`
#: refuses to splice onto. Chained, so a re-drive cannot overwrite it in place
#: and has to move it aside instead.
_METRICS_TRIO = ("metrics.jsonl", "metrics.chain.json", "metrics.json")

#: Rotated aside with the trio, for a different reason. Checkpoints and the
#: attempt log really are wholesale overwrites that self-heal on a re-drive.
#: **The plots are not**, and the docstring here used to claim they were.
#: :func:`~turing.research.loop.plots.render_attempt_plots` *skips* — writes
#: nothing at all — when the series a plot needs is absent from every point,
#: so an attempt that dies before its first verification rewrites ``cap.svg``
#: (``tokens_used`` is on every line, including the terminal one) and leaves
#: the *previous* attempt's ``progress.svg`` sitting untouched beside it. The
#: attempt directory then holds two charts drawn from two different attempts,
#: and the desktop's images pane draws them side by side with nothing marking
#: one as stale — the exact failure rotation exists to prevent, displaced from
#: the JSONL to the SVG. Skipping rather than blanking is the right behaviour
#: for the renderer (an empty chart reads as "the run produced nothing"), so
#: the staleness has to be resolved here, at the one point that already knows
#: a new generation is starting.
#:
#: Named files rather than a glob over ``*.svg``: rotation must never sweep
#: aside a file the runner did not write.
_ROTATED_NAMES: tuple[str, ...] = (*_METRICS_TRIO, *PLOT_FILENAMES)

#: Matches the subdirectory names :func:`_rotate_stale_metrics` creates. Used
#: by :meth:`RoundRunner._viewer_runs` to tell a superseded chain from a live
#: one now that its walk is depth-independent; kept next to the function that
#: mints the names so the two cannot drift.
#:
#: Deliberately does **not** match :data:`_STAGING_DIR_NAME` (a leading dot,
#: no digits) — a leftover staging directory is a rotation-in-progress, not a
#: rotated generation, and must stay invisible to this filter until
#: :func:`_rotate_stale_metrics` has finished committing it.
_PRIOR_DIR_PATTERN = re.compile(r"prior-\d+")

#: The subdirectory :func:`_rotate_stale_metrics` stages the trio and plots
#: into before committing them, in one rename, to a numbered ``prior-N/``.
#: One fixed name rather than one per rotation: the call site is fully
#: synchronous (no ``await`` between staging and commit), so at most one
#: rotation is ever in flight for a given ``metrics_dir``, and a leftover
#: from an earlier crash is drained and committed — see
#: :func:`_commit_staged_rotation` — before a new one is ever staged, so the
#: name is always free by the time it would be reused. Leading dot so it
#: sorts away from ordinary metrics files and never collides with a
#: problem-id path segment (``contracts._reject_unsafe_problem_id`` already
#: refuses ids that could shadow ``prior-<digits>``; a dot-prefixed name
#: needs no matching refusal because no problem id may contain one).
_STAGING_DIR_NAME = ".rotating"


def _next_free_prior_suffix(metrics_dir: Path) -> int:
    """The smallest ``N >= 1`` for which ``metrics_dir / f"prior-{N}"`` is free."""
    suffix = 1
    while (metrics_dir / f"prior-{suffix}").exists():
        suffix += 1
    return suffix


def _commit_staged_rotation(metrics_dir: Path, staging_dir: Path) -> None:
    """Finish moving the trio/plots into ``staging_dir``, then commit it as ``prior-N/``.

    Called from two shapes of caller, and cannot (needs not) tell them apart:

    1. :func:`_rotate_stale_metrics`, right after creating ``staging_dir``
       empty — the ordinary rotation, nothing moved yet.
    2. :func:`_rotate_stale_metrics`'s self-heal, on a ``staging_dir`` a
       previous, crashed call already left behind holding *some* subset of
       :data:`_ROTATED_NAMES` — whatever it managed to move before it died.

    Either way the move loop below is the same: a name still sitting in
    ``metrics_dir`` gets moved in; a name already staged (case 2, partially)
    is simply absent from ``metrics_dir`` and skipped — ``Path.exists()`` on
    the source is enough to tell the two apart, no bookkeeping required. So a
    crash between individual file moves is always recoverable by calling this
    again with the same ``staging_dir``.

    **The final step is what makes the whole set atomic.** One
    ``Path.rename`` moves ``staging_dir`` onto a freshly chosen, currently
    non-existent ``prior-N/`` — the same directory-entry-swap primitive that
    already made each individual file move atomic, just applied one level up.
    An observer walking ``metrics_dir`` therefore never finds a ``prior-N/``
    holding some but not all of the files this rotation moved: either the
    entry does not exist yet (rotation still staging, or not yet started), or
    it exists complete (rotation committed). There is no third, half-written
    state to observe — which is the property three sequential per-file
    renames into a pre-existing ``prior-N/`` never had.
    """
    for name in _ROTATED_NAMES:
        src = metrics_dir / name
        if not src.exists():
            continue
        src.rename(staging_dir / name)
    suffix = _next_free_prior_suffix(metrics_dir)
    prior_dir = metrics_dir / f"prior-{suffix}"
    staging_dir.rename(prior_dir)
    logger.info(
        "research.results.metrics_rotated", staging_dir=str(staging_dir), dest=str(prior_dir)
    )


def _rotate_stale_metrics(metrics_dir: Path) -> None:
    """Move a prior attempt's chained trio — and its plots — aside, as one atomic set.

    A re-run of an attempt into an already-used ``output_dir`` is legitimate
    and reachable — a closed subscription window, a killed process, a
    re-driven round — and nothing in :meth:`RoundRunner.run_attempt` (or
    :meth:`RoundRunner.run_round`, or ``NoiseFloorRunner.run``) has skip-if-
    already-done logic to prevent it. ``results.MetricsWriter.__init__``
    refuses outright rather than splice a second attempt's lines onto an
    existing chain (see its docstring), so without this, a legitimate
    re-drive would crash instead of re-reporting.

    The trigger mirrors the writer's own refusal condition exactly — a
    missing or zero-byte ``metrics.jsonl`` is not a chain and is left alone.
    That one file is also the right trigger for the plots, because a plot can
    only exist where a non-empty ``metrics.jsonl`` already did: the renderer
    is fed points read back out of it.

    **Staged, then committed in one rename — not three-plus sequential
    per-file renames into a pre-existing ``prior-N/``.** Each individual
    ``Path.rename`` is atomic; the *set* of them is not, and a process killed
    between two of them used to leave ``prior-N/`` holding, say, a log with
    no chain sidecar (fails verification on its own) while ``metrics_dir``
    kept a stale sidecar with no log — which ``MetricsWriter``'s refusal used
    to miss (see its docstring), so the next drive wrote a fresh chain beside
    that orphaned sidecar and **both** generations then failed verification
    on completely honest data. The fix: build the destination in a hidden
    staging directory (:data:`_STAGING_DIR_NAME`, inside ``metrics_dir`` —
    same filesystem, so every move below is a cheap directory-entry swap, not
    a copy) and only rename the *directory* onto its numbered ``prior-N/``
    name once every file that exists has landed inside it. See
    :func:`_commit_staged_rotation` for why that last rename is what makes
    the whole set observable as only ever "before" or "after", never
    in-between.

    **Self-heals a leftover staging directory on entry, before deciding
    whether this call needs to start a new rotation.** A crash between two
    of the moves above — or between the last move and the final commit —
    leaves ``metrics_dir / _STAGING_DIR_NAME`` behind, holding whatever
    subset of the trio/plots it had gotten to. Every call to this function
    (which is to say, the start of every attempt — see the call site in
    :meth:`RoundRunner.run_attempt`) checks for that directory first and, if
    present, finishes committing it via :func:`_commit_staged_rotation`
    before touching ``metrics_dir``'s own ``metrics.jsonl``. This closes the
    residual window the staging approach still has: the interval between
    "some files moved" and "directory committed" is no longer *file-level*
    torn (a rename either lands or it does not), but it is still a window a
    kill can land in, and self-heal is what keeps that window from becoming
    permanent the way the old three-rename version's was.

    When a (possibly self-healed) rotation is needed, every name in
    :data:`_ROTATED_NAMES` ends up, un-renamed, in a numbered ``prior-N/``
    subdirectory: not deleted (the earlier attempt's evidence survives, chart
    included), not resumed (the new writer starts a fresh chain from its own
    header), and — because the filenames inside that subdirectory are still
    the canonical ``metrics.jsonl`` / ``metrics.chain.json`` / ``metrics.json``
    — still independently checkable by
    :func:`~turing.research.loop.integrity.verify_metrics_chain` and
    :func:`~turing.research.loop.integrity.reconcile_summary`, and still found
    by ``python -m turing.research.loop.verify``'s directory walk, which
    matches ``metrics.jsonl`` at every depth under a root. Adding SVGs to that
    directory does not disturb the walk: it keys on ``metrics.jsonl`` alone,
    and ``prior-N/`` already contained one before the plots joined it.

    Preserving beats deleting for the plots specifically. A stale chart is
    only misleading while it sits where the *current* attempt's chart belongs;
    under ``prior-N/``, beside the exact metrics it was drawn from, it is the
    superseded generation's evidence and is labelled as such by its path.
    """
    staging_dir = metrics_dir / _STAGING_DIR_NAME
    if staging_dir.is_dir():
        _commit_staged_rotation(metrics_dir, staging_dir)

    jsonl_path = metrics_dir / "metrics.jsonl"
    if not (jsonl_path.exists() and jsonl_path.stat().st_size > 0):
        return
    staging_dir.mkdir(parents=True)
    _commit_staged_rotation(metrics_dir, staging_dir)


#: Written into a contained attempt's ``metrics.json`` as ``final_state``,
#: with the exception's type appended. Deliberately **not** a member of
#: :class:`~turing.research.contracts.AttemptState`: the attempt reached no
#: lifecycle state — it stopped existing — and a reader (or a future ``if
#: final_state == "failed_within_cap"``) must not be able to mistake a
#: harness crash for an outcome the experiment measured. It is prefixed
#: rather than free prose so the whole class is greppable in one pass over a
#: results tree.
_CRASHED_FINAL_STATE_PREFIX = "crashed_in_harness"


def _mint_attempt_id(config: RoundConfig, problem: Problem) -> str:
    """The identity one attempt is known by, minted once per attempt.

    Lifted out of :meth:`RoundRunner.run_attempt` so the *caller* can mint it
    and keep it. That is the whole fix for the round-4 defect: the id is the
    only field in the chain header that distinguishes two generations of the
    same attempt, and it used to be created inside ``run_attempt`` — where it
    died with any exception, leaving :meth:`RoundRunner._close_out_crashed_attempt`
    nothing to compare a found chain against. Minted here and passed down, it
    survives the exception in the caller's frame, so the close-out can always
    answer "is this directory's chain the one *this* attempt wrote?" exactly.

    The ``uuid4`` suffix is what makes it a generation discriminator rather
    than a restatement of ``run_id``/``problem_id``: a re-drive under a
    byte-identical ``RoundConfig`` — the built-in shape of the noise floor's
    "fix the cause and re-run from seed 1" workflow, whose per-seed ``run_id``
    is derived deterministically as ``<run_id>-seed-<n>`` — still gets a fresh
    id here, where every other identity field repeats.
    """
    return f"{config.run_id}-{problem.id}-{uuid.uuid4().hex[:8]}"


class _ForeignChainError(Exception):
    """The chain in an attempt directory is not the chain of the attempt that crashed.

    Raised by :func:`_read_crashed_attempt_record` and caught separately from
    every other failure by :meth:`RoundRunner._close_out_crashed_attempt`,
    because the two mean opposite things: an ordinary failure there is
    "reporting broke, the loss is still disclosed elsewhere", while this one
    is "reporting worked and correctly declined to speak for someone else's
    log". Collapsing them into one ``except`` would log a deliberate,
    correct refusal under an event name an operator greps for when the
    reporting layer is broken.
    """


@dataclass(frozen=True, slots=True)
class _CrashedAttemptRecord:
    """The terminal summary fields for a crashed attempt, read off its own log.

    Every value here is re-derived from ``metrics.jsonl`` and the chain
    sidecar rather than carried out of :meth:`RoundRunner.run_attempt` —
    which is not merely convenient but necessary. The exception can come from
    anywhere in that method, so its local ``attempt`` is gone and no in-memory
    value is trustworthy at the point this is built. Deriving from the log
    also makes the resulting ``metrics.json`` reconcile with that log by
    construction: :func:`~turing.research.loop.integrity.reconcile_summary`
    checks the summary against exactly these fields, so a summary built from
    anything else would trade an ``INCOMPLETE`` verdict for a ``FAILED`` one —
    strictly worse, because ``FAILED`` is that tool's word for dishonesty.
    """

    attempt_id: str
    outcome: Outcome
    steps_recorded: int
    consumed_steps: int
    consumed_tokens: int
    consumed_wall_clock_seconds: float
    cap_extensions: int
    final_progress: float | None
    baseline_score: float | None
    escalation_count: int


def _as_number(value: object) -> float | None:
    """``value`` as a float when it is a real JSON number, else ``None``.

    ``bool`` is excluded explicitly: it is an ``int`` subclass in Python, and
    silently writing ``True`` into a numeric summary field as ``1`` would put
    a value in ``metrics.json`` that the log does not contain.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _read_crashed_attempt_record(
    metrics_dir: Path,
    escalations_dir: Path,
    *,
    expected_attempt_id: str,
    expected_problem_id: str,
    expected_round_id: str,
    expected_seed: int,
) -> _CrashedAttemptRecord:
    """Re-derive one crashed attempt's terminal summary from its own files.

    Raises rather than returning a partial record: every caller is already
    inside a guard that logs and moves on, and a half-derived summary written
    over an honest log is the one outcome worse than no summary at all.

    **The header must name this attempt, or nothing is derived.** The chain
    header is the only place an attempt's identity survives on disk, and it is
    checked field by field — ``attempt_id``, ``problem_id``, ``round_id``,
    ``seed`` — against the attempt that actually crashed, raising
    :class:`_ForeignChainError` when any of them disagrees or is missing. This
    is not defensive programming; the directory genuinely can hold a *previous
    generation's* chain at this moment. ``run_attempt`` materialises the
    workspace (I/O against a template that can be pruned, evicted or fill a
    disk) **before** it calls ``_rotate_stale_metrics``, so an exception in
    that window leaves the earlier generation's trio untouched and un-rotated:
    an intact chain with no summary — the honest "killed before its summary
    write" shape that ``verify`` reports ``INCOMPLETE`` and that rotation
    exists to preserve. Without this check the close-out adopted that chain,
    wrote a summary asserting the *current* round's ``round_id`` and ``seed``
    and an exception type that never touched it, and ``verify`` blessed the
    result ``OK``, because every field it reconciles was re-derived from the
    very log the summary had just been misattributed to. Two harms, either
    sufficient: a fabricated claim about a run that did not make it, and the
    destruction of the one honest state — ``INCOMPLETE`` — that said the
    earlier generation was killed unfinished.

    **``attempt_id`` is the field that actually decides it, and the other
    three are kept for the message.** Round 3 left ``attempt_id`` out on the
    grounds that it was minted inside ``run_attempt`` and died with the
    exception; that made ``problem_id``/``round_id``/``seed`` the whole guard,
    and those three are *equal across generations by construction* in the only
    re-run workflow this package ships. ``NoiseFloorConfig.round_config_for``
    derives each seed's ``run_id`` deterministically as
    ``<run_id>-seed-<n>``, and the refusal an incomplete seed raises tells the
    operator to "fix the cause and re-run the noise floor from seed 1" — so
    the sanctioned recovery reuses the identical ``run_id`` *and* seed, into
    the identical ``noise_floor_seed_dir``. A gen-2 attempt crashing in
    ``materialise`` therefore matched all three, adopted gen 1's chain, and
    overwrote a killed attempt's honest ``INCOMPLETE`` with
    ``crashed_in_harness:OSError`` — flipping the directory to ``OK`` under
    ``verify``, the pre-writeup gate. :func:`_mint_attempt_id` is now called by
    ``run_attempts`` *before* ``run_attempt``, so the crashed attempt's own id
    survives in the caller's frame and reaches here; its ``uuid4`` suffix
    differs between generations even when every other field repeats. The other
    three stay in the comparison because they cost nothing and they are what
    makes the refusal *readable* — "round_id='gen1' (expected 'gen2')" tells
    an operator which generation is sitting in the directory, where a bare
    id mismatch would not.

    This is a different comparison from
    :data:`~turing.research.loop.integrity._IDENTITY_FIELDS`, which
    ``reconcile_summary`` uses, and the two must not be conflated.
    That one compares a ``metrics.json`` against the header *beside it* and
    excludes ``started_at_ms`` because that field never reaches a summary —
    a statement about which fields the two **documents** have in common. This
    one compares the header against the **live attempt** that just crashed,
    where the available evidence is whatever survived the exception in the
    caller's frame. Different sides, different evidence, so neither list is
    the other's.

    ``started_at_ms`` cannot serve here, which is why the id is minted
    upstream instead. ``run_attempt`` awaits ``materialise`` *before* it reads
    the clock and builds the :class:`~turing.research.contracts.Attempt`, so
    at the canonical crash point no start time has been taken at all; the
    nearest substitute — a watermark stamped by the caller before the call and
    compared with ``>=`` — would rest on wall-clock monotonicity *across
    processes*, which ``SystemClock.now_ms`` (``time.time()``) does not
    provide. An NTP step, a container clock reset, or simply a re-run inside
    the same millisecond would make a previous generation's header look
    current, and the guard would adopt exactly the chain it exists to refuse.
    A ``uuid4`` needs no clock and no ordering assumption.

    ``escalation_count`` is counted from the round's ``escalations/``
    directory rather than defaulted to ``0``. The in-memory request list died
    with the attempt, but the requests themselves were written to disk before
    the loop suspended on each one, so the real number is recoverable — and
    writing ``0`` for an attempt that interrupted its operator three times
    would be a fabricated number in a file whose entire purpose is to be
    citable. Files that do not parse, or that name a different attempt, are
    skipped: this is a count of evidence found, not an assertion about
    evidence missing.
    """
    tail = integrity.read_log_tail(metrics_dir)
    last = tail.last_line
    if last is None:
        raise ContractViolationError(f"{metrics_dir} has no parseable metrics lines")
    header = tail.header
    if header is None:
        raise ContractViolationError(f"{metrics_dir} has no readable chain header")
    attempt_id = header.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ContractViolationError(f"{metrics_dir}'s chain header carries no attempt_id")

    foreign = [
        f"{field}={header.get(field)!r} (expected {expected!r})"
        for field, expected in (
            ("attempt_id", expected_attempt_id),
            ("problem_id", expected_problem_id),
            ("round_id", expected_round_id),
            ("seed", expected_seed),
        )
        if header.get(field) != expected
    ]
    if foreign:
        raise _ForeignChainError(
            f"{metrics_dir}'s chain header names a different attempt ({attempt_id}): "
            + "; ".join(foreign)
        )

    numbers = {
        name: _as_number(last.get(key))
        for name, key in (
            ("outcome", "outcome_code"),
            ("consumed_steps", "consumed_steps"),
            ("consumed_tokens", "tokens_used"),
            ("consumed_wall_clock_seconds", "wall_clock_s"),
            ("cap_extensions", "cap_extensions"),
        )
    }
    missing = sorted(name for name, value in numbers.items() if value is None)
    if missing:
        raise ContractViolationError(
            f"{metrics_dir}'s last metrics line is missing or non-numeric in {missing}"
        )

    escalation_count = 0
    for path in sorted(escalations_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        request = payload.get("request") if isinstance(payload, dict) else None
        if isinstance(request, dict) and request.get("attempt_id") == attempt_id:
            escalation_count += 1

    return _CrashedAttemptRecord(
        attempt_id=attempt_id,
        # ``Outcome(...)`` rather than a cast: an outcome code the enum does
        # not define is a corrupt log, and inventing a code for it here would
        # put a number in metrics.json that no state maps to.
        outcome=Outcome(int(numbers["outcome"] or 0)),
        steps_recorded=tail.line_count,
        consumed_steps=int(numbers["consumed_steps"] or 0),
        consumed_tokens=int(numbers["consumed_tokens"] or 0),
        consumed_wall_clock_seconds=numbers["consumed_wall_clock_seconds"] or 0.0,
        cap_extensions=int(numbers["cap_extensions"] or 0),
        final_progress=_as_number(tail.final_progress),
        baseline_score=_as_number(tail.baseline_score),
        escalation_count=escalation_count,
    )


def _parent_attempts_completed(parent: RoundRecord | None) -> bool | None:
    """Whether the parent round measured its whole corpus, as *it* recorded it.

    Read off the parent's persisted ``all_attempts_completed`` gate rather
    than recomputed, because it cannot be recomputed: the parent's losses
    lived in a previous ``run_round`` call's ``attempt_failures``, and the
    only surviving statement about them is the gate that call wrote. A round
    that lost an attempt has cells reduced over a strict subset of the corpus,
    and that is exactly as fatal to a delta when it happens on the parent side
    of the subtraction as on this one — see :func:`compute_deltas`, which had
    the argument in its docstring while the parent's gate was never read.

    Three answers, not two. ``None`` means the record carries no such gate at
    all — a record written before the gate existed, or one reconstructed by a
    caller that dropped it. That is *unknown*, not *fine*, and
    :func:`compute_deltas` refuses it: the failure this guards is a phantom
    gain emitted with every gate green, so "silent" is the exact state it
    cannot be allowed to resolve to. A non-``bool`` value is folded into
    ``None`` for the same reason — a truthy string in a gate map is not a
    measurement of anything.

    ``parent is None`` answers ``True`` rather than ``None``: there is no
    parent to be short a problem, and every consumer already refuses a
    parentless round through ``REFUSED_NO_PARENT``. Answering ``None`` there
    would relabel round 0's honest "no baseline yet" as a suspect baseline.
    """
    if parent is None:
        return True
    value = parent.gates.get("all_attempts_completed")
    return value if isinstance(value, bool) else None


class RoundRunner:
    """Runs one round of the corpus. Unattended except for escalations."""

    def __init__(
        self,
        *,
        solver: Solver,
        workspaces: WorkspaceProvider,
        trajectory: TrajectoryStore,
        escalations: EscalationChannel,
        clock: Clock | None = None,
    ) -> None:
        self._solver = solver
        self._workspaces = workspaces
        self._trajectory = trajectory
        self._escalations = escalations
        self._clock = clock or SystemClock()
        self._operator_wait_seconds = 0.0
        self._attempt_failures: tuple[AttemptFailure, ...] = ()

    @property
    def clock(self) -> Clock:
        """The injected clock, so callers timestamp against the same one."""
        return self._clock

    @property
    def attempt_failures(self) -> tuple[AttemptFailure, ...]:
        """Problems lost to a raising attempt during the last :meth:`run_attempts`.

        Reset at the head of every :meth:`run_attempts` call, the same
        per-invocation lifetime ``_operator_wait_seconds`` already has, so a
        noise-floor runner's seed-by-seed calls each report their own losses
        instead of accumulating them. Returned rather than raised because the
        round that lost an attempt still has real results to record for the
        rest of the corpus — see :meth:`run_attempts`.
        """
        return self._attempt_failures

    # -- one attempt -------------------------------------------------------- #

    async def run_attempt(
        self,
        problem: Problem,
        config: RoundConfig,
        *,
        output_dir: Path,
        attempt_id: str | None = None,
    ) -> AttemptOutcome:
        """Work one problem until it passes, the cap trips, or it is abandoned.

        ``attempt_id`` is minted here when the caller does not supply one, so a
        direct call still works. :meth:`run_attempts` *does* supply one, and
        must: the id is the only evidence that distinguishes two generations of
        the same attempt, and minting it inside this method meant it died with
        any exception raised out of it — see :func:`_mint_attempt_id` and
        :meth:`_close_out_crashed_attempt`.
        """
        attempt_id = attempt_id or _mint_attempt_id(config, problem)
        workspace = await self._workspaces.materialise(problem, attempt_id=attempt_id)
        now = self._clock.now_ms()
        attempt = Attempt(
            attempt_id=attempt_id,
            problem_id=problem.id,
            round_id=config.run_id,
            seed=config.seed,
            workspace_path=workspace,
            cap=problem.default_cap or config.default_cap,
            state=AttemptState.PENDING,
            started_at_ms=now,
            updated_at_ms=now,
        )
        attempt = attempt.resume(now_ms=now)

        steps: list[StepLog] = []
        escalations: list[EscalationRequest] = []
        decisions: list[EscalationDecision] = []
        best: VerificationResult | None = None
        criterion = config.criterion_for(problem.id)

        # -- reporting: the seam that makes this attempt visible while it
        # runs, not only after trajectory.json is appended to at round end.
        # See turing.research.loop.results for the file contract.
        metrics_dir = output_dir / "attempts" / problem.id
        _rotate_stale_metrics(metrics_dir)
        metrics_writer = MetricsWriter(
            metrics_dir / "metrics.jsonl",
            header={
                "attempt_id": attempt.attempt_id,
                "problem_id": problem.id,
                "round_id": attempt.round_id,
                "seed": attempt.seed,
                "score_scale": problem.verifier.score_scale,
                "started_at_ms": attempt.started_at_ms,
            },
            clock=self._clock,
        )
        progress_tracker = ProgressTracker(
            target=None if criterion is None else criterion.min_score
        )
        score_series = problem.verifier.score_scale
        baseline_score: float | None = None
        final_progress: float | None = None

        while True:
            attempt, cap_action = await self._enforce_cap(
                problem=problem,
                attempt=attempt,
                best=best,
                config=config,
                output_dir=output_dir,
                escalations=escalations,
                decisions=decisions,
            )
            if cap_action == "break":
                break
            if cap_action == "continue":
                continue

            step_started = self._clock.monotonic()
            try:
                step = await self._solver.step(SolverTask.from_problem(problem), attempt)
            except Exception as exc:  # solver/backend blew up — a harness failure
                logger.exception(
                    "research.attempt.solver_failed",
                    problem_id=problem.id,
                    attempt_id=attempt.attempt_id,
                )
                attempt = self._charge_failed_step(attempt, step_started, exc)
                attempt, cap_action = await self._enforce_cap(
                    problem=problem,
                    attempt=attempt,
                    best=best,
                    config=config,
                    output_dir=output_dir,
                    escalations=escalations,
                    decisions=decisions,
                )
                if cap_action == "break":
                    break
                if cap_action == "continue":
                    continue
                attempt, _ = await self._escalate_or_fail(
                    problem=problem,
                    attempt=attempt,
                    reason=EscalationReason.HARNESS_FAILURE,
                    summary=f"solver raised {type(exc).__name__}: {exc}",
                    best=best,
                    config=config,
                    output_dir=output_dir,
                    escalations=escalations,
                    decisions=decisions,
                )
                if attempt.is_terminal:
                    break
                continue

            step_elapsed = self._elapsed(step_started)
            now = self._clock.now_ms()
            attempt = attempt.record_consumption(
                CapConsumption(steps=1, tokens=step.tokens, wall_clock_seconds=step_elapsed),
                now_ms=now,
            )
            attempt = attempt.evolve(
                now_ms=now,
                step_index=attempt.step_index + 1,
                resume_token=step.resume_token
                if step.resume_token is not None
                else attempt.resume_token,
            )

            result: VerificationResult | None = None
            verifier_error: str | None = None
            verify_elapsed = 0.0
            # Per-step verify is skipped once the cap is already gone (unit 14).
            # ``verify_every_step=False`` still verifies once, on that trip —
            # otherwise ``best`` stays None and the problem enters its cell at
            # the scale floor, fabricating a parent-round delta.
            verify_now = (config.verify_every_step and not attempt.cap_exhausted) or (
                not config.verify_every_step and attempt.cap_exhausted and best is None
            )
            if verify_now:
                attempt = attempt.evolve(now_ms=now, state=AttemptState.VERIFYING)
                verify_started = self._clock.monotonic()
                try:
                    result = await problem.verifier.verify(attempt.workspace_path)
                except Exception as exc:
                    verifier_error = f"{type(exc).__name__}: {exc}"
                    logger.exception(
                        "research.attempt.verifier_failed",
                        problem_id=problem.id,
                        attempt_id=attempt.attempt_id,
                    )
                else:
                    if _is_better(result, best):
                        best = result
                verify_elapsed = self._elapsed(verify_started)
                now = self._clock.now_ms()
                attempt = attempt.record_consumption(
                    CapConsumption(wall_clock_seconds=verify_elapsed), now_ms=now
                )
                attempt = attempt.evolve(
                    now_ms=now,
                    state=AttemptState.RUNNING,
                    result=best,
                )

            steps.append(
                StepLog(
                    index=attempt.step_index,
                    at_ms=now,
                    tokens=step.tokens,
                    wall_clock_seconds=step_elapsed + verify_elapsed,
                    note=step.note,
                    made_progress=step.made_progress,
                    escalate_requested=None if step.escalate is None else step.escalate.value,
                    score=None if result is None else result.score,
                    passed_correctness=None if result is None else result.passed_correctness,
                    verifier_error=verifier_error,
                )
            )
            await self._trajectory.write_attempt_checkpoint(attempt, output_dir=output_dir)

            observed = None if result is None else progress_tracker.observe(result.score)
            if observed is not None:
                final_progress = observed
            if baseline_score is None:
                baseline_score = progress_tracker.baseline
            await metrics_writer.append(
                MetricsLine(
                    step=attempt.step_index,
                    total_steps=attempt.cap.max_steps,
                    ts=now / 1000.0,
                    outcome=Outcome.from_attempt_state(attempt.state),
                    correctness_pass=None if result is None else result.passed_correctness,
                    tokens_used=attempt.consumed.tokens,
                    tokens_cap=attempt.cap.max_tokens,
                    steps_cap=attempt.cap.max_steps,
                    consumed_steps=attempt.consumed.steps,
                    wall_clock_s=attempt.consumed.wall_clock_seconds,
                    wall_clock_cap_s=attempt.cap.max_wall_clock_seconds,
                    cap_extensions=attempt.cap.extension_count,
                    step_wall_clock_s=step_elapsed,
                    verify_wall_clock_s=verify_elapsed,
                    step_tokens=step.tokens,
                    made_progress=step.made_progress,
                    progress=observed,
                    metrics={} if result is None else {score_series: result.score},
                    diagnostics=await read_agent_diagnostics(attempt.workspace_path),
                )
            )

            if verifier_error is not None:
                attempt, _ = await self._escalate_or_fail(
                    problem=problem,
                    attempt=attempt,
                    reason=EscalationReason.VERIFIER_UNRUNNABLE,
                    summary=f"verifier {problem.verifier_id} failed: {verifier_error}",
                    best=best,
                    config=config,
                    output_dir=output_dir,
                    escalations=escalations,
                    decisions=decisions,
                )
                if attempt.is_terminal:
                    break
                continue

            if result is not None and result.harness_failed:
                logger.warning(
                    "research.attempt.harness_failure",
                    problem_id=problem.id,
                    attempt_id=attempt.attempt_id,
                    detail=result.detail,
                )
                attempt, _ = await self._escalate_or_fail(
                    problem=problem,
                    attempt=attempt,
                    reason=EscalationReason.HARNESS_FAILURE,
                    summary=result.detail or "verifier reported a harness failure",
                    best=best,
                    config=config,
                    output_dir=output_dir,
                    escalations=escalations,
                    decisions=decisions,
                )
                if attempt.is_terminal:
                    break
                continue

            attempt, cap_action = await self._enforce_cap(
                problem=problem,
                attempt=attempt,
                best=best,
                config=config,
                output_dir=output_dir,
                escalations=escalations,
                decisions=decisions,
            )
            if cap_action == "break":
                break
            if cap_action == "continue":
                continue

            if result is not None and criterion is not None and criterion.satisfied_by(result):
                logger.info(
                    "research.attempt.passed",
                    problem_id=problem.id,
                    attempt_id=attempt.attempt_id,
                    score=result.score,
                    steps=attempt.consumed.steps,
                )
                attempt = self._finish(attempt, AttemptState.PASSED, best)
                break

            if step.escalate is not None:
                attempt, _ = await self._escalate_or_fail(
                    problem=problem,
                    attempt=attempt,
                    reason=step.escalate,
                    summary=step.note or f"solver requested {step.escalate.value}",
                    best=best,
                    config=config,
                    output_dir=output_dir,
                    escalations=escalations,
                    decisions=decisions,
                )
                if attempt.is_terminal:
                    break

        await self._trajectory.write_attempt_checkpoint(attempt, output_dir=output_dir)

        # Edit D, terminal line (spec's "Terminal-line rule", added
        # 2026-08-14). Every line appended above was written from *inside*
        # the loop, so each one necessarily carries the attempt's
        # pre-termination state — nothing recorded the state the attempt
        # actually ended in. metrics.json's outcome is the terminal Outcome,
        # so without this the log's last line and the summary disagree on
        # every honest run and reconcile_summary reports a false mismatch
        # (this is the bug the gate caught). Resource fields are read from
        # ``attempt``/``best`` directly rather than copied from the last
        # in-loop line, so this still reconciles even on the
        # solver-exception termination path, where no metrics line is
        # written for the charge that ended the attempt. Deliberately *not*
        # wrapped in the try/except below: it is a chain-extending write
        # with the same integrity contract as the per-step appends above
        # (also unwrapped) — a swallowed failure here would silently break
        # the hash chain on the file's own last line.
        #
        # What that costs, stated exactly (the earlier claim here — "raising
        # here cannot turn a solved attempt into a lost one" — was too
        # strong): the workspace and the checkpoint written just above are
        # durable, so the *work* survives a raise. But this append precedes
        # write_attempt_log and the AttemptOutcome return below, so a raise
        # does cost this attempt its attempts/<problem-id>.json log and its
        # place in the round's cells. What it can no longer do is take the
        # rest of the round with it: run_attempts contains a raising attempt
        # to that attempt and reports the loss through the round's
        # all_attempts_completed gate. See its docstring.
        await metrics_writer.append(
            MetricsLine(
                step=attempt.step_index,
                total_steps=attempt.cap.max_steps,
                ts=self._clock.now_ms() / 1000.0,
                outcome=Outcome.from_attempt_state(attempt.state),
                correctness_pass=None if best is None else best.passed_correctness,
                tokens_used=attempt.consumed.tokens,
                tokens_cap=attempt.cap.max_tokens,
                steps_cap=attempt.cap.max_steps,
                consumed_steps=attempt.consumed.steps,
                wall_clock_s=attempt.consumed.wall_clock_seconds,
                wall_clock_cap_s=attempt.cap.max_wall_clock_seconds,
                cap_extensions=attempt.cap.extension_count,
                step_wall_clock_s=0.0,
                verify_wall_clock_s=0.0,
                step_tokens=0,
                made_progress=None,
                progress=final_progress,
                metrics={},
                diagnostics={},
            )
        )

        log = AttemptLog(
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            round_id=attempt.round_id,
            seed=attempt.seed,
            split=problem.split.value,
            problem_type=problem.problem_type.value,
            final_state=attempt.state.value,
            steps=tuple(steps),
            best_score=None if best is None else best.score,
            best_passed_correctness=None if best is None else best.passed_correctness,
            score_scale=problem.verifier.score_scale,
            consumed_steps=attempt.consumed.steps,
            consumed_tokens=attempt.consumed.tokens,
            consumed_wall_clock_seconds=attempt.consumed.wall_clock_seconds,
            cap_extensions=attempt.cap.extension_count,
            escalation_ids=tuple(e.request_id for e in escalations),
            workspace_path=str(attempt.workspace_path),
        )
        path = await self._trajectory.write_attempt_log(log, output_dir=output_dir)

        # Reporting failures must not fail an attempt that otherwise solved
        # its problem — this is the one sanctioned swallowed error in this
        # module. The metrics.jsonl append above is *not* inside this guard:
        # a chain-breaking write failure there is a contract bug that must
        # surface immediately, not an observability hiccup.
        try:
            await write_attempt_summary(
                metrics_dir,
                problem_id=problem.id,
                attempt_id=attempt.attempt_id,
                round_id=attempt.round_id,
                seed=attempt.seed,
                problem_type=problem.problem_type.value,
                split=problem.split.value,
                score_scale=problem.verifier.score_scale,
                outcome=Outcome.from_attempt_state(attempt.state),
                final_state=attempt.state.value,
                best_score=None if best is None else best.score,
                best_passed_correctness=None if best is None else best.passed_correctness,
                baseline_score=baseline_score,
                target_score=None if criterion is None else criterion.min_score,
                final_progress=final_progress,
                consumed_steps=attempt.consumed.steps,
                consumed_tokens=attempt.consumed.tokens,
                consumed_wall_clock_seconds=attempt.consumed.wall_clock_seconds,
                cap_extensions=attempt.cap.extension_count,
                escalation_count=len(escalations),
                # Lines, not steps: the terminal line above makes the two
                # legitimately differ by one (spec's terminal-line rule).
                steps_recorded=metrics_writer.line_count,
            )
            points = await read_metrics_points(metrics_writer.path)
            await render_attempt_plots(
                metrics_dir,
                points,
                # Corrected 2026-08-14: was the literal "score", which the
                # emitter never writes (the raw series is named after
                # score_series, bound above from problem.verifier.score_scale).
                # That mismatch skipped progress.svg on every attempt. See
                # AC23 and the spec's per-file note on this call site.
                forcing_series=score_series,
                # ``usable_target`` rather than the raw ``min_score``: with a
                # non-finite target matplotlib draws no reference line but
                # still adds "target" to the legend, so the plot would claim a
                # bar it never drew while metrics.json (which normalises the
                # same value) says ``target_score: null``. One predicate, so
                # the tracker, the summary and the plot cannot disagree.
                target=usable_target(None if criterion is None else criterion.min_score),
            )
        except Exception:
            logger.exception(
                "research.results.emit_failed",
                problem_id=problem.id,
                attempt_id=attempt.attempt_id,
                # The directory whose files did not land. ``verify`` reports a
                # missing summary by path, and without this the ERROR that
                # explains why cannot be matched to it in an overnight log.
                directory=str(metrics_dir),
            )

        return AttemptOutcome(
            problem=problem,
            attempt=attempt,
            best_result=best,
            steps=tuple(steps),
            escalations=tuple(escalations),
            decisions=tuple(decisions),
            attempt_log_path=path,
        )

    def _finish(
        self,
        attempt: Attempt,
        state: AttemptState,
        best: VerificationResult | None,
    ) -> Attempt:
        return attempt.evolve(now_ms=self._clock.now_ms(), state=state, result=best)

    async def _enforce_cap(
        self,
        *,
        problem: Problem,
        attempt: Attempt,
        best: VerificationResult | None,
        config: RoundConfig,
        output_dir: Path,
        escalations: list[EscalationRequest],
        decisions: list[EscalationDecision],
    ) -> tuple[Attempt, Literal["ok", "break", "continue"]]:
        """Stop or extend as soon as a charge trips the cap — not at the next loop head.

        Rechecking only at the top of the loop lets an over-budget step still
        verify, pass, or escalate. After a charge, this is the next thing that
        runs.
        """
        if not attempt.cap_exhausted:
            return attempt, "ok"
        if (
            config.escalate_on_cap_exhaustion
            and len(escalations) < config.max_escalations_per_attempt
        ):
            attempt, decision = await self._escalate(
                problem=problem,
                attempt=attempt,
                reason=EscalationReason.CAP_EXHAUSTED,
                summary=self._cap_summary(attempt),
                best=best,
                config=config,
                output_dir=output_dir,
                escalations=escalations,
                decisions=decisions,
            )
            if attempt.is_terminal:
                return attempt, "break"
            if attempt.cap_exhausted:
                # CONTINUE with no budget left cannot mean "keep
                # working"; record the honest outcome rather than
                # spinning on an exhausted cap.
                logger.warning(
                    "research.attempt.continue_without_budget",
                    problem_id=problem.id,
                    attempt_id=attempt.attempt_id,
                    verdict=decision.verdict.value if decision else None,
                )
                return self._finish(attempt, AttemptState.FAILED_WITHIN_CAP, best), "break"
            return attempt, "continue"
        logger.info(
            "research.attempt.cap_exhausted",
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            dimensions=[d.value for d in attempt.exceeded_cap_dimensions],
            steps=attempt.consumed.steps,
            tokens=attempt.consumed.tokens,
        )
        return self._finish(attempt, AttemptState.FAILED_WITHIN_CAP, best), "break"

    def _elapsed(self, started: float) -> float:
        return max(0.0, self._clock.monotonic() - started)

    def _charge_failed_step(self, attempt: Attempt, started: float, exc: BaseException) -> Attempt:
        """Charge wall-clock, plus any backend spend the error carried.

        ``generate`` records usage before adapter parse / apply / command
        can raise. If that spend stayed only on the backend ledger,
        operator CONTINUE would retry with it omitted. Tokens on the
        error are accounted usage, never a model-authored JSON field.
        A proposal call that happened still costs a step.
        """
        spend = _spend_carried_on_error(exc)
        return attempt.record_consumption(
            CapConsumption(
                steps=spend.steps,
                tokens=spend.tokens,
                wall_clock_seconds=self._elapsed(started),
            ),
            now_ms=self._clock.now_ms(),
        )

    @staticmethod
    def _cap_summary(attempt: Attempt) -> str:
        dims = ", ".join(d.value for d in attempt.exceeded_cap_dimensions) or "none"
        return (
            f"cap exhausted on {dims} after {attempt.consumed.steps} steps / "
            f"{attempt.consumed.tokens} tokens"
        )

    async def _escalate_or_fail(
        self,
        *,
        problem: Problem,
        attempt: Attempt,
        reason: EscalationReason,
        summary: str,
        best: VerificationResult | None,
        config: RoundConfig,
        output_dir: Path,
        escalations: list[EscalationRequest],
        decisions: list[EscalationDecision],
    ) -> tuple[Attempt, EscalationDecision | None]:
        """Escalate. This call-site guard has no quit path of its own.

        ``max_escalations_per_attempt`` does not terminate here. Stopping after
        N operator ``CONTINUE`` verdicts would both override the operator and
        write ``FAILED_WITHIN_CAP`` with the cap untouched — a false outcome
        that truncates driving function #4 by exactly the worst problems.
        Only an operator ``ABANDON`` or a tripped cap ends the attempt
        through this writer; :meth:`~turing.research.contracts.Attempt.evolve`
        can still reach ``ABANDONED`` with a leftover ``escalation_id``.
        """
        if len(escalations) >= config.max_escalations_per_attempt:
            logger.warning(
                "research.attempt.escalation_budget_spent",
                problem_id=problem.id,
                attempt_id=attempt.attempt_id,
                escalations=len(escalations),
                reason=reason.value,
            )
        return await self._escalate(
            problem=problem,
            attempt=attempt,
            reason=reason,
            summary=summary,
            best=best,
            config=config,
            output_dir=output_dir,
            escalations=escalations,
            decisions=decisions,
        )

    async def _escalate(
        self,
        *,
        problem: Problem,
        attempt: Attempt,
        reason: EscalationReason,
        summary: str,
        best: VerificationResult | None,
        config: RoundConfig,
        output_dir: Path,
        escalations: list[EscalationRequest],
        decisions: list[EscalationDecision],
    ) -> tuple[Attempt, EscalationDecision | None]:
        """Suspend the loop on an operator decision, then resume or terminate."""
        now = self._clock.now_ms()
        request = EscalationRequest(
            request_id=f"esc-{uuid.uuid4().hex[:12]}",
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            round_id=attempt.round_id,
            reason=reason,
            summary=summary,
            cap=attempt.cap,
            consumed=attempt.consumed,
            created_at_ms=now,
            best_result=best,
        )
        escalations.append(request)
        # ESCALATED is neither terminal nor resumable — only an operator
        # decision moves it — so the checkpoint is written *before* suspending:
        # a crash while waiting resumes here rather than restarting the attempt.
        attempt = attempt.evolve(
            now_ms=now,
            state=AttemptState.ESCALATED,
            escalation_id=request.request_id,
            result=best,
        )
        await self._trajectory.write_attempt_checkpoint(attempt, output_dir=output_dir)
        await self._trajectory.write_escalation(request, None, output_dir=output_dir)
        logger.warning(
            "research.attempt.escalated",
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            request_id=request.request_id,
            reason=reason.value,
        )

        waited_from = self._clock.monotonic()
        try:
            decision = await self._escalations.request_decision(request)
        finally:
            self._operator_wait_seconds += max(0.0, self._clock.monotonic() - waited_from)
        if decision.request_id != request.request_id:
            raise EscalationProtocolError(
                f"decision {decision.request_id!r} does not answer open request "
                f"{request.request_id!r}"
            )
        decisions.append(decision)
        await self._trajectory.write_escalation(request, decision, output_dir=output_dir)
        now = self._clock.now_ms()

        if decision.verdict is EscalationVerdict.ABANDON:
            logger.warning(
                "research.attempt.abandoned",
                problem_id=problem.id,
                attempt_id=attempt.attempt_id,
                request_id=request.request_id,
            )
            attempt = attempt.evolve(now_ms=now, state=AttemptState.ABANDONED, result=best)
            await self._trajectory.write_attempt_checkpoint(attempt, output_dir=output_dir)
            return attempt, decision

        # CONTINUE leaves the cap alone; EXTEND_CAP grows it via the
        # sanctioned path. `evolve` rather than `resume` because ESCALATED is
        # deliberately not resumable: this is the operator's hand on the
        # switch, not the agent's. Clear the bound id — a leftover one
        # would let evolve(state=ABANDONED) succeed without a new verdict.
        if decision.verdict is EscalationVerdict.EXTEND_CAP:
            assert decision.cap_extension is not None  # guaranteed by EscalationDecision
            attempt = attempt.apply_cap_extension(decision.cap_extension, now_ms=now)
        attempt = attempt.evolve(now_ms=now, state=AttemptState.RUNNING, escalation_id=None)
        await self._trajectory.write_attempt_checkpoint(attempt, output_dir=output_dir)
        logger.info(
            "research.attempt.resumed",
            problem_id=problem.id,
            attempt_id=attempt.attempt_id,
            request_id=request.request_id,
            verdict=decision.verdict.value,
            cap_extensions=attempt.cap.extension_count,
        )
        return attempt, decision

    # -- a whole round ------------------------------------------------------ #

    async def run_attempts(
        self,
        corpus: Sequence[Problem],
        config: RoundConfig,
        *,
        output_dir: Path,
    ) -> tuple[AttemptOutcome, ...]:
        """Run every problem once, sequentially. No trajectory row is written.

        Sequential because the brief buys opportunistic subscription use with
        "no parallel sweep"; it also keeps the round's wall clock a meaningful
        cost number (machine time, not operator sleep — see :meth:`run_round`).

        Used directly by the noise-floor runner, whose seed runs are *not*
        rounds and must not enter ``trajectory.json``.

        **One attempt's failure is contained to that attempt.** Everything
        inside :meth:`run_attempt` that is *not* the sanctioned reporting
        guard raises straight out of it, and that is correct: the per-step and
        terminal ``metrics_writer.append`` calls are chain-extending writes,
        and swallowing a failure there would silently break the hash chain
        that ``verify`` exists to check. It is *this* loop that must not
        propagate. Without the guard below, one problem discards every attempt
        the round had already completed — no round record, no trajectory row,
        no cells — and the reachable triggers are not exotic: a workspace
        template that cannot be read (pruned, evicted, a full disk), a
        rotation the results tree will not permit, a backend whose usage
        accounting reports a non-finite token count. Ten completed attempts
        must not be thrown away by the eleventh problem's I/O.

        The trigger this guard was originally built around — a
        ``problem.verifier.score_scale`` colliding with a reserved or core
        metrics key, fatal at that problem's *first verification* — is gone:
        ``contracts._reject_unusable_score_scale`` refuses such a scale where
        the verifier declares it, so a corpus carrying one now fails before
        the round starts, at zero compute, instead of losing an attempt
        mid-round. The containment stays: it guards "``run_attempt`` raised",
        not any particular cause, and the remaining causes are I/O against a
        results tree and a workspace this process does not own.

        **A lost attempt is dropped from the results, not floored into
        them.** The alternative — synthesising an ``AttemptOutcome`` with
        ``best_result=None`` — looks safer and is worse: that is exactly the
        shape of an honest attempt that ran its cap and never scored, so
        ``ScoredProblem.from_result`` would enter the problem into its cell at
        the scale floor and ``build_type_scores`` would average an instrument
        failure in as a capability reading. A crashed harness would then
        register as the agent performing at its worst, manufacturing a
        regression against the parent round out of a bug in this file. This
        module is the experiment's instrument; a measurement it did not take
        must be reported as absent, never as a bad one. For the colliding-
        scale trigger specifically, the floored outcome is not even
        constructible: ``ScoredProblem.from_result`` refuses a scale with no
        declared floor, so a synthesised outcome would only move the same
        abort from the metrics line to the scoring pass.

        Absent is not silent. Each loss is logged with its traceback
        (``research.attempt.crashed``), returned to :meth:`run_round` via
        :attr:`attempt_failures`, and surfaced in the round record's
        ``all_attempts_completed`` gate and verdict line, so no reader of the
        round can mistake ``n=10`` for a ten-problem corpus. It also *refuses
        the round's deltas outright* — see :meth:`run_round` — because a cell
        mean over nine problems is not comparable with a parent's over ten,
        and the bias is one-directional: a mean rises when its weakest member
        drops out, so a contained crash left unrefused manufactures a
        marginal gain out of a bug in this file.

        **A lost attempt still gets a finished record on disk.**
        :meth:`_close_out_crashed_attempt` writes the terminal
        ``metrics.json`` :meth:`run_attempt` never reached, so a round that
        is over stops reporting itself as unfinished to ``verify``. That is a
        reporting completion, not a rehabilitation: the attempt is still
        absent from every cell and still counted as a loss here.

        If **every** attempt is lost the round is refused outright rather than
        recorded with zero cells: an empty round record is a baseline that
        measures nothing, and a later round comparing against it would compute
        deltas from an absence.

        **The noise-floor caller has a stricter bar than this, and it is not
        enforced here.** ``measure_noise_floor`` guards that every *cell* is
        present in every seed, which cannot see a seed that measured a cell
        over one fewer *problem* — and a spread computed over a shifting
        problem set measures the set, not the agent. An unreadable workspace
        template is a property of the problem and so is lost by every seed
        identically (consistent, just narrower); a transient failure hitting
        one seed only is not. ``NoiseFloorRunner.run`` therefore wants
        to refuse a seed with a non-empty :attr:`attempt_failures` rather than
        report a floor from it. That check belongs at that call site — a round
        legitimately continues where a floor must not — and the property
        exists for it.

        Not caught: :class:`asyncio.CancelledError`,
        :class:`KeyboardInterrupt` and :class:`SystemExit` are
        ``BaseException``\\ s and stay outside ``except Exception`` on
        purpose. A cancelled round must stop, not quietly re-label the
        cancellation as a per-problem harness failure and keep working
        through the corpus.
        """
        if not corpus:
            raise ContractViolationError("a round with no problems measures nothing")
        outcomes: list[AttemptOutcome] = []
        failures: list[AttemptFailure] = []
        self._attempt_failures = ()
        for problem in corpus:
            # Minted here, not inside ``run_attempt``, so it outlives an
            # exception raised out of it and the close-out below can tell this
            # attempt's chain from a previous generation's — the one field
            # that can, when a re-drive repeats run_id, problem_id and seed.
            attempt_id = _mint_attempt_id(config, problem)
            try:
                outcomes.append(
                    await self.run_attempt(
                        problem, config, output_dir=output_dir, attempt_id=attempt_id
                    )
                )
            except Exception as exc:
                logger.exception(
                    "research.attempt.crashed",
                    problem_id=problem.id,
                    run_id=config.run_id,
                    round_index=config.round_index,
                    error=f"{type(exc).__name__}: {exc}",
                    detail=(
                        "the attempt raised out of run_attempt and produced no outcome; it "
                        "is absent from this round's cells rather than scored at its floor, "
                        "and the remaining problems still run"
                    ),
                )
                failures.append(
                    AttemptFailure(problem_id=problem.id, error=f"{type(exc).__name__}: {exc}")
                )
                await self._close_out_crashed_attempt(
                    problem=problem,
                    config=config,
                    output_dir=output_dir,
                    exc_type=type(exc).__name__,
                    attempt_id=attempt_id,
                )
        # Set before the refusal below so a caller inspecting the runner after
        # a total-loss raise can still see which problems were lost.
        self._attempt_failures = tuple(failures)
        if not outcomes:
            raise ContractViolationError(
                f"every one of the {len(corpus)} attempt(s) failed; a round with no "
                "measured attempt has no cells, and recording it would put a baseline "
                "in the trajectory that a later round computes its deltas against"
            )
        return tuple(outcomes)

    async def _close_out_crashed_attempt(
        self,
        *,
        problem: Problem,
        config: RoundConfig,
        output_dir: Path,
        exc_type: str,
        attempt_id: str,
    ) -> None:
        """Give a contained attempt a terminal ``metrics.json``, or leave it alone.

        **What this fixes.** ``metrics.json`` is written once, at the end of
        :meth:`run_attempt`, so an attempt whose exception was contained never
        reaches it. ``verify`` reads an intact chain with no summary beside it
        as :attr:`~turing.research.loop.integrity.ReconcileState.INCOMPLETE`
        and exits ``2``, which is correct and load-bearing for a round that is
        *still running* — and wrong here, because this round is finished and
        that directory will never gain a summary. The whole results root then
        reports ``2`` forever, a pre-writeup gate demanding ``0`` can never
        pass on the tree, and an operator taught "``2`` means still running"
        learns to ignore it: a fresh cry-wolf, which is the exact failure
        class this contract exists to remove. Worse, the same event produced
        two different codes depending on *where* the attempt died — a crash
        before the first append leaves no ``metrics.jsonl``, so ``find_runs``
        never sees the directory and the root exits ``0``. One event class,
        one code, and the code is decided by whether the round finished, not
        by which line of ``run_attempt`` raised.

        **The record is honest, not a placeholder.** ``best_score`` is
        ``null``: this attempt produced no grade, and a floor value here would
        be the same fabricated-regression bug :meth:`run_attempts` refuses one
        level up. Every resource number is re-derived from the attempt's own
        log by :func:`_read_crashed_attempt_record`, so the summary reconciles
        with the log rather than merely coexisting with it — a summary built
        from anything else would turn an ``INCOMPLETE`` into a ``FAILED``, and
        ``FAILED`` is ``verify``'s word for dishonesty. ``final_state`` names
        the crash and is deliberately not an
        :class:`~turing.research.contracts.AttemptState` value; see
        :data:`_CRASHED_FINAL_STATE_PREFIX`. The loss stays disclosed exactly
        where it was — the ``research.attempt.crashed`` log, the round's
        ``all_attempts_completed`` gate, the verdict prefix, and the refused
        deltas — and ``INCOMPLETE`` goes back to meaning what the docs say it
        means: genuinely unfinished.

        **It cannot break the hash chain.** ``metrics.json`` is not a chained
        file: nothing here opens ``metrics.jsonl`` or the sidecar for writing,
        and the chain's digests do not cover the summary. The two files it
        does read, it reads.

        **It cannot mask the exception it is reporting on.** The caller is
        already inside ``except Exception``; every failure in here is caught
        and logged rather than raised, so a reporting problem can neither
        replace the original traceback (already logged, with the exception
        chained through ``logger.exception`` before this runs) nor escalate a
        contained per-attempt loss into a lost round. That is the same
        sanctioned-swallowed-error rule :meth:`run_attempt`'s emission block
        follows, applied on the error path where it matters more.

        **Three cases are deliberately left alone**, each ending in a code
        that is already right:

        * no ``metrics.jsonl`` (the attempt died before its first append —
          a workspace that could not be materialised, say). ``find_runs``
          keys on that file, so the directory is not a run and never affected
          the exit code. Writing a summary for a run that does not exist would
          *create* a finding, not clear one.
        * ``metrics.json`` already present. The crash happened after the
          summary landed, so the honest terminal record is already there and
          overwriting it with a cruder one would lose information.
        * a chain header that names a different attempt — a different
          ``attempt_id``, ``problem_id``, ``round_id`` or ``seed``, or one of
          those missing. Then this directory's chain belongs to an earlier
          generation and a summary written over it would be a claim about
          someone else's log. (A header that cannot be read *at all* is left
          alone too, by the generic guard below rather than by this check: with
          no header there is nothing to attribute a summary to, and it takes
          the same "write nothing" path.) This is reachable, not hypothetical:
          ``run_attempt`` materialises the workspace before it rotates the
          stale trio aside, so a crash in that window finds the previous
          generation's chain still in place, intact and summary-less — the
          honest ``INCOMPLETE`` shape a killed attempt leaves and rotation
          promises to preserve. :func:`_read_crashed_attempt_record` enforces
          the check (see its docstring for the full driven example, and for why
          ``attempt_id`` is the field that decides it); the refusal is logged
          under its own event name so it cannot be mistaken for reporting
          failing.

        ``attempt_id`` is a parameter rather than something read back off disk
        precisely so it can be *compared* with what is on disk. The caller
        mints it before calling :meth:`run_attempt` (:func:`_mint_attempt_id`)
        so it outlives the exception; reading it out of the header instead
        would make the guard compare the found chain against itself, which is
        what round 3's version effectively did and why a re-drive repeating
        ``run_id``, ``problem_id`` and ``seed`` — the noise floor's documented
        "re-run from seed 1" recovery, by construction — walked straight
        through it.

        Because the header must match the current attempt before anything is
        written, ``target_score`` — taken from *this* config's criterion — is
        necessarily the criterion the log was produced under. It could not be
        while an adopted foreign chain was reachable.
        """
        metrics_dir = output_dir / "attempts" / problem.id
        try:
            if (metrics_dir / "metrics.json").exists():
                return
            if not (metrics_dir / "metrics.jsonl").exists():
                return
            record = await asyncio.to_thread(
                _read_crashed_attempt_record,
                metrics_dir,
                output_dir / "escalations",
                expected_attempt_id=attempt_id,
                expected_problem_id=problem.id,
                expected_round_id=config.run_id,
                expected_seed=config.seed,
            )
            criterion = config.criterion_for(problem.id)
            await write_attempt_summary(
                metrics_dir,
                problem_id=problem.id,
                attempt_id=record.attempt_id,
                round_id=config.run_id,
                seed=config.seed,
                problem_type=problem.problem_type.value,
                split=problem.split.value,
                score_scale=problem.verifier.score_scale,
                outcome=record.outcome,
                final_state=f"{_CRASHED_FINAL_STATE_PREFIX}:{exc_type}",
                # No score, and no floor standing in for one. This attempt
                # was not graded; the cells above already exclude it.
                best_score=None,
                best_passed_correctness=None,
                baseline_score=record.baseline_score,
                target_score=None if criterion is None else criterion.min_score,
                final_progress=record.final_progress,
                consumed_steps=record.consumed_steps,
                consumed_tokens=record.consumed_tokens,
                consumed_wall_clock_seconds=record.consumed_wall_clock_seconds,
                cap_extensions=record.cap_extensions,
                escalation_count=record.escalation_count,
                steps_recorded=record.steps_recorded,
            )
        except _ForeignChainError as exc:
            # Not a failure: the guard fired and nothing was written. Logged
            # at ERROR anyway, because the *reason* it fired — a previous
            # generation's chain still sitting where this attempt's belongs —
            # is a real finding about the results tree, and the directory will
            # keep reporting INCOMPLETE until someone looks at it.
            logger.error(
                "research.results.crashed_attempt_summary_refused",
                problem_id=problem.id,
                run_id=config.run_id,
                round_index=config.round_index,
                directory=str(metrics_dir),
                reason=str(exc),
                detail=(
                    "this directory holds a different attempt's chain (an earlier "
                    "generation that was never rotated aside, because the crash "
                    "happened before rotation); no summary is written over it — that "
                    "would assert this round's identity and exception over a log they "
                    "never touched, and would destroy the honest INCOMPLETE state that "
                    "says the earlier attempt was killed unfinished"
                ),
            )
        except Exception:
            logger.exception(
                "research.results.crashed_attempt_summary_failed",
                problem_id=problem.id,
                run_id=config.run_id,
                round_index=config.round_index,
                directory=str(metrics_dir),
                detail=(
                    "the contained attempt keeps its intact chain with no summary, so "
                    "verify reports it INCOMPLETE and the results root exits 2; the "
                    "attempt loss itself is already reported by research.attempt.crashed"
                ),
            )
        else:
            logger.info(
                "research.results.crashed_attempt_summary_written",
                problem_id=problem.id,
                run_id=config.run_id,
                round_index=config.round_index,
                directory=str(metrics_dir),
                final_state=f"{_CRASHED_FINAL_STATE_PREFIX}:{exc_type}",
            )

    def _viewer_runs(self) -> list[str]:
        """Every attempt directory with a metrics chain, across every round on disk.

        Deriving this from the filesystem rather than the current round's
        in-memory ``outcomes`` is the fix for the bug where ``.viewer.json``
        showed only the latest round: ``outcomes`` only ever holds *this*
        round's attempts, so building the list from it — even merged into
        whatever the file already held — would still lose every earlier
        round the moment the runner process restarts between rounds and that
        in-memory state is gone. The directories are exactly as durable as
        ``metrics.jsonl`` itself, so re-deriving them from disk on every round
        costs nothing a restart doesn't already pay, and never depends on a
        prior ``.viewer.json`` having been written honestly (or at all).

        **The walk is depth-independent, and it has to be.** An attempt's
        metrics directory is ``attempts/<problem.id>``, and ``problem.id`` is
        a path component operators may plausibly namespace —
        ``"cuda/matmul-speedup"`` is a reasonable id and produces
        ``attempts/cuda/matmul-speedup/metrics.jsonl``. A single-segment glob
        (``attempts/*/metrics.jsonl``) matched exactly one path segment, so
        such a run vanished from ``.viewer.json`` with no log line and no
        warning while ``verify`` — whose walk has always been recursive —
        went on finding and checking it. Silently listing fewer runs than
        exist is the same class of fault as listing only the latest round,
        which this method was written to fix. Nesting stays supported; what
        ``contracts._reject_unsafe_problem_id`` refuses (RES-16) is the
        narrower set of ids that escape the results root or collide with a
        directory this runner mints itself.

        Rotated ``prior-N/`` chains (see :func:`_rotate_stale_metrics`) are
        still excluded, now by directory *name* rather than by depth, since
        depth no longer distinguishes them. That is exact for every
        directory this runner creates: ``prior-N/`` is only ever the last
        segment before a rotated ``metrics.jsonl``. The residual this filter
        used to concede — a problem id whose final segment is literally
        ``prior-<digits>``, which would be skipped — is closed at the other
        end: ``contracts._reject_unsafe_problem_id`` refuses such an id at
        problem-definition time, naming this filter as the reason.

        An attempt whose ``MetricsWriter`` never took its first
        :meth:`~turing.research.loop.results.MetricsWriter.append`
        (``metrics.jsonl`` is created lazily, on that first write) is
        correctly absent, the same as it would be from ``outcomes``.
        """
        loop_dir = self._trajectory.loop_dir
        results_root = loop_dir.parent
        return sorted(
            {
                match.parent.relative_to(results_root).as_posix()
                for match in loop_dir.glob("round-*/attempts/**/metrics.jsonl")
                if not _PRIOR_DIR_PATTERN.fullmatch(match.parent.name)
            }
        )

    @staticmethod
    def _floors_from_report(
        eval_set_hash: str,
        engine: EngineIdentity,
        noise_floor: NoiseFloorReport | None,
    ) -> tuple[NoiseFloor, ...]:
        """Bind a provenanced floor to this round, or refuse.

        A bare sequence of :class:`NoiseFloor` values has no eval-set hash and
        no engine. Accepting one would let a floor from anywhere license a
        gain — the gate that exists so "no noise floor, no verdict" cannot be
        satisfied by a number from a different corpus.
        """
        if noise_floor is None:
            return ()
        report_hash = getattr(noise_floor, "eval_set_hash", None)
        report_engine = getattr(noise_floor, "engine", None)
        floors = getattr(noise_floor, "floors", None)
        if not isinstance(report_hash, str) or report_engine is None or floors is None:
            raise ContractViolationError(
                "a noise floor must be a NoiseFloorReport bound to an eval set and "
                "engine; a bare sequence of floors has no provenance and a floor "
                "from anywhere would license every gain"
            )
        if report_hash != eval_set_hash:
            raise ContractViolationError(
                f"noise floor eval_set_hash {report_hash!r} does not match this "
                f"round's {eval_set_hash!r}; a floor from a different corpus "
                "is not a floor"
            )
        if report_engine != engine:
            raise ContractViolationError(
                "noise floor engine does not match this round's engine; a floor "
                "measured on a different scaffold is not a floor"
            )
        return tuple(floors)

    async def run_round(
        self,
        corpus: Sequence[Problem],
        config: RoundConfig,
        *,
        parent: RoundRecord | None = None,
        noise_floor: NoiseFloorReport | None = None,
    ) -> RoundOutcome:
        """Run the round, reduce it to cells, and append the trajectory row.

        Round wall-clock cost is machine time only. Time spent blocked in
        :meth:`EscalationChannel.request_decision` is subtracted, so driving
        function #3 cannot improve merely because the operator was interrupted
        less (that signal belongs to human-gate load).

        ``noise_floor`` must be a :class:`NoiseFloorReport` (eval set + engine
        bound). A bare sequence of floors has no provenance; a floor from
        anywhere would license every gain.
        """
        await self._trajectory.ensure_layout()
        output_dir = self._trajectory.round_dir(config.round_index)
        self._operator_wait_seconds = 0.0
        if not corpus:
            raise ContractViolationError("a round with no problems measures nothing")
        eval_set_hash = bind_eval_set_hash(corpus, config.eval_set_hash)
        floors_seq = self._floors_from_report(eval_set_hash, config.engine, noise_floor)
        started = self._clock.monotonic()
        logger.info(
            "research.round.started",
            round_index=config.round_index,
            run_id=config.run_id,
            parent_round_id=config.parent_round_id,
            eval_set_hash=eval_set_hash,
            problems=len(corpus),
            seed=config.seed,
            noise_floors=len(floors_seq),
        )
        if not floors_seq:
            logger.warning(
                "research.round.no_noise_floor",
                round_index=config.round_index,
                detail=(
                    "no seed-noise floor supplied; marginal gain cannot be called signal "
                    "or noise and the saturation verdict will be refused"
                ),
            )

        outcomes = await self.run_attempts(corpus, config, output_dir=output_dir)
        failures = self._attempt_failures
        if failures:
            logger.error(
                "research.round.attempts_lost",
                round_index=config.round_index,
                run_id=config.run_id,
                lost=[f.problem_id for f in failures],
                errors=[f.error for f in failures],
                measured=len(outcomes),
                corpus=len(corpus),
                detail=(
                    "these problems are absent from this round's cells and cost; the "
                    "round's n is smaller than the corpus and is not comparable "
                    "problem-for-problem with a round that measured all of them"
                ),
            )
        elapsed = max(0.0, self._clock.monotonic() - started - self._operator_wait_seconds)

        scored = tuple(
            ScoredProblem.from_result(o.problem, o.best_result, score_floors=config.score_floors)
            for o in outcomes
        )
        type_scores = build_type_scores(scored)
        cost = RoundCost(
            wall_clock_seconds=elapsed,
            tokens=sum(o.attempt.consumed.tokens for o in outcomes),
            # Measured attempts, not attempted ones. RoundCost is driving
            # function #3's numerator and ``type_scores`` is its denominator's
            # source; counting an attempt here whose score is missing from
            # there would compute cost-per-unit-gain over two different sets
            # of problems. ``wall_clock_seconds`` cannot be split that way (it
            # is one clock over the whole round) and ``tokens`` undercounts by
            # whatever a lost attempt spent before it raised, since its
            # ``Attempt`` died with it — so a round with a non-empty
            # ``failures`` list reads slightly cheap, and the
            # ``all_attempts_completed`` gate below is what says so.
            attempts=len(outcomes),
        )
        # Same undercount as cost.tokens, and it lands on driving function #4:
        # escalations a lost attempt raised before it died went to a real
        # operator but are not counted here, because the request list died with
        # the attempt. Human-gate load therefore reads low on a round whose
        # all_attempts_completed gate is False, which is the second reason that
        # gate has to be written rather than inferred from these numbers.
        escalation_count = sum(o.escalation_count for o in outcomes)

        parent_scores: tuple[TypeScore, ...] | None = None
        comparable = True
        parent_attempts_completed: bool | None = True
        if parent is not None:
            if config.parent_round_id != parent.run_id:
                raise ContractViolationError(
                    f"round {config.run_id!r} records parent {config.parent_round_id!r} but "
                    f"was handed round {parent.run_id!r} to compare against; a trajectory "
                    "whose lineage disagrees with its deltas is worse than none"
                )
            parent_scores = parent.type_scores
            comparable = parent.eval_set_hash == eval_set_hash
            if not comparable:
                logger.error(
                    "research.round.eval_set_changed",
                    round_index=config.round_index,
                    parent_eval_set_hash=parent.eval_set_hash,
                    eval_set_hash=eval_set_hash,
                    detail="rounds measured on different eval sets are not comparable",
                )
            parent_attempts_completed = _parent_attempts_completed(parent)
            if parent_attempts_completed is not True:
                logger.error(
                    "research.round.parent_attempts_lost",
                    round_index=config.round_index,
                    run_id=config.run_id,
                    parent_round_id=parent.run_id,
                    parent_all_attempts_completed=parent_attempts_completed,
                    detail=(
                        "the parent round did not measure its full corpus (or does not "
                        "record whether it did), so its cells are the reduced side of "
                        "every subtraction this round would report; the deltas and the "
                        "saturation verdicts are refused. Re-driving this round does not "
                        "fix it — the baseline has to be re-measured"
                    ),
                )

        floors = floors_by_cell(floors_seq)
        # Two separate facts, deliberately not folded into one flag.
        # ``comparable`` is "the parent measured the same corpus"; it is the
        # eval-set gate and nothing else, because ``trajectory.py`` derives
        # ``trajectory_restart`` from exactly that question and a lost attempt
        # is emphatically *not* a trajectory restart — the next round still
        # descends from this one. ``all_attempts_completed`` is "this round
        # measured all of that corpus". A delta needs both, and the two
        # refusals read differently in the verdict on purpose, because the
        # operator actions they call for are different (re-derive the corpus
        # vs. re-drive the lost problem).
        #
        # ``parent_attempts_completed`` is the third fact, and it is *this*
        # round's gate asked of the parent record rather than anything
        # recomputed here: a delta is a subtraction, and it is contaminated
        # by a missing problem on either side. It is deliberately not written
        # into ``gates`` below — every entry there is a statement about this
        # round, and a gate whose subject is a different round would be read
        # (by the desktop's flywheel pane, among others) as this round having
        # failed something. The refusal travels in the channel built for it
        # instead: ``refused_parent_attempt_lost`` per cell, in the record,
        # the trajectory row and the verdict line.
        all_attempts_completed = not failures
        deltas = compute_deltas(
            type_scores,
            parent_scores,
            floors,
            cost=cost,
            basis=config.cost_basis,
            comparable=comparable,
            all_attempts_completed=all_attempts_completed,
            parent_attempts_completed=parent_attempts_completed,
        )
        assessments = assess_saturation(
            type_scores,
            parent_scores,
            floors,
            comparable=comparable,
            all_attempts_completed=all_attempts_completed,
            parent_attempts_completed=parent_attempts_completed,
        )
        gates = {
            "eval_set_stable": comparable,
            "lineage_recorded": config.round_index == 0 or config.parent_round_id is not None,
            "noise_floor_available": bool(floors_seq),
            # False means at least one problem is missing from the cells
            # above. Every other number in this record is computed over the
            # attempts that survived, so without this gate a round of ten
            # measured problems and a round of eleven are indistinguishable
            # on disk. ``gates`` is written into both the round record and
            # trajectory.json's per-round ``constraints``, which is the
            # narrowest honest channel available here — ``round_verdict``
            # lives in metrics.py and takes only assessments, and
            # ``write_round_summary``'s payload is fixed by results.py.
            "all_attempts_completed": all_attempts_completed,
        }
        # Refusals first, never summarised away — the same rule
        # ``round_verdict`` applies to unscorable cells, applied to unmeasured
        # problems. The verdict string is the one line an operator reads off
        # a trajectory row, so a round missing a problem must say so there and
        # not only in a gate flag two levels down.
        verdict = round_verdict(assessments)
        if failures:
            lost = ", ".join(f.problem_id for f in failures)
            verdict = (
                f"{len(failures)} of {len(corpus)} attempt(s) failed and are absent from "
                f"every cell ({lost}); {verdict}"
            )
        record = RoundRecord(
            round_index=config.round_index,
            run_id=config.run_id,
            parent_round_id=config.parent_round_id,
            eval_set_hash=eval_set_hash,
            engine=config.engine,
            type_scores=type_scores,
            deltas=deltas,
            cost=cost,
            escalation_count=escalation_count,
            created_at_ms=self._clock.now_ms(),
            gates=gates,
            verdict=verdict,
        )
        await self._trajectory.write_round_record(
            record, scored=scored, assessments=assessments, noise_floors=floors_seq
        )
        row = await self._trajectory.append_round(
            record,
            assessments=assessments,
            noise_floors=floors_seq,
            cost_basis=config.cost_basis,
            scored=scored,
            seed=config.seed,
            cap=config.default_cap,
            verify_every_step=config.verify_every_step,
        )
        logger.info(
            "research.round.finished",
            round_index=config.round_index,
            run_id=config.run_id,
            cells={
                f"{ts.problem_type.value}/{ts.split.value}": ts.mean_score for ts in type_scores
            },
            escalations=escalation_count,
            wall_clock_seconds=elapsed,
            operator_wait_seconds=self._operator_wait_seconds,
            verdict=record.verdict,
        )

        # Reporting failures must not lose a round record that is otherwise
        # valid — the same one-sanctioned-swallowed-error reasoning as
        # run_attempt's summary/plot emission.
        try:
            deltas_by_cell = {(d.problem_type, d.split): d for d in record.deltas}
            cells: list[dict[str, object]] = []
            for ts in type_scores:
                cell: dict[str, object] = {
                    "cell": cell_key((ts.problem_type, ts.split)),
                    "problem_type": ts.problem_type.value,
                    "split": ts.split.value,
                    "mean_score": ts.mean_score,
                    "n": ts.n,
                    "correctness_passes": ts.correctness_passes,
                }
                delta = deltas_by_cell.get((ts.problem_type, ts.split))
                if delta is not None:
                    cell["marginal_gain"] = delta.marginal_gain
                    cell["noise_floor"] = delta.noise_floor
                    cell["cost_per_unit_gain"] = delta.cost_per_unit_gain
                cells.append(cell)

            await write_round_summary(
                output_dir,
                round_index=config.round_index,
                run_id=config.run_id,
                parent_round_id=config.parent_round_id,
                eval_set_hash=eval_set_hash,
                seed=config.seed,
                # Fix 4: match trajectory.json's append_round semantics.
                # "Comparable to parent" is not a yes/no fact about a round
                # with no parent -- it's not applicable, and trajectory.json
                # already encodes exactly that (`comparable` stays None
                # there when there is no previous row). `comparable` here
                # defaults to True even when `parent is None`, which made
                # the round summary claim comparability to a parent that
                # does not exist while the trajectory row for the same
                # round recorded null. trajectory.py owns that null and is
                # do-not-touch, so the summary is what changes to match it.
                #
                # It is ``comparable and all_attempts_completed``, not
                # ``comparable`` alone, and the asymmetry with the
                # identically-named trajectory.json field is intentional
                # rather than the drift it looks like. This field answers the
                # question a reader of a round summary is actually asking --
                # *may these numbers be compared with the parent's?* -- for
                # which a matching eval-set hash is necessary and not
                # sufficient: a round that lost an attempt reduced its cells
                # over a strict subset of the problems the parent's cells were
                # reduced over. trajectory.py's field is narrower by
                # construction (it is computed from the previous *row*, which
                # carries an eval_set_hash and knows nothing about lost
                # attempts) and, more importantly, must stay narrow, because
                # ``trajectory_restart`` is derived from it and a lost attempt
                # does not restart the trajectory. The row carries this
                # round's incomparability the other way instead: an empty
                # ``delta`` map, ``saturation`` entries reading
                # ``refused_attempt_lost``, and
                # ``constraints.all_attempts_completed: false``.
                #
                # The parent's own completeness is in this conjunction for the
                # same reason ``not failures`` is: the question is whether
                # these numbers may be compared with the parent's, and a
                # parent that measured a subset of the corpus (or is silent
                # about whether it did) makes the answer no from the other
                # side of the subtraction.
                comparable_to_parent=(
                    None
                    if parent is None
                    else (comparable and not failures and parent_attempts_completed is True)
                ),
                cells=cells,
                wall_clock_seconds=elapsed,
                tokens=cost.tokens,
                attempts=cost.attempts,
                escalations=escalation_count,
                verdict=record.verdict,
            )
            # Viewer config ahead of the plot, deliberately — this used to be
            # last. ``render_round_plot`` does ``float(cell["mean_score"])``
            # with no guard, so a missing/non-numeric mean_score raises and
            # everything after it in this block never runs; with the plot
            # last, that took the viewer config down too, and an unrelated
            # matplotlib problem left the desktop pane pointed at a stale (or
            # in round 0's case, nonexistent) run list. Both writes here are
            # cheap, structural JSON — nothing here should ever raise, so the
            # plot (the one call in this block with a genuine, known failure
            # mode) is what moves, not these.
            await write_viewer_config(self._trajectory.loop_dir.parent, runs=self._viewer_runs())
        except Exception:
            logger.exception(
                "research.results.round_emit_failed",
                round_index=config.round_index,
                run_id=config.run_id,
            )
        else:
            # Isolated in its own try/except rather than folded into the one
            # above: render_round_plot's known failure mode (unguarded
            # float(cell["mean_score"])) is a different fault than a summary
            # or viewer-config write failing outright, and collapsing both
            # into "round_emit_failed" would read, on an overnight log, as
            # "this round's results never landed" when the JSON in fact did.
            # `else` rather than unconditional: cells is only guaranteed
            # bound if the try above completed without raising.
            try:
                await render_round_plot(output_dir, cells)
            except Exception:
                logger.exception(
                    "research.results.round_plot_failed",
                    round_index=config.round_index,
                    run_id=config.run_id,
                    directory=str(output_dir),
                )

        return RoundOutcome(
            record=record,
            attempts=outcomes,
            scored=scored,
            assessments=assessments,
            trajectory_row=row,
            failures=failures,
        )
