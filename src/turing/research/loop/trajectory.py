"""On-disk trajectory: ``<results-root>/loop-<slug>/`` and ``trajectory.json``.

The results root comes from ``ResearchLoopSettings.research_results_root``,
``~/research-results`` by default — outside any repo, so the desktop app watches
one root no matter which project the loop runs from.

The layout is the one ``driving-functions.md`` prescribes::

    ~/research-results/loop-<slug>/
      trajectory.json        one object per round
      noise-floor/           the pre-round-0 seed runs
      round-00/              baseline (the un-improved starting point)
      round-01/  round-02/   … each a standard run directory

with each ``round-NN/`` holding the full round record, one JSON log per attempt
(the step-by-step trajectories loop 2 will sample from), and the escalations
raised during the round.

**Two deviations from the document's example row, both deliberate:**

1. ``primary``, ``delta``, ``noise_floor`` and ``cost_per_point`` are *objects
   keyed by* ``"<type>/<split>"``, not scalars. The brief forbids averaging
   across problem types — a speedup ratio and a leaderboard percentile share no
   scale, and blending them hides a round that helped one family and hurt
   another. A scalar ``primary`` is exactly the field that would force the
   average, so it does not exist.
2. Rows carry ``eval_set_hash``, ``engine`` and ``comparable_to_parent``.
   Lineage is mandatory: rounds measured on different eval sets may not be
   compared at all, so the row says so in a machine-readable field rather than
   leaving a reader to notice.
3. Rows carry per-cell ``n``, ``scored_n`` and ``floored_n``, plus ``seed``,
   ``cap`` and ``verify_every_step``. A cell mean built entirely from floored
   contributions must not be byte-identical to one built from real
   measurements — ``correctness_pass_rate`` is not enough of a tell.

``dollars`` is absent from ``cost`` because metering was retired.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from turing.research.contracts import ContractViolationError
from turing.research.loop.protocols import SystemClock

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from turing.research.contracts import (
        Attempt,
        Cap,
        EscalationDecision,
        EscalationRequest,
        RoundRecord,
        TypeScore,
    )
    from turing.research.loop.metrics import (
        Cell,
        CostBasis,
        NoiseFloor,
        SaturationAssessment,
        ScoredProblem,
    )
    from turing.research.loop.protocols import Clock

logger = structlog.get_logger(__name__)

__all__ = [
    "TRAJECTORY_FILENAME",
    "TRAJECTORY_SCHEMA_VERSION",
    "AttemptLog",
    "StepLog",
    "TrajectoryStore",
    "cell_key",
    "encode_assessment",
    "encode_noise_floor",
    "encode_round_record",
    "encode_trajectory_row",
    "encode_type_score",
]

TRAJECTORY_SCHEMA_VERSION = 1
TRAJECTORY_FILENAME = "trajectory.json"
NOISE_FLOOR_DIRNAME = "noise-floor"
NOISE_FLOOR_FILENAME = "noise-floor.json"


def cell_key(cell: Cell) -> str:
    """``(SPEEDUP, PRACTICE)`` -> ``"speedup/practice"`` — never a pooled key."""
    return f"{cell[0].value}/{cell[1].value}"


# --------------------------------------------------------------------------- #
# Per-attempt logs (loop 2 samples these; loop 1 only writes them)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StepLog:
    """One solver step and the verification that followed it."""

    index: int
    at_ms: int
    tokens: int
    wall_clock_seconds: float
    note: str
    made_progress: bool
    escalate_requested: str | None
    score: float | None
    passed_correctness: bool | None
    verifier_error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "at_ms": self.at_ms,
            "tokens": self.tokens,
            "wall_clock_seconds": self.wall_clock_seconds,
            "note": self.note,
            "made_progress": self.made_progress,
            "escalate_requested": self.escalate_requested,
            "score": self.score,
            "passed_correctness": self.passed_correctness,
            "verifier_error": self.verifier_error,
        }


@dataclass(frozen=True, slots=True)
class AttemptLog:
    """Everything one attempt did, written to ``round-NN/attempts/<id>.json``.

    This is the "few sampled trajectories" half of what loop 2's self-edit step
    is allowed to look at. It is written for every attempt including held-out
    ones — everything is *scored and reported* — and the split filter that
    keeps held-out material out of a self-edit summary lives in
    :mod:`turing.research.loop.self_edit_seam`, not here.
    """

    problem_id: str
    attempt_id: str
    round_id: str
    seed: int
    split: str
    problem_type: str
    final_state: str
    steps: tuple[StepLog, ...]
    best_score: float | None
    best_passed_correctness: bool | None
    score_scale: str
    consumed_steps: int
    consumed_tokens: int
    consumed_wall_clock_seconds: float
    cap_extensions: int
    escalation_ids: tuple[str, ...]
    workspace_path: str

    def to_json(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "attempt_id": self.attempt_id,
            "round_id": self.round_id,
            "seed": self.seed,
            "split": self.split,
            "problem_type": self.problem_type,
            "final_state": self.final_state,
            "best_score": self.best_score,
            "best_passed_correctness": self.best_passed_correctness,
            "score_scale": self.score_scale,
            "consumed": {
                "steps": self.consumed_steps,
                "tokens": self.consumed_tokens,
                "wall_clock_seconds": self.consumed_wall_clock_seconds,
            },
            "cap_extensions": self.cap_extensions,
            "escalation_ids": list(self.escalation_ids),
            "workspace_path": self.workspace_path,
            "steps": [s.to_json() for s in self.steps],
        }


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def encode_type_score(ts: TypeScore) -> dict[str, Any]:
    return {
        "problem_type": ts.problem_type.value,
        "split": ts.split.value,
        "n": ts.n,
        "mean_score": ts.mean_score,
        "correctness_passes": ts.correctness_passes,
        "correctness_pass_rate": ts.correctness_pass_rate,
        "scores": dict(ts.scores),
    }


def encode_noise_floor(floor: NoiseFloor) -> dict[str, Any]:
    return {
        "problem_type": floor.problem_type.value,
        "split": floor.split.value,
        "seeds": list(floor.seeds),
        "per_seed_mean": {str(seed): value for seed, value in floor.per_seed_mean.items()},
        "stdev": floor.stdev,
        "range": floor.spread_range,
        "statistic": floor.statistic.value,
        "value": floor.value,
        "degenerate": floor.is_degenerate,
    }


def encode_assessment(a: SaturationAssessment) -> dict[str, Any]:
    return {
        "problem_type": a.problem_type.value,
        "split": a.split.value,
        "verdict": a.verdict.value,
        "reason": a.reason,
        "marginal_gain": a.marginal_gain,
        "noise_floor": a.noise_floor,
        "gain_in_noise_units": a.gain_in_noise_units,
    }


def encode_round_record(
    record: RoundRecord,
    *,
    scored: Sequence[ScoredProblem] = (),
    assessments: Sequence[SaturationAssessment] = (),
    noise_floors: Sequence[NoiseFloor] = (),
) -> dict[str, Any]:
    """Full round artifact — ``round-NN/round.json``."""
    return {
        "round_index": record.round_index,
        "run_id": record.run_id,
        "parent_round_id": record.parent_round_id,
        "eval_set_hash": record.eval_set_hash,
        "created_at_ms": record.created_at_ms,
        "engine": {
            "backend": record.engine.backend,
            "orchestrator_model": record.engine.orchestrator_model,
            "substep_model": record.engine.substep_model,
            "scaffold_git_sha": record.engine.scaffold_git_sha,
        },
        "type_scores": [encode_type_score(ts) for ts in record.type_scores],
        "deltas": [
            {
                "problem_type": d.problem_type.value,
                "split": d.split.value,
                "marginal_gain": d.marginal_gain,
                "noise_floor": d.noise_floor,
                "beats_noise_floor": d.beats_noise_floor,
                "gain_in_noise_units": d.gain_in_noise_units,
                "cost_per_unit_gain": d.cost_per_unit_gain,
            }
            for d in record.deltas
        ],
        "saturation": [encode_assessment(a) for a in assessments],
        "noise_floors": [encode_noise_floor(f) for f in noise_floors],
        "cost": {
            "wall_clock_seconds": record.cost.wall_clock_seconds,
            "tokens": record.cost.tokens,
            "attempts": record.cost.attempts,
        },
        "escalation_count": record.escalation_count,
        "gates": dict(record.gates),
        "verdict": record.verdict,
        "problems": [
            {
                "problem_id": s.problem_id,
                "problem_type": s.problem_type.value,
                "split": s.split.value,
                "score": s.score,
                "score_scale": s.score_scale,
                "passed_correctness": s.passed_correctness,
                "scored": s.scored,
            }
            for s in scored
        ],
    }


def _encode_cap(cap: Cap) -> dict[str, Any]:
    return {
        "max_steps": cap.max_steps,
        "max_tokens": cap.max_tokens,
        "max_wall_clock_seconds": cap.max_wall_clock_seconds,
    }


def _cell_score_counts(
    scored: Sequence[ScoredProblem], n: Mapping[str, int]
) -> tuple[dict[str, int], dict[str, int]]:
    scored_n = dict.fromkeys(n, 0)
    floored_n = dict.fromkeys(n, 0)
    for item in scored:
        key = cell_key((item.problem_type, item.split))
        if item.scored:
            scored_n[key] = scored_n.get(key, 0) + 1
        else:
            floored_n[key] = floored_n.get(key, 0) + 1
    return scored_n, floored_n


def encode_trajectory_row(
    record: RoundRecord,
    *,
    assessments: Sequence[SaturationAssessment] = (),
    noise_floors: Sequence[NoiseFloor] = (),
    cost_basis: CostBasis | None = None,
    comparable_to_parent: bool | None = None,
    scored: Sequence[ScoredProblem] | None = None,
    seed: int | None = None,
    cap: Cap | None = None,
    verify_every_step: bool | None = None,
) -> dict[str, Any]:
    """One row of ``trajectory.json`` — the four numbers plus lineage.

    Every per-cell field is an object keyed by ``"<type>/<split>"``. There is
    no scalar ``primary``: see the module docstring.
    """
    floors = {cell_key(f.cell): f.value for f in noise_floors}
    n = {cell_key((ts.problem_type, ts.split)): ts.n for ts in record.type_scores}
    row: dict[str, Any] = {
        "round": record.round_index,
        "run_id": record.run_id,
        "parent_round": record.parent_round_id,
        "eval_set_hash": record.eval_set_hash,
        "comparable_to_parent": comparable_to_parent,
        "engine": {
            "backend": record.engine.backend,
            "orchestrator_model": record.engine.orchestrator_model,
            "substep_model": record.engine.substep_model,
            "scaffold_git_sha": record.engine.scaffold_git_sha,
        },
        # driving function #1 — per cell, never pooled
        "primary": {
            cell_key((ts.problem_type, ts.split)): ts.mean_score for ts in record.type_scores
        },
        "n": n,
        "correctness_pass_rate": {
            cell_key((ts.problem_type, ts.split)): ts.correctness_pass_rate
            for ts in record.type_scores
        },
        # driving function #2 — in units of the seed-noise floor
        "delta": {cell_key((d.problem_type, d.split)): d.marginal_gain for d in record.deltas},
        "noise_floor": floors,
        "delta_in_noise_units": {
            cell_key((d.problem_type, d.split)): d.gain_in_noise_units for d in record.deltas
        },
        # driving function #3
        "cost": {
            "wall_clock_seconds": record.cost.wall_clock_seconds,
            "tokens": record.cost.tokens,
            "attempts": record.cost.attempts,
        },
        "cost_basis": None if cost_basis is None else cost_basis.value,
        "cost_per_point": {
            cell_key((d.problem_type, d.split)): d.cost_per_unit_gain for d in record.deltas
        },
        # driving function #4
        "human_interventions": record.escalation_count,
        "constraints": dict(record.gates),
        "saturation": [encode_assessment(a) for a in assessments],
        "verdict": record.verdict,
        "seed": seed,
        "cap": None if cap is None else _encode_cap(cap),
        "verify_every_step": verify_every_step,
    }
    if scored is not None:
        scored_n, floored_n = _cell_score_counts(scored, n)
        row["scored_n"] = scored_n
        row["floored_n"] = floored_n
    return row


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    # ``allow_nan=False`` because Python emits bare ``NaN``/``Infinity``, which
    # is not JSON: every other reader rejects the file outright. The desktop
    # flywheel pane would blank rather than show one bad number, and a NaN that
    # reached here would mean a driving function was computed wrong anyway —
    # better to fail at the write, where the traceback still names the round.
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


class TrajectoryStore:
    """Owns one ``loop-<slug>/`` directory and the trajectory inside it.

    Append-only by construction: :meth:`append_round` refuses to rewrite a
    round that is already in the file. A round that failed a gate is logged,
    not deleted — it is usually the most informative round in the sweep.
    """

    def __init__(
        self,
        results_root: Path,
        loop_slug: str,
        *,
        clock: Clock | None = None,
    ) -> None:
        if not loop_slug:
            raise ValueError("loop_slug must be non-empty")
        self._root = results_root
        self._slug = loop_slug
        self._clock = clock or SystemClock()

    @property
    def loop_slug(self) -> str:
        return self._slug

    @property
    def loop_dir(self) -> Path:
        return self._root / f"loop-{self._slug}"

    @property
    def trajectory_path(self) -> Path:
        return self.loop_dir / TRAJECTORY_FILENAME

    @property
    def noise_floor_dir(self) -> Path:
        return self.loop_dir / NOISE_FLOOR_DIRNAME

    @property
    def noise_floor_path(self) -> Path:
        return self.noise_floor_dir / NOISE_FLOOR_FILENAME

    def noise_floor_seed_dir(self, seed: int) -> Path:
        return self.noise_floor_dir / f"seed-{seed}"

    def round_dir(self, round_index: int) -> Path:
        return self.loop_dir / f"round-{round_index:02d}"

    def attempts_dir(self, round_index: int) -> Path:
        return self.round_dir(round_index) / "attempts"

    def escalations_dir(self, round_index: int) -> Path:
        return self.round_dir(round_index) / "escalations"

    async def ensure_layout(self) -> None:
        def _mk() -> None:
            self.loop_dir.mkdir(parents=True, exist_ok=True)
            self.noise_floor_dir.mkdir(parents=True, exist_ok=True)

        await asyncio.to_thread(_mk)

    # -- attempts ---------------------------------------------------------- #

    async def write_attempt_log(self, log: AttemptLog, *, output_dir: Path) -> Path:
        path = output_dir / "attempts" / f"{log.problem_id}.json"
        await asyncio.to_thread(_write_json, path, log.to_json())
        return path

    async def write_attempt_checkpoint(self, attempt: Attempt, *, output_dir: Path) -> Path:
        """Persist the latest immutable checkpoint of an attempt.

        Reloading this file *is* resume: an interruption — a closed
        subscription window, a killed process — costs the remainder of the
        attempt, not the attempt. Written after every step and, critically,
        before the loop suspends on an escalation.
        """
        path = output_dir / "checkpoints" / f"{attempt.problem_id}.json"
        payload = {
            "attempt_id": attempt.attempt_id,
            "problem_id": attempt.problem_id,
            "round_id": attempt.round_id,
            "seed": attempt.seed,
            "workspace_path": str(attempt.workspace_path),
            "state": attempt.state.value,
            "step_index": attempt.step_index,
            "checkpoint_seq": attempt.checkpoint_seq,
            "resume_token": attempt.resume_token,
            "started_at_ms": attempt.started_at_ms,
            "updated_at_ms": attempt.updated_at_ms,
            "escalation_id": attempt.escalation_id,
            "cap": {
                "max_steps": attempt.cap.max_steps,
                "max_tokens": attempt.cap.max_tokens,
                "max_wall_clock_seconds": attempt.cap.max_wall_clock_seconds,
                "extension_count": attempt.cap.extension_count,
            },
            "consumed": {
                "steps": attempt.consumed.steps,
                "tokens": attempt.consumed.tokens,
                "wall_clock_seconds": attempt.consumed.wall_clock_seconds,
            },
            "result": None
            if attempt.result is None
            else {
                "score": attempt.result.score,
                "score_scale": attempt.result.score_scale,
                "passed_correctness": attempt.result.passed_correctness,
                "detail": attempt.result.detail,
            },
        }
        await asyncio.to_thread(_write_json, path, payload)
        return path

    async def write_escalation(
        self,
        request: EscalationRequest,
        decision: EscalationDecision | None,
        *,
        output_dir: Path,
    ) -> Path:
        from turing.research.loop.escalation import encode_decision, encode_request

        path = output_dir / "escalations" / f"{request.request_id}.json"
        payload = {
            "request": encode_request(request),
            "decision": None if decision is None else encode_decision(decision),
        }
        await asyncio.to_thread(_write_json, path, payload)
        return path

    # -- rounds ------------------------------------------------------------ #

    async def write_round_record(
        self,
        record: RoundRecord,
        *,
        scored: Sequence[ScoredProblem] = (),
        assessments: Sequence[SaturationAssessment] = (),
        noise_floors: Sequence[NoiseFloor] = (),
    ) -> Path:
        path = self.round_dir(record.round_index) / "round.json"
        payload = encode_round_record(
            record,
            scored=scored,
            assessments=assessments,
            noise_floors=noise_floors,
        )
        await asyncio.to_thread(_write_json, path, payload)
        return path

    async def load_trajectory(self) -> dict[str, Any]:
        raw = await asyncio.to_thread(_read_json, self.trajectory_path)
        if raw is None:
            return {
                "schema_version": TRAJECTORY_SCHEMA_VERSION,
                "loop_slug": self._slug,
                "rounds": [],
            }
        if not isinstance(raw, dict):
            raise ContractViolationError(f"{self.trajectory_path} is not a trajectory document")
        raw.setdefault("rounds", [])
        return raw

    async def append_round(
        self,
        record: RoundRecord,
        *,
        assessments: Sequence[SaturationAssessment] = (),
        noise_floors: Sequence[NoiseFloor] = (),
        cost_basis: CostBasis | None = None,
        scored: Sequence[ScoredProblem] | None = None,
        seed: int | None = None,
        cap: Cap | None = None,
        verify_every_step: bool | None = None,
    ) -> dict[str, Any]:
        """Append one round object to ``trajectory.json``, flagging lineage.

        Returns the row as written. The lineage flags are computed here rather
        than trusted from the caller:

        * ``comparable_to_parent`` is ``False`` when the previous row was
          measured on a different ``eval_set_hash``. Never compare rounds
          measured on different eval sets — if the eval set changes the
          trajectory restarts, and the row says so loudly instead of leaving a
          reader to spot it.
        * ``engine_changed`` marks a shifted engine identity, which is a
          reportable confound rather than an error.
        """
        document = await self.load_trajectory()
        rows: list[dict[str, Any]] = list(document.get("rounds", []))
        if any(row.get("round") == record.round_index for row in rows):
            raise ContractViolationError(
                f"round {record.round_index} is already in {self.trajectory_path}; the "
                "trajectory is append-only — a re-run gets a new round index"
            )
        previous = rows[-1] if rows else None
        comparable: bool | None = None
        engine_changed = False
        if previous is not None:
            comparable = previous.get("eval_set_hash") == record.eval_set_hash
            engine_changed = previous.get("engine") != {
                "backend": record.engine.backend,
                "orchestrator_model": record.engine.orchestrator_model,
                "substep_model": record.engine.substep_model,
                "scaffold_git_sha": record.engine.scaffold_git_sha,
            }
            if not comparable:
                logger.error(
                    "research.trajectory.eval_set_changed",
                    round_index=record.round_index,
                    previous_eval_set_hash=previous.get("eval_set_hash"),
                    eval_set_hash=record.eval_set_hash,
                    detail=(
                        "rounds measured on different eval sets are not comparable; "
                        "the trajectory restarts here"
                    ),
                )
        row = encode_trajectory_row(
            record,
            assessments=assessments,
            noise_floors=noise_floors,
            cost_basis=cost_basis,
            comparable_to_parent=comparable,
            scored=scored,
            seed=seed,
            cap=cap,
            verify_every_step=verify_every_step,
        )
        row["engine_changed"] = engine_changed
        row["trajectory_restart"] = comparable is False
        row["noise_floor_measured"] = bool(noise_floors)
        rows.append(row)
        document["schema_version"] = TRAJECTORY_SCHEMA_VERSION
        document["loop_slug"] = self._slug
        document["updated_at_ms"] = self._clock.now_ms()
        document["rounds"] = rows
        await asyncio.to_thread(_write_json, self.trajectory_path, document)
        logger.info(
            "research.trajectory.round_appended",
            round_index=record.round_index,
            run_id=record.run_id,
            parent_round_id=record.parent_round_id,
            eval_set_hash=record.eval_set_hash,
            comparable_to_parent=comparable,
            escalations=record.escalation_count,
        )
        return row

    # -- noise floor ------------------------------------------------------- #

    async def write_noise_floor(self, payload: Mapping[str, Any]) -> Path:
        await asyncio.to_thread(_write_json, self.noise_floor_path, dict(payload))
        return self.noise_floor_path

    async def load_noise_floor(self) -> dict[str, Any] | None:
        raw = await asyncio.to_thread(_read_json, self.noise_floor_path)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ContractViolationError(f"{self.noise_floor_path} is not a noise-floor document")
        return raw

    async def has_noise_floor(self) -> bool:
        return await asyncio.to_thread(self.noise_floor_path.exists)
