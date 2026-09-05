"""The four numbers, and the refusals that keep them honest.

``driving-functions.md`` requires every round of a self-improving loop to
report four things:

1. **Primary score** — here, per ``(problem_type, split)`` cell and continuous.
   :func:`build_type_scores`.
2. **Marginal round gain**, in units of the seed-noise floor.
   :func:`compute_deltas`.
3. **Cost per unit gain** — wall-clock or tokens per point.
4. **Human-gate load** — escalations per round, which is just a count and
   therefore lives on :class:`~turing.research.contracts.RoundRecord` directly.

Two rules are enforced here rather than trusted to callers, because both are
ways a loop produces a beautiful fake curve:

**Never average across problem types.** A speedup ratio and a leaderboard
percentile do not share a scale, and a round that helps one family while
hurting another reads as flat once blended. Every function in this module
returns *cells*; none returns a corpus-wide scalar, and none may be added.

**No noise floor, no verdict.** ``marginal_gain`` is uninterpretable without a
measured spread, so :func:`compute_deltas` emits **no delta at all** for a cell
with no floor (rather than defaulting the floor to zero, which would make any
gain beat it), and :func:`assess_saturation` returns an explicit refusal.
Refusal is a required behaviour, not an error case: it is what the record says
when the measurement was not made.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

from turing.research.contracts import (
    SCORE_SCALE_LEADERBOARD_PERCENTILE,
    SCORE_SCALE_SPEEDUP,
    ContractViolationError,
    ProblemType,
    RoundDelta,
    Split,
    TypeScore,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from turing.research.contracts import Problem, RoundCost, VerificationResult

__all__ = [
    "DEFAULT_SCORE_FLOORS",
    "MIN_NOISE_FLOOR_SEEDS",
    "Cell",
    "CostBasis",
    "NoiseFloor",
    "NoiseFloorStatistic",
    "SaturationAssessment",
    "SaturationVerdict",
    "ScoredProblem",
    "assess_saturation",
    "build_type_scores",
    "compute_deltas",
    "cost_per_unit_gain",
    "floors_by_cell",
    "measure_noise_floor",
    "round_verdict",
    "scores_by_cell",
]

#: A ``(problem_type, split)`` cell — the finest grain at which scores are
#: comparable, and the coarsest at which they may be pooled.
Cell = tuple[ProblemType, Split]

#: ``driving-functions.md``: "run the same configuration at >=3 seeds and
#: measure the spread". Fewer seeds is not a smaller measurement, it is no
#: measurement — two points have no spread worth the name.
MIN_NOISE_FLOOR_SEEDS = 3

#: What an *unscored* problem contributes to its cell. Dropping unscored
#: problems instead would bias every cell upward by exactly the problems that
#: went worst (survivorship), so a floor is required; there is no universal
#: one, so it is per score scale.
#:
#: The floor is the **worst grade the scale actually emits**, not a "did
#: nothing" midpoint. Speedup ``1.0`` means unchanged — a successful
#: measurement — and a failed correctness gate scores ``0.0``. Flooring an
#: abandon at ``1.0`` would let the operator raise the primary score by
#: refusing to finish hopeless problems.
#:
#: This map covers the two scales the loop ships with; it is deliberately
#: **not** the only place a floor can come from. A problem scored on a scale
#: this map does not know (loss, throughput, anything else an operator's
#: verifier invents) declares its own floor on
#: :attr:`~turing.research.contracts.Verifier.score_floor` instead of forcing
#: every corpus author to extend a shared module-level table for a scale only
#: their problem uses. :meth:`ScoredProblem.from_result` resolves the two in a
#: fixed order — **the round's ``score_floors`` table wins, then the
#: problem's own declared floor, then refuse**:
#:
#: 1. ``score_floors`` (this map, or :class:`~turing.research.loop.runner.RoundConfig`'s
#:    override of it) is checked first, because it is the operator's own
#:    table for *this round* and an explicit round-level choice should not be
#:    silently shadowed by whatever a verifier happened to declare.
#: 2. :attr:`~turing.research.contracts.Verifier.score_floor` is checked
#:    second — the problem's own declaration, used only when the round did
#:    not already have an answer for that scale.
#: 3. Neither: :class:`~turing.research.contracts.ContractViolationError`,
#:    exactly as when no floor existed at all.
DEFAULT_SCORE_FLOORS: Mapping[str, float] = MappingProxyType(
    {
        SCORE_SCALE_SPEEDUP: 0.0,  # failed gate; worse than an unchanged baseline
        SCORE_SCALE_LEADERBOARD_PERCENTILE: 0.0,  # bottom of the leaderboard
    }
)


class CostBasis(str, Enum):  # noqa: UP042
    """Which cost number sits in the numerator of driving function #3.

    Dollars are deliberately absent: metering was retired, and reintroducing
    it here would resurrect the ``BudgetGate`` semantics the step/token/
    wall-clock cap replaced.
    """

    WALL_CLOCK = "wall_clock_seconds"
    TOKENS = "tokens"


class NoiseFloorStatistic(str, Enum):  # noqa: UP042
    """How the seed spread is reduced to one number.

    ``STDEV`` (sample standard deviation across per-seed cell means) is the
    default reading of "spread". ``RANGE`` is the conservative alternative —
    at three seeds it runs ~1.7x larger, so it makes "beats the noise floor"
    harder to claim. Both numbers are always recorded; this only selects which
    one the verdict is taken against.
    """

    STDEV = "stdev"
    RANGE = "range"


# --------------------------------------------------------------------------- #
# Primary score — driving function #1
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ScoredProblem:
    """One problem's contribution to one cell.

    ``scored`` is ``False`` when the attempt produced no usable verification
    (the verifier never ran, raised, or reported
    :attr:`~turing.research.contracts.VerificationResult.harness_failed`).
    The problem still enters its cell at the scale's floor — see
    :data:`DEFAULT_SCORE_FLOORS` — and the flag survives into the round
    artifact so the reader can see how much of a cell is real measurement.
    """

    problem_id: str
    problem_type: ProblemType
    split: Split
    score: float
    passed_correctness: bool
    score_scale: str
    scored: bool = True

    @classmethod
    def from_result(
        cls,
        problem: Problem,
        result: VerificationResult | None,
        *,
        score_floors: Mapping[str, float] | None = None,
    ) -> ScoredProblem:
        """Build a cell contribution, flooring an unscored problem.

        The floor for a scale with no usable verification is resolved in a
        fixed order — see :data:`DEFAULT_SCORE_FLOORS` for the reasoning:

        1. ``score_floors`` (the round's table; :data:`DEFAULT_SCORE_FLOORS`
           when the caller passes none) — an operator-level override, checked
           first so it can shadow a problem's own declaration.
        2. ``problem.verifier.score_floor`` — the problem's own declared
           floor, used only when the round's table has no entry for this
           scale.
        3. Neither: :class:`~turing.research.contracts.ContractViolationError`.
        """
        scale = problem.verifier.score_scale
        if result is not None and not result.harness_failed:
            return cls(
                problem_id=problem.id,
                problem_type=problem.problem_type,
                split=problem.split,
                score=result.score,
                passed_correctness=result.passed_correctness,
                score_scale=scale,
                scored=True,
            )
        floors = DEFAULT_SCORE_FLOORS if score_floors is None else score_floors
        if scale in floors:
            floor_value = floors[scale]
        elif problem.verifier.score_floor is not None:
            floor_value = problem.verifier.score_floor
        else:
            raise ContractViolationError(
                f"problem {problem.id!r} produced no verification and score scale "
                f"{scale!r} has no declared floor; dropping it would bias the "
                f"{problem.problem_type.value}/{problem.split.value} cell upward by "
                "exactly the attempts that went worst"
            )
        return cls(
            problem_id=problem.id,
            problem_type=problem.problem_type,
            split=problem.split,
            score=floor_value,
            passed_correctness=False,
            score_scale=scale,
            scored=False,
        )


def build_type_scores(scored: Sequence[ScoredProblem]) -> tuple[TypeScore, ...]:
    """Group per-problem scores into ``(type, split)`` cells.

    Returns one :class:`TypeScore` per cell, ordered by type then split so the
    trajectory is byte-stable across runs. **There is no corpus-wide return
    value and there must never be one.**

    A cell's mean is only a number if every score in it is on the same scale,
    so a cell that mixes ``score_scale`` values is refused outright. Widening
    which scales exist (declared floors, RES-15) must never make two of them
    poolable — flooring a ``val_loss`` problem beside a ``speedup`` problem
    would average seconds with losses and call it a score.
    """
    buckets: dict[Cell, dict[str, float]] = {}
    passes: dict[Cell, int] = {}
    scales: dict[Cell, tuple[str, str]] = {}
    for item in scored:
        cell = (item.problem_type, item.split)
        bucket = buckets.setdefault(cell, {})
        if item.problem_id in bucket:
            raise ContractViolationError(
                f"problem {item.problem_id!r} scored twice in one round; each problem "
                "contributes to its cell exactly once"
            )
        first = scales.setdefault(cell, (item.problem_id, item.score_scale))
        if first[1] != item.score_scale:
            raise ContractViolationError(
                f"cell {cell[0].value}/{cell[1].value} mixes score scales: problem "
                f"{first[0]!r} scores on {first[1]!r} but {item.problem_id!r} scores on "
                f"{item.score_scale!r}; scores on different scales are never averaged"
            )
        bucket[item.problem_id] = item.score
        passes[cell] = passes.get(cell, 0) + (1 if item.passed_correctness else 0)
    return tuple(
        TypeScore(
            problem_type=cell[0],
            split=cell[1],
            scores=buckets[cell],
            correctness_passes=passes[cell],
        )
        for cell in sorted(buckets, key=lambda c: (c[0].value, c[1].value))
    )


def scores_by_cell(type_scores: Sequence[TypeScore]) -> Mapping[Cell, TypeScore]:
    """Index cells for lookup. Deliberately not a reduction."""
    return MappingProxyType({(ts.problem_type, ts.split): ts for ts in type_scores})


# --------------------------------------------------------------------------- #
# Noise floor — measured before round 0
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class NoiseFloor:
    """The seed spread for one cell, measured at >= 3 seeds before round 0.

    Without this, "round 4 improved by 0.8 points" is an uninterpretable
    statement and saturation cannot be defined — so the noise floor has first
    claim on an opportunistic budget. Measuring it late makes every earlier
    number uninterpretable retroactively.
    """

    problem_type: ProblemType
    split: Split
    seeds: tuple[int, ...]
    per_seed_mean: Mapping[int, float]
    stdev: float
    spread_range: float
    statistic: NoiseFloorStatistic = NoiseFloorStatistic.STDEV

    def __post_init__(self) -> None:
        if len(self.seeds) < MIN_NOISE_FLOOR_SEEDS:
            raise ContractViolationError(
                f"a noise floor needs at least {MIN_NOISE_FLOOR_SEEDS} seeds, got {len(self.seeds)}"
            )
        object.__setattr__(self, "seeds", tuple(self.seeds))
        object.__setattr__(self, "per_seed_mean", MappingProxyType(dict(self.per_seed_mean)))

    @property
    def cell(self) -> Cell:
        return (self.problem_type, self.split)

    @property
    def value(self) -> float:
        """The number a marginal gain is compared against."""
        if self.statistic is NoiseFloorStatistic.RANGE:
            return self.spread_range
        return self.stdev

    @property
    def is_degenerate(self) -> bool:
        """True when the seeds produced no spread at all.

        A stochastic agent measured at three seeds with *identical* cell means
        has not resolved its own noise — it has produced a floor of zero, past
        which every gain trivially "beats the noise". Treated as a refusal
        rather than a licence.
        """
        return self.value <= 0.0


def measure_noise_floor(
    per_seed_scores: Mapping[int, Sequence[TypeScore]],
    *,
    statistic: NoiseFloorStatistic = NoiseFloorStatistic.STDEV,
) -> tuple[NoiseFloor, ...]:
    """Reduce >=3 same-config seed runs to one floor per cell.

    Args:
        per_seed_scores: seed -> that seed's cells, from an identical config.
        statistic: which spread number the verdict is taken against.

    Raises:
        ContractViolationError: fewer than :data:`MIN_NOISE_FLOOR_SEEDS` seeds,
            or a cell missing from some seed (an incomplete run would make the
            spread a measure of which problems ran, not of the agent).
    """
    if len(per_seed_scores) < MIN_NOISE_FLOOR_SEEDS:
        raise ContractViolationError(
            f"the noise floor needs at least {MIN_NOISE_FLOOR_SEEDS} seeds of the same "
            f"config, got {len(per_seed_scores)}"
        )
    seeds = tuple(sorted(per_seed_scores))
    indexed = {seed: scores_by_cell(per_seed_scores[seed]) for seed in seeds}
    cells: list[Cell] = []
    for seed in seeds:
        for cell in indexed[seed]:
            if cell not in cells:
                cells.append(cell)
    floors: list[NoiseFloor] = []
    for cell in sorted(cells, key=lambda c: (c[0].value, c[1].value)):
        missing = [seed for seed in seeds if cell not in indexed[seed]]
        if missing:
            raise ContractViolationError(
                f"cell {cell[0].value}/{cell[1].value} is missing from seeds {missing}; "
                "a noise floor measured on a shifting set of problems measures the set"
            )
        means = {seed: indexed[seed][cell].mean_score for seed in seeds}
        values = list(means.values())
        floors.append(
            NoiseFloor(
                problem_type=cell[0],
                split=cell[1],
                seeds=seeds,
                per_seed_mean=means,
                stdev=statistics.stdev(values),
                spread_range=max(values) - min(values),
                statistic=statistic,
            )
        )
    return tuple(floors)


def floors_by_cell(floors: Sequence[NoiseFloor]) -> Mapping[Cell, NoiseFloor]:
    return MappingProxyType({floor.cell: floor for floor in floors})


# --------------------------------------------------------------------------- #
# Marginal gain and cost per unit gain — driving functions #2 and #3
# --------------------------------------------------------------------------- #


def cost_per_unit_gain(
    cost: RoundCost,
    marginal_gain: float,
    *,
    basis: CostBasis = CostBasis.WALL_CLOCK,
) -> float | None:
    """Round cost per point of gain in one cell, or ``None`` when undefined.

    The round's cost is indivisible — attempts share one engine, one scaffold
    and one wall clock — so it is attributed *whole* to each cell rather than
    apportioned by some invented weighting. These numbers therefore do not add
    up across cells, and are read per cell only.

    ``None`` when the gain is not positive: the division is undefined and any
    sentinel would be read as a measurement.
    """
    if marginal_gain <= 0:
        return None
    numerator = cost.wall_clock_seconds if basis is CostBasis.WALL_CLOCK else float(cost.tokens)
    return numerator / marginal_gain


def compute_deltas(
    current: Sequence[TypeScore],
    parent: Sequence[TypeScore] | None,
    floors: Mapping[Cell, NoiseFloor],
    *,
    cost: RoundCost,
    basis: CostBasis = CostBasis.WALL_CLOCK,
    comparable: bool = True,
    all_attempts_completed: bool = True,
    parent_attempts_completed: bool | None = True,
) -> tuple[RoundDelta, ...]:
    """Per-cell marginal gain, paired with the floor it is read against.

    A delta is emitted **only** for a cell that has both a parent measurement
    and a *resolved* noise floor. A cell with no floor gets no delta at all —
    substituting ``noise_floor=0.0`` would silently license every gain, which
    is the exact mechanism behind fake curves. A cell whose measured floor is
    zero is treated the same way, because
    :attr:`~turing.research.contracts.RoundDelta.beats_noise_floor` against a
    zero floor is true for any positive gain and would read as a finding. The
    absence is visible: the record's ``deltas`` tuple is shorter than its
    ``type_scores`` tuple, the marginal gain is still reported by
    :func:`assess_saturation`, and the refusal says so in words.

    ``comparable=False`` (rounds measured on different eval sets) suppresses
    every delta: a curve drawn across an eval-set change is fiction.

    ``all_attempts_completed=False`` suppresses every delta for the same
    reason one step down. The eval set is the corpus a round *intended* to
    measure; this flag is whether it actually measured all of it. A round that
    lost an attempt has cells reduced over a strict subset of the problems the
    parent's cells were reduced over, so the difference of the two means is
    partly the agent and partly which problems dropped out — and, exactly as
    with an eval-set change, nothing downstream can separate the two again.
    The failure mode is not theoretical or symmetric: it is *biased*, because
    a mean rises when its weakest member goes missing, so a contained harness
    crash manufactures a marginal gain out of a bug in the runner. This is the
    argument ``noise_floor.py`` already makes for refusing a seed that lost an
    attempt rather than reducing a floor from it; a round-over-round delta is
    the number that floor exists to be read against, and it gets the same
    answer.

    Suppressing the delta rather than flagging it is deliberate, and matches
    the no-floor case above. A number that exists will be read; a caveat
    beside it will not. The absence is machine-readable — the ``deltas`` tuple
    is empty, :func:`assess_saturation` returns
    :attr:`SaturationVerdict.REFUSED_ATTEMPT_LOST` naming the cause, and the
    round record's ``all_attempts_completed`` gate is ``False``.

    Refusal is round-wide, not per cell, even though a cell that lost nothing
    still has a comparable mean. Two reasons, both structural rather than
    conservative: ``cost_per_unit_gain`` divides *round* cost — one wall clock
    over the whole round, and a token total the lost attempt's spend is
    missing from — so every cell's driving-function-#3 number is contaminated
    by the loss regardless of which cell it fell in; and ``noise_floor.py``
    refuses the whole seed rather than the affected cell for the same shape of
    fault, so per-cell salvage here would leave two neighbouring modules
    disagreeing about what a lost attempt costs a measurement.

    ``parent_attempts_completed`` is the **same refusal read off the other
    side of the subtraction, and without it the fix above is one-sided.**
    "Cells reduced over a strict subset of the problems the parent's cells
    were reduced over" is a statement about a *pair* of rounds: it is equally
    true when the child measured everything and the parent — the round whose
    means are the ``before`` in every subtraction here — is the one missing a
    problem. Driven for real, a round 0 that lost its *strongest* problem
    recorded a cell mean of 0.5 over ``n=1`` and correctly refused its own
    deltas; round 1 then measured the identical corpus with the identical
    agent, reduced 1.75 over ``n=2``, and emitted ``marginal_gain=+1.25``
    with every gate green. Nothing about that number is the agent: it is
    "which problems ran in the parent". Losing the *weak* problem in the
    parent manufactures the mirror-image phantom regression, so the bias
    argument above applies with its sign flipped, not weakened.

    **``None`` — the parent record carries no ``all_attempts_completed``
    gate — is refused, not trusted.** Records written before that gate
    existed are silent about the very fact this argument turns on, and the
    two readings of that silence are not symmetric: trusting it re-opens
    exactly the manufactured gain above (emitted with every gate green, which
    is the property that makes it dangerous), while refusing it costs a delta
    that can be recovered the moment the parent is re-driven under a runner
    that writes the gate. Every other unresolved basis in this module — no
    floor, a degenerate floor, a changed eval set — already resolves the same
    way: refuse, and say which measurement was missing.
    """
    if (
        parent is None
        or not comparable
        or not all_attempts_completed
        or parent_attempts_completed is not True
    ):
        return ()
    previous = scores_by_cell(parent)
    deltas: list[RoundDelta] = []
    for ts in current:
        cell = (ts.problem_type, ts.split)
        before = previous.get(cell)
        floor = floors.get(cell)
        if before is None or floor is None or floor.is_degenerate:
            continue
        gain = ts.mean_score - before.mean_score
        deltas.append(
            RoundDelta(
                problem_type=cell[0],
                split=cell[1],
                marginal_gain=gain,
                noise_floor=floor.value,
                cost_per_unit_gain=cost_per_unit_gain(cost, gain, basis=basis),
            )
        )
    return tuple(deltas)


# --------------------------------------------------------------------------- #
# Saturation — and the refusal to guess
# --------------------------------------------------------------------------- #


class SaturationVerdict(str, Enum):  # noqa: UP042
    """Whether a cell is still improving — or why that cannot be said.

    The six ``REFUSED_*`` members are not error codes. They are the honest
    answer when the measurement required to make the call was not made, and
    they are written into ``trajectory.json`` verbatim so a reader cannot
    mistake a missing verdict for a flat one.

    ``REFUSED_ATTEMPT_LOST`` and ``REFUSED_PARENT_ATTEMPT_LOST`` are the same
    fault on the two sides of one subtraction, and they are separate members
    rather than one because the operator action differs: the first says *this*
    round is short a problem, which a re-drive of that problem fixes; the
    second says the *baseline* is short one, which no amount of re-driving
    this round will repair — the parent has to be re-measured (or a later,
    complete round adopted as the baseline) before any delta from it means
    anything. One member for both would leave an operator re-running the wrong
    round and watching the same refusal come back.
    """

    IMPROVING = "improving"
    SATURATED = "saturated"
    REFUSED_NO_PARENT = "refused_no_parent"
    REFUSED_NO_NOISE_FLOOR = "refused_no_noise_floor"
    REFUSED_DEGENERATE_NOISE_FLOOR = "refused_degenerate_noise_floor"
    REFUSED_EVAL_SET_CHANGED = "refused_eval_set_changed"
    REFUSED_ATTEMPT_LOST = "refused_attempt_lost"
    REFUSED_PARENT_ATTEMPT_LOST = "refused_parent_attempt_lost"


@dataclass(frozen=True, slots=True)
class SaturationAssessment:
    """One cell's saturation call, or the refusal to make one."""

    problem_type: ProblemType
    split: Split
    verdict: SaturationVerdict
    reason: str
    marginal_gain: float | None = None
    noise_floor: float | None = None
    gain_in_noise_units: float | None = None

    @property
    def cell(self) -> Cell:
        return (self.problem_type, self.split)

    @property
    def is_refusal(self) -> bool:
        return self.verdict.value.startswith("refused_")


