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
from turing.research.loop.trajectory import cell_key, encode_noise_floor, encode_type_score
from turing.research.problems.adapter import bind_eval_set_hash

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from turing.research.contracts import Cap, EngineIdentity, Problem, TypeScore
    from turing.research.loop.metrics import Cell, NoiseFloor
    from turing.research.loop.runner import PassCriterion, RoundRunner
    from turing.research.loop.trajectory import TrajectoryStore

logger = structlog.get_logger(__name__)

__all__ = ["NoiseFloorConfig", "NoiseFloorReport", "NoiseFloorRunner"]


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

    async def run(
        self,
        corpus: Sequence[Problem],
        config: NoiseFloorConfig,
    ) -> NoiseFloorReport:
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
        for seed in config.seeds:
            seed_config = replace(config.round_config_for(seed), eval_set_hash=eval_set_hash)
            output_dir = self._trajectory.noise_floor_seed_dir(seed)
            logger.info(
                "research.noise_floor.seed_started",
                seed=seed,
                run_id=seed_config.run_id,
                problems=len(corpus),
            )
            outcomes = await self._runner.run_attempts(corpus, seed_config, output_dir=output_dir)
            escalations += sum(o.escalation_count for o in outcomes)
            scored = [
                ScoredProblem.from_result(
                    o.problem, o.best_result, score_floors=config.score_floors
                )
                for o in outcomes
            ]
            per_seed[seed] = build_type_scores(scored)

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
