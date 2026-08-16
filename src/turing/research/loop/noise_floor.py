"""The noise-floor run: the same config at >=3 seeds, before round 0.

``driving-functions.md`` calls this "the single most skippable and least
skippable step in the whole protocol". Without it:

* "round 4 improved by 0.8 points" is an uninterpretable statement;
* saturation cannot be defined, so the stopping criterion cannot exist;
* every round-over-round comparison is vulnerable to the objection that it is
  reading noise, and the objection is correct roughly as often as not.

The brief adds a scheduling consequence: because compute is an opportunistic
subscription, **the noise floor has first claim on it** — measuring it late
makes every earlier number uninterpretable retroactively.

Its runs are *not rounds*. They are written under ``noise-floor/seed-<n>/``
with the reduced floors in ``noise-floor/noise-floor.json``, and they never
enter ``trajectory.json``: a seed run has no parent and is not a point on the
curve. The wrinkle recorded in the brief — the agent is stochastic *within* a
project as well as across seeds — is why every seed's per-problem scores are
kept in the artifact rather than only the reduced spread.

Because it is not a round, it does not inherit a round's tolerances either.
:meth:`RoundRunner.run_attempts` deliberately *contains* an attempt that
raises: the round keeps its other ten problems, drops the lost one from its
cells, and says so through ``all_attempts_completed``. That is right for a
round and wrong here — see :meth:`NoiseFloorRunner.run`, which refuses a seed
that lost an attempt instead of reducing a floor from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import structlog

from turing.research.contracts import ContractViolationError
from turing.research.loop.metrics import (
    DEFAULT_SCORE_FLOORS,
    MIN_NOISE_FLOOR_SEEDS,
    NoiseFloorStatistic,
    ScoredProblem,
    build_type_scores,
    floors_by_cell,
    measure_noise_floor,
)
from turing.research.loop.runner import RoundConfig
from turing.research.loop.trajectory import (
    RunIdentity,
    cell_key,
    encode_noise_floor,
    encode_type_score,
)
from turing.research.problems.adapter import bind_eval_set_hash

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from turing.research.contracts import Cap, EngineIdentity, Problem, TypeScore
    from turing.research.loop.metrics import Cell, NoiseFloor
    from turing.research.loop.runner import AttemptFailure, PassCriterion, RoundRunner
    from turing.research.loop.trajectory import TrajectoryStore

logger = structlog.get_logger(__name__)

__all__ = ["NoiseFloorConfig", "NoiseFloorReport", "NoiseFloorRunner"]


def _incomplete_seed_refusal(
    *,
    seed: int,
    run_id: str,
    corpus_size: int,
    failures: Sequence[AttemptFailure],
) -> ContractViolationError:
    """Build the refusal for a seed that did not measure the whole corpus.

    A function rather than an inline ``raise`` because two call sites reach it:
    a seed that lost *some* attempts (:attr:`RoundRunner.attempt_failures` is
    non-empty and ``run_attempts`` returned) and a seed that lost *all* of them
    (``run_attempts`` refuses on its own, with a message that does not know
    which seed it was running). Both are the same fault and an operator reading
    the log at 3am should not have to tell them apart, so both produce this
    message.

    The message names the seed, its run id and every lost problem id, because
    the whole point of failing here is that the next person to look does not
    have to reconstruct it from a traceback and a directory listing.
    """
    lost = ", ".join(f"{failure.problem_id} ({failure.error})" for failure in failures)
    return ContractViolationError(
        f"noise-floor seed {seed} (run {run_id!r}) lost {len(failures)} of "
        f"{corpus_size} attempt(s): {lost}. The floor is refused rather than "
        "reduced from what survived: a floor is the yardstick every later round's "
        "marginal gain is called signal or noise against, so it is only meaningful "
        "measured over the same problem set those rounds are measured over. A seed "
        "missing a problem contributes a cell mean taken over a different set than "
        "its sibling seeds, so the resulting spread is part run-to-run variance and "
        "part 'which problems ran' — and nothing downstream can separate the two "
        "again. Fix the cause and re-run the noise floor from seed 1."
    )


@dataclass(frozen=True, slots=True)
class NoiseFloorConfig:
    """One config, several seeds. Everything except the seed must be identical."""

    run_id: str
    eval_set_hash: str
    engine: EngineIdentity
    seeds: tuple[int, ...]
    default_cap: Cap
    statistic: NoiseFloorStatistic = NoiseFloorStatistic.STDEV
    pass_criteria: Mapping[str, PassCriterion] = field(default_factory=dict)
    score_floors: Mapping[str, float] = field(default_factory=lambda: DEFAULT_SCORE_FLOORS)
    escalate_on_cap_exhaustion: bool = False
    max_escalations_per_attempt: int = 3

    def __post_init__(self) -> None:
        if len(self.seeds) < MIN_NOISE_FLOOR_SEEDS:
            raise ContractViolationError(
                f"the noise floor needs at least {MIN_NOISE_FLOOR_SEEDS} seeds, got "
                f"{len(self.seeds)}; two points have no spread worth the name"
            )
        if len(set(self.seeds)) != len(self.seeds):
            raise ContractViolationError(
                "seeds must be distinct; re-running one seed measures determinism, not noise"
            )
        object.__setattr__(self, "seeds", tuple(self.seeds))

    def round_config_for(self, seed: int) -> RoundConfig:
        """The per-seed execution config.

        ``round_index=0`` and ``parent_round_id=None`` because a seed run is a
        baseline measurement with no lineage — it is not a round, and the
        ``run_id`` says so.
        """
        return RoundConfig(
            round_index=0,
            run_id=f"{self.run_id}-seed-{seed}",
            parent_round_id=None,
            eval_set_hash=self.eval_set_hash,
            engine=self.engine,
            seed=seed,
            default_cap=self.default_cap,
            escalate_on_cap_exhaustion=self.escalate_on_cap_exhaustion,
            max_escalations_per_attempt=self.max_escalations_per_attempt,
            pass_criteria=self.pass_criteria,
            score_floors=self.score_floors,
        )


@dataclass(frozen=True, slots=True)
class NoiseFloorReport:
    """Per-cell floors, plus the per-seed cells they were reduced from."""

    run_id: str
    eval_set_hash: str
    engine: EngineIdentity
    seeds: tuple[int, ...]
    floors: tuple[NoiseFloor, ...]
    per_seed_scores: Mapping[int, tuple[TypeScore, ...]]
    statistic: NoiseFloorStatistic
    escalation_count: int
    created_at_ms: int

    def floor_for(self, cell: Cell) -> NoiseFloor | None:
        return floors_by_cell(self.floors).get(cell)

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "eval_set_hash": self.eval_set_hash,
            "engine": {
                "backend": self.engine.backend,
                "orchestrator_model": self.engine.orchestrator_model,
                "substep_model": self.engine.substep_model,
                "scaffold_git_sha": self.engine.scaffold_git_sha,
            },
            "seeds": list(self.seeds),
            "statistic": self.statistic.value,
            "escalation_count": self.escalation_count,
            "created_at_ms": self.created_at_ms,
            "floors": {cell_key(floor.cell): encode_noise_floor(floor) for floor in self.floors},
            "per_seed": {
                str(seed): [encode_type_score(ts) for ts in cells]
                for seed, cells in self.per_seed_scores.items()
            },
        }


class NoiseFloorRunner:
    """Drives >=3 identical seed runs and reduces them to per-cell floors."""

    def __init__(self, runner: RoundRunner, trajectory: TrajectoryStore) -> None:
        self._runner = runner
        self._trajectory = trajectory
        self._skipped_seeds: tuple[int, ...] = ()

    @property
    def skipped_seeds(self) -> tuple[int, ...]:
        """Seeds the last :meth:`run` reused whole instead of re-driving.

        Reset at the head of every call. A seed is only listed here when
        *every* problem in the corpus had a complete attempt under its
        directory — see
        :meth:`~turing.research.loop.trajectory.TrajectoryStore.list_completed_seeds`
        for why a seed is all-or-nothing.
        """
        return self._skipped_seeds

    async def _reduce_completed_seed(
        self,
        seed: int,
        corpus: Sequence[Problem],
        identity: RunIdentity,
        config: NoiseFloorConfig,
    ) -> tuple[tuple[TypeScore, ...], int]:
        """Re-derive one already-measured seed's cells from its own artifacts.

        No compute: each problem's score is read out of the checkpoint the
        earlier process wrote, scored through the same
        :meth:`ScoredProblem.from_result` the live path uses, and reduced by
        the same :func:`build_type_scores`. A seed reconstructed here is
        therefore byte-identical in the report to one driven now, which is the
        property that lets the floor be reduced from a mix of the two at all.
        """
        output_dir = self._trajectory.noise_floor_seed_dir(seed)
        scored: list[ScoredProblem] = []
        escalations = 0
        for problem in corpus:
            stored = await self._trajectory.load_stored_attempt(
                problem.id, output_dir=output_dir, identity=identity
            )
            if not stored.is_complete:  # pragma: no cover — list_completed_seeds checked
                raise ContractViolationError(
                    f"noise-floor seed {seed} was listed complete but {problem.id!r} is not: "
                    f"{stored.reason}"
                )
            scored.append(
                ScoredProblem.from_result(
                    problem, stored.best_result, score_floors=config.score_floors
                )
            )
            escalations += len(stored.escalation_ids)
        return build_type_scores(scored), escalations

    def _refuse_seed(
        self,
        *,
        seed: int,
        run_id: str,
        corpus: Sequence[Problem],
        failures: Sequence[AttemptFailure],
    ) -> ContractViolationError:
        """Log the incomplete seed, then hand back the refusal to raise.

        Returns the exception instead of raising it so the caller keeps the
        ``raise ... from exc`` chain on the total-loss path; the logging is
        here so both paths emit the same event exactly once.

        Logged as well as raised because the two readers differ: the exception
        text is for whoever is at the terminal, the structured event is for the
        overnight log an operator greps in the morning. ``lost`` is a plain
        list of problem ids so ``research.noise_floor.seed_incomplete`` answers
        "which seed, which problems" without opening the traceback.
        """
        logger.error(
            "research.noise_floor.seed_incomplete",
            seed=seed,
            run_id=run_id,
            lost=[failure.problem_id for failure in failures],
            errors=[failure.error for failure in failures],
            measured=len(corpus) - len(failures),
            corpus=len(corpus),
            detail=(
                "this seed measured a different problem set than its siblings, so the "
                "spread across seeds would be part variance and part corpus; no floor "
                "is written and the remaining seeds are not run"
            ),
        )
        return _incomplete_seed_refusal(
            seed=seed,
            run_id=run_id,
            corpus_size=len(corpus),
            failures=failures,
        )

    async def run(
        self,
        corpus: Sequence[Problem],
        config: NoiseFloorConfig,
        *,
        resume: bool = True,
    ) -> NoiseFloorReport:
        """Measure every seed on the whole corpus, or refuse to report a floor.

        **Resume, and what it does not soften.** With ``resume=True`` (the
        default) a seed whose every problem already has a complete attempt
        under :meth:`noise_floor_seed_dir` is not driven again: its cells are
        re-derived from those attempts' own checkpoints, which costs nothing
        and produces exactly the numbers a re-drive would have produced. A
        seed that is *partly* done is driven, and ``run_attempts`` skips the
        finished problems inside it. The floor is measured before round 0 and
        has first claim on the subscription; paying twice for a seed a closed
        window interrupted is the failure this exists to remove.

        The refusal below is untouched by any of that. A seed is skipped only
        when it is *complete* — every problem, terminally, with a finished
        record on disk — so an incomplete seed still reaches the same guard,
        still refuses, and still leaves ``noise-floor.json`` absent. What
        counts as complete is
        :meth:`~turing.research.loop.trajectory.TrajectoryStore.load_stored_attempt`'s
        table, and it agrees with ``verify``: a terminal attempt whose summary
        never landed is not complete here either.

        **Why a seed that lost an attempt is fatal here but not in a round.**
        :meth:`RoundRunner.run_attempts` contains a raising attempt: the round
        drops that problem, keeps the rest, and flags itself with
        ``all_attempts_completed=False``. A round is a *point* — a smaller n,
        honestly labelled, is still a reading. A noise floor is a *yardstick*:
        it is the divisor that turns "round 4 improved by 0.8 points" into
        signal or noise, and it is compared against rounds run on the full
        corpus. Reduce it from seeds that measured different problem sets and
        the spread stops being run-to-run variance and becomes partly a
        measure of which problems happened to run — and every saturation
        verdict downstream inherits that without any way to detect it.

        ``measure_noise_floor`` cannot catch this. It guards *cell presence*
        (every ``(type, split)`` present in every seed), which is blind to a
        seed whose cell simply averaged one fewer problem: two problems in one
        seed and three in another still produce a cell in both, and the mean
        moves. So the guard has to be here, where the losses are still
        attributable to a seed.

        **Deterministic vs transient failures do not get different
        treatment**, and the distinction is worth stating because it is the
        obvious place to soften this. An unreadable workspace template is a
        property of the *problem* and so is lost by every seed identically:
        the resulting floor is at least internally consistent, just measured
        over a narrower corpus than advertised. A transient loss (I/O, a
        killed process, a flaky verifier) hits one seed and not the others and
        is the case that silently corrupts the floor. Both refuse, for two
        reasons. First, this runner cannot tell them apart at the point of
        failure — it has an exception string and one seed's worth of evidence;
        "deterministic" is only knowable after the *other* seeds have run and
        lost the same problem, which is exactly the hours of subscription time
        the fail-fast below is spending to avoid. Second, even the benign case
        is not benign: a floor measured over ten problems is not the yardstick
        for a round measured over eleven, so a consistently narrower floor is
        still a floor whose problem set does not match what it will be used to
        judge. The honest fix for a deterministically broken problem is to
        take it out of the corpus — which changes ``eval_set_hash``, which the
        round comparison already checks — not to let the floor quietly measure
        a different set than the rounds do.

        **Refused per seed, immediately.** The remaining seeds are hours of
        opportunistic compute that can only produce a report already known to
        be unusable, and stopping at the first bad seed keeps the message
        pinned to one seed and one list of problem ids. Nothing is written:
        ``noise-floor.json`` stays absent, and absent is exactly what the
        downstream refusals (``compute_deltas``, ``assess_saturation``,
        ``noise_floor_available``) are already built to handle. A partial
        artifact would be read as a measurement.

        Raises:
            ContractViolationError: a seed lost one or more attempts, or —
                from ``measure_noise_floor`` and :class:`NoiseFloorConfig` —
                too few seeds or a cell missing from some seed.
        """
        await self._trajectory.ensure_layout()
        existing = await self._trajectory.load_trajectory()
        if existing.get("rounds"):
            logger.warning(
                "research.noise_floor.measured_late",
                rounds_already_logged=len(existing["rounds"]),
                detail=(
                    "the noise floor is supposed to precede round 0; measuring it after "
                    "rounds have run makes those rounds interpretable only in hindsight"
                ),
            )
        per_seed: dict[int, tuple[TypeScore, ...]] = {}
        escalations = 0
        eval_set_hash = bind_eval_set_hash(corpus, config.eval_set_hash)
        self._skipped_seeds = ()
        identities = {
            seed: RunIdentity(
                round_id=config.round_config_for(seed).run_id,
                seed=seed,
                eval_set_hash=eval_set_hash,
            )
            for seed in config.seeds
        }
        already_measured: tuple[int, ...] = ()
        if resume:
            already_measured = await self._trajectory.list_completed_seeds(
                identities, [problem.id for problem in corpus]
            )
        for seed in config.seeds:
            seed_config = replace(config.round_config_for(seed), eval_set_hash=eval_set_hash)
            output_dir = self._trajectory.noise_floor_seed_dir(seed)
            if seed in already_measured:
                logger.info(
                    "research.noise_floor.seed_reused",
                    seed=seed,
                    run_id=seed_config.run_id,
                    problems=len(corpus),
                    detail=(
                        "every problem in this seed already has a complete attempt on "
                        "disk; its cells are re-derived from them and no compute is spent"
                    ),
                )
                cells, seed_escalations = await self._reduce_completed_seed(
                    seed, corpus, identities[seed], config
                )
                per_seed[seed] = cells
                escalations += seed_escalations
                continue
            logger.info(
                "research.noise_floor.seed_started",
                seed=seed,
                run_id=seed_config.run_id,
                problems=len(corpus),
            )
            try:
                outcomes = await self._runner.run_attempts(
                    corpus, seed_config, output_dir=output_dir, resume=resume
                )
            except ContractViolationError as exc:
                # ``run_attempts`` refuses outright when *every* attempt was
                # lost, and it sets ``attempt_failures`` before doing so
                # precisely so this caller can still say which problems went.
                # Re-raised with the seed attached; any other contract
                # violation (an empty corpus, say) is not ours to relabel.
                if not self._runner.attempt_failures:
                    raise
                raise self._refuse_seed(
                    seed=seed,
                    run_id=seed_config.run_id,
                    corpus=corpus,
                    failures=self._runner.attempt_failures,
                ) from exc
            if self._runner.attempt_failures:
                raise self._refuse_seed(
                    seed=seed,
                    run_id=seed_config.run_id,
                    corpus=corpus,
                    failures=self._runner.attempt_failures,
                )
            escalations += sum(o.escalation_count for o in outcomes)
            scored = [
                ScoredProblem.from_result(
                    o.problem, o.best_result, score_floors=config.score_floors
                )
                for o in outcomes
            ]
            per_seed[seed] = build_type_scores(scored)

        self._skipped_seeds = already_measured
        floors = measure_noise_floor(per_seed, statistic=config.statistic)
        report = NoiseFloorReport(
            run_id=config.run_id,
            eval_set_hash=eval_set_hash,
            engine=config.engine,
            seeds=config.seeds,
            floors=floors,
            per_seed_scores=per_seed,
            statistic=config.statistic,
            escalation_count=escalations,
            created_at_ms=self._runner.clock.now_ms(),
        )
        await self._trajectory.write_noise_floor(report.to_json())
        for floor in floors:
            if floor.is_degenerate:
                logger.warning(
                    "research.noise_floor.degenerate",
                    cell=cell_key(floor.cell),
                    seeds=list(floor.seeds),
                    detail=(
                        "identical means across seeds resolves no noise; every later "
                        "saturation verdict for this cell will be refused"
                    ),
                )
        logger.info(
            "research.noise_floor.measured",
            run_id=config.run_id,
            seeds=list(config.seeds),
            floors={cell_key(f.cell): f.value for f in floors},
            statistic=config.statistic.value,
        )
        return report