def assess_saturation(
    current: Sequence[TypeScore],
    parent: Sequence[TypeScore] | None,
    floors: Mapping[Cell, NoiseFloor],
    *,
    comparable: bool = True,
    all_attempts_completed: bool = True,
    parent_attempts_completed: bool | None = True,
) -> tuple[SaturationAssessment, ...]:
    """Per-cell saturation verdicts, refusing wherever the basis is missing.

    Saturation is "marginal gain below the seed-noise floor". Each of the six
    ways that sentence can fail to apply gets its own refusal rather than a
    fabricated number:

    * round 0 has no parent to be a delta *from*;
    * the eval set changed, so the two rounds are not on the same axes;
    * an attempt was lost, so the round measured a strict subset of the
      problems the parent measured and the difference of the two means is
      partly the agent and partly the missing problem — see
      :func:`compute_deltas` for why that is refused rather than flagged, and
      why the refusal is round-wide;
    * the *parent* lost an attempt (or is silent about whether it did), so the
      subtraction's ``before`` is the reduced mean and the same contamination
      arrives from the other side — see :func:`compute_deltas` for the driven
      example and for why an absent gate refuses rather than trusts;
    * no floor was measured for the cell;
    * the measured floor is zero, so "beats the noise" resolves nothing.

    The gain is deliberately **not** carried on the attempt-lost refusal, and
    that is the difference between this refusal and the two floor ones below
    it. ``REFUSED_NO_NOISE_FLOOR`` reports ``marginal_gain`` because the gain
    is a real difference between two comparable means that simply has no floor
    to be judged against — it is a measurement missing its yardstick. A gain
    computed across a shifting problem set is not a measurement at all, so
    there is nothing honest to report; reporting it anyway is precisely how
    ``"a marginal gain of +1.25"`` reached a verdict line for a round whose
    only change was that its weakest problem crashed. The parent-side refusal
    carries no gain for the identical reason — it is the same shifting problem
    set, whichever of the two rounds it moved in.

    **When both rounds are lossy the round's own loss is the verdict**, with
    the parent's named in the same reason string rather than dropped. Naming
    only the parent would contradict this record's own
    ``all_attempts_completed`` gate and its verdict prefix, both of which
    already say this round is short a problem; naming only this round's loss
    would send an operator to re-drive a round whose delta is refused again
    the moment it completes.
    """
    assessments: list[SaturationAssessment] = []
    previous = scores_by_cell(parent) if parent is not None else {}
    for ts in current:
        cell = (ts.problem_type, ts.split)
        label = f"{cell[0].value}/{cell[1].value}"
        if parent is None:
            assessments.append(
                SaturationAssessment(
                    problem_type=cell[0],
                    split=cell[1],
                    verdict=SaturationVerdict.REFUSED_NO_PARENT,
                    reason=f"{label}: no parent round; a baseline has no marginal gain",
                )
            )
            continue
        if not comparable:
            assessments.append(
                SaturationAssessment(
                    problem_type=cell[0],
                    split=cell[1],
                    verdict=SaturationVerdict.REFUSED_EVAL_SET_CHANGED,
                    reason=(
                        f"{label}: eval set changed since the parent round; the "
                        "trajectory restarts and no delta is defined across the change"
                    ),
                )
            )
            continue
        if not all_attempts_completed:
            reason = (
                f"{label}: at least one attempt was lost, so this round measured "
                "fewer problems than its parent; a difference of means over a "
                "shifting problem set measures the set as much as the agent and "
                "no delta is reported"
            )
            if parent_attempts_completed is not True:
                reason += (
                    " (the parent round is also not a full-corpus measurement, so "
                    "re-driving this round alone will not restore the delta)"
                )
            assessments.append(
                SaturationAssessment(
                    problem_type=cell[0],
                    split=cell[1],
                    verdict=SaturationVerdict.REFUSED_ATTEMPT_LOST,
                    reason=reason,
                )
            )
            continue
        if parent_attempts_completed is not True:
            assessments.append(
                SaturationAssessment(
                    problem_type=cell[0],
                    split=cell[1],
                    verdict=SaturationVerdict.REFUSED_PARENT_ATTEMPT_LOST,
                    reason=(
                        f"{label}: the parent round "
                        + (
                            "lost at least one attempt"
                            if parent_attempts_completed is False
                            else "records no all_attempts_completed gate, so whether it "
                            "measured the full corpus is unknown"
                        )
                        + ", so this round's cells cover problems the parent's cells did "
                        "not; a difference of means over a shifting problem set measures "
                        "the set as much as the agent and no delta is reported"
                    ),
                )
            )
            continue
        before = previous.get(cell)
        if before is None:
            assessments.append(
                SaturationAssessment(
                    problem_type=cell[0],
                    split=cell[1],
                    verdict=SaturationVerdict.REFUSED_NO_PARENT,
                    reason=f"{label}: cell absent from the parent round",
                )
            )
            continue
        gain = ts.mean_score - before.mean_score
        floor = floors.get(cell)
        if floor is None:
            assessments.append(
                SaturationAssessment(
                    problem_type=cell[0],
                    split=cell[1],
                    verdict=SaturationVerdict.REFUSED_NO_NOISE_FLOOR,
                    reason=(
                        f"{label}: no noise floor measured; a marginal gain of {gain:+.6g} "
                        "cannot be called signal or noise"
                    ),
                    marginal_gain=gain,
                )
            )
            continue
        if floor.is_degenerate:
            assessments.append(
                SaturationAssessment(
                    problem_type=cell[0],
                    split=cell[1],
                    verdict=SaturationVerdict.REFUSED_DEGENERATE_NOISE_FLOOR,
                    reason=(
                        f"{label}: measured noise floor is {floor.value:.6g} across "
                        f"{len(floor.seeds)} seeds; a zero spread resolves nothing to "
                        "compare a gain against"
                    ),
                    marginal_gain=gain,
                    noise_floor=floor.value,
                )
            )
            continue
        units = gain / floor.value
        beats = gain > floor.value
        assessments.append(
            SaturationAssessment(
                problem_type=cell[0],
                split=cell[1],
                verdict=(SaturationVerdict.IMPROVING if beats else SaturationVerdict.SATURATED),
                reason=(
                    f"{label}: gain {gain:+.6g} = {units:+.2f}x the noise floor "
                    f"({floor.value:.6g}, {floor.statistic.value} of "
                    f"{len(floor.seeds)} seeds)"
                    + ("" if beats else " — below the floor, saturation candidate")
                ),
                marginal_gain=gain,
                noise_floor=floor.value,
                gain_in_noise_units=units,
            )
        )
    return tuple(assessments)


def round_verdict(assessments: Sequence[SaturationAssessment]) -> str:
    """One human-readable line for the trajectory row.

    Refusals are reported first and never summarised away: a round whose
    verdict was refused must not read as a round that came out flat. Cells
    that *did* get a call still follow the refusals — dropping them would
    hide an improving family behind one cell that could not be scored.
    """
    if not assessments:
        return "no cells scored"
    refusals = [a for a in assessments if a.is_refusal]
    others = [a for a in assessments if not a.is_refusal]
    return "; ".join(a.reason for a in (*refusals, *others))
