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
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from turing.research.contracts import (
    Attempt,
    AttemptState,
    Cap,
    CapConsumption,
    ContractViolationError,
    EngineIdentity,
    EscalationReason,
    EscalationRequest,
    ProblemType,
    RoundCost,
    RoundDelta,
    RoundRecord,
    Split,
    TypeScore,
    VerificationResult,
)
from turing.research.loop.metrics import (
    SaturationAssessment,
    SaturationVerdict,
    ScoredProblem,
)
from turing.research.loop.protocols import SystemClock

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from turing.research.contracts import EscalationDecision
    from turing.research.loop.metrics import Cell, CostBasis, NoiseFloor
    from turing.research.loop.protocols import Clock

logger = structlog.get_logger(__name__)

__all__ = [
    "TRAJECTORY_FILENAME",
    "TRAJECTORY_SCHEMA_VERSION",
    "AttemptDisposition",
    "AttemptLog",
    "RunIdentity",
    "StepLog",
    "StoredAttempt",
    "TrajectoryStore",
    "cell_key",
    "decode_assessments",
    "decode_attempt_checkpoint",
    "decode_escalation_request",
    "decode_round_record",
    "decode_scored_problems",
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
# Resume: what is already on disk, and what counts as done
# --------------------------------------------------------------------------- #


class AttemptDisposition(str, Enum):  # noqa: UP042
    """What a restart may do with an attempt it finds already on disk.

    The four values are the whole resume decision. See
    :meth:`TrajectoryStore.load_stored_attempt` for the rules that pick one.
    """

    #: Nothing usable for this run — drive the attempt from scratch.
    ABSENT = "absent"
    #: Finished; reuse its outcome and spend no compute on it.
    COMPLETE = "complete"
    #: Interrupted mid-flight; carry on from the checkpoint.
    RESUMABLE = "resumable"
    #: Suspended on an operator decision that has not arrived; re-enter the
    #: wait rather than re-driving the attempt.
    AWAITING_DECISION = "awaiting_decision"


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Which run a stored attempt would have to belong to, to be reusable.

    Resume reuses *this* run's own work. A checkpoint left by a different
    ``run_id``, a different ``seed``, or a corpus with a different
    ``eval_set_hash`` is a **previous generation**, not a resume point:
    adopting its numbers would report a measurement this run never took. That
    distinction is what keeps the sanctioned re-drive workflows — a new round
    id, a re-measured baseline — from silently inheriting the old numbers, and
    it is the same identity comparison
    ``runner._read_crashed_attempt_record`` makes for the metrics chain.

    ``config_digest`` is the fourth coordinate, and it exists because the
    first three name *which* run without naming *how it measured*. A round
    killed under a cap of 2 steps and restarted with a cap of 6 has the same
    ``run_id``, ``seed`` and corpus; without this field it would reuse the
    cap-2 scores for the finished problems and drive the rest at cap 6, and
    the resulting :class:`~turing.research.contracts.RoundRecord` would be
    one number measured two ways. The same holds for the engine — a floor
    measured under scaffold ``A`` re-derived and stamped with ``B`` would pass
    ``run_round``'s engine guard on a floor that never saw ``B``. The digest
    is ``RoundConfig.config_digest()``: a SHA-256 over the canonical JSON of
    every measurement-affecting field, so any drift in one of them makes the
    stored attempt a previous generation and re-drives it — the safe
    direction. Empty means "compare nothing", which only a caller that has no
    ``RoundConfig`` (a hand-built identity in a test) should rely on.
    """

    round_id: str
    seed: int
    eval_set_hash: str = ""
    config_digest: str = ""


@dataclass(frozen=True, slots=True)
class StoredAttempt:
    """One attempt as it exists on disk, with the resume question answered."""

    problem_id: str
    disposition: AttemptDisposition
    #: The rebuilt checkpoint. ``None`` only for :attr:`AttemptDisposition.ABSENT`.
    attempt: Attempt | None = None
    steps: tuple[StepLog, ...] = ()
    escalation_ids: tuple[str, ...] = ()
    attempt_log_path: Path | None = None
    #: Why this disposition, in one line, for the log an operator greps at 3am.
    reason: str = ""

    @property
    def is_complete(self) -> bool:
        return self.disposition is AttemptDisposition.COMPLETE

    @property
    def is_resumable(self) -> bool:
        return self.disposition in (
            AttemptDisposition.RESUMABLE,
            AttemptDisposition.AWAITING_DECISION,
        )

    @property
    def best_result(self) -> VerificationResult | None:
        """The verification this attempt reported, as the checkpoint recorded it.

        The runner writes ``result=best`` onto every checkpoint it takes, so
        the terminal checkpoint carries exactly the result the attempt would
        have returned. ``None`` is honest — the attempt produced no usable
        verification — and :meth:`ScoredProblem.from_result` floors it.
        """
        return None if self.attempt is None else self.attempt.result


def _decode_result(payload: Any) -> VerificationResult | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ContractViolationError("an attempt checkpoint's result must be a JSON object")
    return VerificationResult(
        problem_id=payload["problem_id"],
        verifier_id=payload["verifier_id"],
        score=payload["score"],
        passed_correctness=payload["passed_correctness"],
        score_scale=payload["score_scale"],
        raw_measurements=payload.get("raw_measurements", {}),
        detail=payload.get("detail", ""),
    )


def decode_attempt_checkpoint(payload: Mapping[str, Any]) -> Attempt:
    """Rebuild the :class:`~turing.research.contracts.Attempt` a checkpoint holds.

    The inverse of :meth:`TrajectoryStore.write_attempt_checkpoint`, and the
    whole of resume from a caller's point of view: no replay, no
    reconstruction of intermediate state. Construction goes through
    :class:`~turing.research.contracts.Attempt`, so its invariants (notably
    "``ABANDONED`` requires an ``escalation_id``") are re-checked on the way
    back in rather than trusted from the file.

    Deliberately **not** shared with
    :func:`turing.research.solver.checkpoints.attempt_from_json`, which
    decodes the same shape: :mod:`turing.research.loop` has no import
    dependency on :mod:`turing.research.solver` and gains nothing by growing
    one for twenty lines.
    """
    return Attempt(
        attempt_id=payload["attempt_id"],
        problem_id=payload["problem_id"],
        round_id=payload["round_id"],
        seed=payload["seed"],
        workspace_path=Path(payload["workspace_path"]),
        cap=Cap(
            max_steps=payload["cap"]["max_steps"],
            max_tokens=payload["cap"]["max_tokens"],
            max_wall_clock_seconds=payload["cap"]["max_wall_clock_seconds"],
            extension_count=payload["cap"].get("extension_count", 0),
        ),
        consumed=CapConsumption(
            steps=payload["consumed"]["steps"],
            tokens=payload["consumed"]["tokens"],
            wall_clock_seconds=payload["consumed"]["wall_clock_seconds"],
        ),
        state=AttemptState(payload["state"]),
        step_index=payload["step_index"],
        checkpoint_seq=payload["checkpoint_seq"],
        resume_token=payload.get("resume_token"),
        started_at_ms=payload.get("started_at_ms"),
        updated_at_ms=payload.get("updated_at_ms"),
        result=_decode_result(payload.get("result")),
        escalation_id=payload.get("escalation_id"),
    )


def _decode_step_log(payload: Mapping[str, Any]) -> StepLog:
    return StepLog(
        index=payload["index"],
        at_ms=payload["at_ms"],
        tokens=payload["tokens"],
        wall_clock_seconds=payload["wall_clock_seconds"],
        note=payload.get("note", ""),
        made_progress=payload.get("made_progress", False),
        escalate_requested=payload.get("escalate_requested"),
        score=payload.get("score"),
        passed_correctness=payload.get("passed_correctness"),
        verifier_error=payload.get("verifier_error"),
    )


def decode_escalation_request(payload: Any, *, attempt_id: str) -> EscalationRequest | None:
    """One ``escalations/<id>.json`` file back into the request it recorded.

    ``None`` for anything that is not a readable request **for this attempt**:
    a payload that is not the ``{"request": ..., "decision": ...}`` shape this
    store writes, a request naming another attempt, or one missing a field the
    rebuild needs. Shared by ``runner._read_prior_escalations`` (which counts
    the requests an attempt already raised) and by :meth:`_read_stored_attempt`
    (which decides whether an ``ESCALATED`` checkpoint can be re-entered), so
    the two cannot disagree about whether a request file is usable — the case
    that used to lose a problem outright: the store said *awaiting decision*
    because the file existed, the runner then failed to decode it and raised.

    ``best_result`` is dropped rather than decoded: the operator-facing
    encoding keeps four fields of a
    :class:`~turing.research.contracts.VerificationResult`, and a half-built
    result would be a fabricated measurement.
    """
    request = payload.get("request") if isinstance(payload, dict) else None
    if not isinstance(request, dict) or request.get("attempt_id") != attempt_id:
        return None
    try:
        return EscalationRequest(
            request_id=request["request_id"],
            problem_id=request["problem_id"],
            attempt_id=request["attempt_id"],
            round_id=request["round_id"],
            reason=EscalationReason(request["reason"]),
            summary=request.get("summary", ""),
            cap=Cap(
                max_steps=request["cap"]["max_steps"],
                max_tokens=request["cap"]["max_tokens"],
                max_wall_clock_seconds=request["cap"]["max_wall_clock_seconds"],
                extension_count=request["cap"].get("extension_count", 0),
            ),
            consumed=CapConsumption(
                steps=request["consumed"]["steps"],
                tokens=request["consumed"]["tokens"],
                wall_clock_seconds=request["consumed"]["wall_clock_seconds"],
            ),
            created_at_ms=int(request["created_at_ms"]),
            best_result=None,
        )
    except (KeyError, TypeError, ValueError, ContractViolationError):
        return None


def _decode_type_score(payload: Mapping[str, Any]) -> TypeScore:
    return TypeScore(
        problem_type=ProblemType(payload["problem_type"]),
        split=Split(payload["split"]),
        scores={str(k): float(v) for k, v in payload["scores"].items()},
        correctness_passes=int(payload["correctness_passes"]),
    )


def decode_round_record(payload: Mapping[str, Any]) -> RoundRecord:
    """``round-NN/round.json`` back into the :class:`RoundRecord` it encodes.

    The inverse of :func:`encode_round_record` for the record itself; the
    per-problem ``scored`` list and the saturation assessments beside it have
    their own decoders. Construction goes through the contract dataclasses,
    so their invariants are re-checked rather than trusted from the file.

    Written for one caller: a ``run_round`` that finds its round already in
    ``trajectory.json`` and must return what the first process measured
    without measuring, or writing, anything (RES-17). It is deliberately
    *not* the "decode the parent round" piece the driver's ``--round N``
    refusal names — that refusal has a second, sufficient reason (loop 2's
    self-edit step), and nothing here loads a *parent*.
    """
    engine = payload["engine"]
    cost = payload["cost"]
    return RoundRecord(
        round_index=int(payload["round_index"]),
        run_id=payload["run_id"],
        parent_round_id=payload.get("parent_round_id"),
        eval_set_hash=payload["eval_set_hash"],
        engine=EngineIdentity(
            backend=engine["backend"],
            orchestrator_model=engine["orchestrator_model"],
            substep_model=engine["substep_model"],
            scaffold_git_sha=engine["scaffold_git_sha"],
        ),
        type_scores=tuple(_decode_type_score(ts) for ts in payload.get("type_scores", [])),
        deltas=tuple(
            RoundDelta(
                problem_type=ProblemType(d["problem_type"]),
                split=Split(d["split"]),
                marginal_gain=float(d["marginal_gain"]),
                noise_floor=float(d["noise_floor"]),
                cost_per_unit_gain=(
                    None if d.get("cost_per_unit_gain") is None else float(d["cost_per_unit_gain"])
                ),
            )
            for d in payload.get("deltas", [])
        ),
        cost=RoundCost(
            wall_clock_seconds=float(cost["wall_clock_seconds"]),
            tokens=int(cost["tokens"]),
            attempts=int(cost["attempts"]),
        ),
        escalation_count=int(payload["escalation_count"]),
        created_at_ms=int(payload["created_at_ms"]),
        gates={str(k): v for k, v in payload.get("gates", {}).items()},
        verdict=str(payload.get("verdict", "")),
    )


def decode_scored_problems(payload: Mapping[str, Any]) -> tuple[ScoredProblem, ...]:
    """The ``problems`` list of ``round.json`` back into cell contributions."""
    return tuple(
        ScoredProblem(
            problem_id=item["problem_id"],
            problem_type=ProblemType(item["problem_type"]),
            split=Split(item["split"]),
            score=float(item["score"]),
            passed_correctness=bool(item["passed_correctness"]),
            score_scale=item["score_scale"],
            scored=bool(item.get("scored", True)),
        )
        for item in payload.get("problems", [])
    )


def decode_assessments(payload: Mapping[str, Any]) -> tuple[SaturationAssessment, ...]:
    """The ``saturation`` list of ``round.json`` back into assessments."""
    return tuple(
        SaturationAssessment(
            problem_type=ProblemType(item["problem_type"]),
            split=Split(item["split"]),
            verdict=SaturationVerdict(item["verdict"]),
            reason=str(item.get("reason", "")),
            marginal_gain=item.get("marginal_gain"),
            noise_floor=item.get("noise_floor"),
            gain_in_noise_units=item.get("gain_in_noise_units"),
        )
        for item in payload.get("saturation", [])
    )


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
    # The scratch name carries the pid and a uuid rather than a fixed
    # ``<name>.tmp``. Two writers of the same file is not hypothetical here: an
    # operator who starts a second driver over one results tree (or a
    # ``--noise-floor-only`` run alongside a full one) has two processes
    # writing ``checkpoints/<problem>.json``, and with one shared scratch name
    # they interleave — writer A's partial text, writer B's ``replace``, and a
    # file that is neither. Unique names make the worst case "the last
    # ``replace`` wins", which is atomic and yields one whole document.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        # ``allow_nan=False`` because Python emits bare ``NaN``/``Infinity``,
        # which is not JSON: every other reader rejects the file outright. The
        # desktop flywheel pane would blank rather than show one bad number,
        # and a NaN that reached here would mean a driving function was
        # computed wrong anyway — better to fail at the write, where the
        # traceback still names the round.
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    finally:
        # A refused serialisation (the NaN guard above) used to leave its
        # scratch file behind. With a unique name per call that would
        # accumulate one corpse per failure instead of one in total, so the
        # cleanup is no longer optional.
        tmp.unlink(missing_ok=True)


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

    def attempt_checkpoint_path(self, problem_id: str, *, output_dir: Path) -> Path:
        return output_dir / "checkpoints" / f"{problem_id}.json"

    async def write_attempt_checkpoint(
        self,
        attempt: Attempt,
        *,
        output_dir: Path,
        eval_set_hash: str = "",
        config_digest: str = "",
    ) -> Path:
        """Persist the latest immutable checkpoint of an attempt.

        Reloading this file *is* resume: an interruption — a closed
        subscription window, a killed process — costs the remainder of the
        attempt, not the attempt. Written after every step and, critically,
        before the loop suspends on an escalation.

        Two fields exist for the *reader* rather than the writer, and both were
        added when resume stopped being a story and became a code path
        (RES-17):

        * The full ``result`` — ``problem_id``, ``verifier_id`` and
          ``raw_measurements`` alongside the score. The operator-facing
          rendering in ``escalation.encode_request`` keeps only the four
          human-readable fields, which is right there and wrong here: a
          resumed round reports a skipped attempt's score straight out of this
          file, so the file has to round-trip a whole
          :class:`~turing.research.contracts.VerificationResult` rather than a
          summary of one.
        * ``eval_set_hash``, so a checkpoint cannot be reused by a run
          measuring a *different corpus*. ``run_id`` and ``seed`` alone do not
          catch it: the noise floor's per-seed run id is derived
          deterministically as ``<run_id>-seed-<n>``, so a corpus edited
          between two invocations of the same floor repeats both. See
          :class:`RunIdentity`.
        * ``config_digest``, so a checkpoint cannot be reused by a run
          measuring under a *different configuration* — a raised cap, a
          swapped scaffold, a flipped ``verify_every_step``. See
          :attr:`RunIdentity.config_digest`.
        """
        path = self.attempt_checkpoint_path(attempt.problem_id, output_dir=output_dir)
        payload = {
            "attempt_id": attempt.attempt_id,
            "problem_id": attempt.problem_id,
            "round_id": attempt.round_id,
            "seed": attempt.seed,
            "eval_set_hash": eval_set_hash,
            "config_digest": config_digest,
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
                "problem_id": attempt.result.problem_id,
                "verifier_id": attempt.result.verifier_id,
                "score": attempt.result.score,
                "score_scale": attempt.result.score_scale,
                "passed_correctness": attempt.result.passed_correctness,
                "raw_measurements": dict(attempt.result.raw_measurements),
                "detail": attempt.result.detail,
            },
        }
        await asyncio.to_thread(_write_json, path, payload)
        return path

    # -- resume ------------------------------------------------------------ #

    async def load_stored_attempt(
        self,
        problem_id: str,
        *,
        output_dir: Path,
        identity: RunIdentity,
    ) -> StoredAttempt:
        """What is already on disk for one ``(run, problem)``, and what to do with it.

        **The contract**, which every rule below serves and every test of
        resume is a test of: *resume never adopts a number this run did not
        measure under this exact configuration, never destroys a measurement,
        and re-running a finished sweep with the identical command is a no-op
        success.*

        **Complete — reuse the outcome, spend nothing.** All of:

        * the checkpoint decodes and names this ``identity``
          (``round_id`` / ``seed`` / ``eval_set_hash`` / ``config_digest``);
        * its state is terminal — ``PASSED``, ``FAILED_WITHIN_CAP`` or
          ``ABANDONED`` (:data:`~turing.research.contracts.TERMINAL_ATTEMPT_STATES`);
        * the attempt log ``attempts/<problem-id>.json`` is beside it **and
          names the checkpoint's** ``attempt_id``;
        * the attempt summary ``attempts/<problem-id>/metrics.json`` is beside
          it **and names the checkpoint's** ``attempt_id``.

        The last two are not belt-and-braces. ``metrics.json`` is written once,
        at the end of ``run_attempt``, and its absence beside an intact chain
        is exactly the shape ``python -m turing.research.loop.verify`` reports
        ``INCOMPLETE`` — a process killed between its last append and its
        summary write. Skipping such an attempt would have this resume path
        call *finished* what the pre-writeup gate calls *unfinished*, and the
        two must not disagree about the same directory. So a terminal
        checkpoint with no finished record on disk is re-driven: the honest
        reading of "we cannot tell what it reported". The ``attempt_id``
        comparison closes the remaining gap: a log and summary left by a
        *previous generation* of the same problem (same run, same seed, a
        different attempt id) are a finished record of some attempt, not of
        this one, and must not bless a checkpoint they never described.

        **The three states that are deliberately not complete:**

        * ``PAUSED`` / ``PENDING`` / ``RUNNING`` / ``VERIFYING`` →
          :attr:`AttemptDisposition.RESUMABLE`. This is the load-bearing case:
          subscription windows close mid-attempt, and an interruption must
          cost the remainder of the attempt, not the attempt.
        * ``ESCALATED`` with a request file that decodes and names this
          attempt → :attr:`AttemptDisposition.AWAITING_DECISION`.
          ``ESCALATED`` is neither terminal nor resumable — only an operator
          moves it — so the restart re-enters the wait on that same request.
          Re-driving would both throw away the work and raise a second
          escalation for one event, inflating driving function #4. The file
          is *decoded* here, with the same decoder the runner rebuilds it
          with (:func:`decode_escalation_request`), not merely stat-ed: a
          request that exists but does not decode, or names another attempt,
          is ``ABSENT`` — re-driven — rather than an ``AWAITING_DECISION``
          the runner then cannot honour and loses the problem to.
        * anything unreadable, foreign, or contradictory →
          :attr:`AttemptDisposition.ABSENT`, i.e. drive it. Every refusal
          direction here is towards *spending compute*, never towards
          adopting a number this run did not measure. That includes a
          checkpoint whose bytes are not UTF-8 or not JSON: every read
          failure is a ``ValueError`` or an ``OSError``, and both mean
          *absent*, never *the round dies here*.

        ``ABANDONED`` counts as complete, and that is a deliberate reading of
        the state rather than an oversight: it is reachable only through an
        operator ``ABANDON`` verdict, so re-driving it would re-ask a question
        the operator has already answered.
        """
        return await asyncio.to_thread(self._read_stored_attempt, problem_id, output_dir, identity)

    async def load_completed_attempt(
        self,
        round_index: int,
        problem_id: str,
        *,
        identity: RunIdentity,
    ) -> StoredAttempt | None:
        """The finished attempt for ``(round, problem)``, or ``None``.

        The narrow, round-shaped form of :meth:`load_stored_attempt`: it
        answers only "is there something here this round may reuse instead of
        re-driving?", and ``None`` covers every reason the answer is no —
        nothing on disk, a previous generation's work, an interrupted attempt,
        an open escalation, a terminal checkpoint whose record never finished.
        The completeness rules, in full, are in
        :meth:`load_stored_attempt`.
        """
        stored = await self.load_stored_attempt(
            problem_id, output_dir=self.round_dir(round_index), identity=identity
        )
        return stored if stored.is_complete else None

    async def list_completed_seeds(
        self,
        identities: Mapping[int, RunIdentity],
        problem_ids: Sequence[str],
    ) -> tuple[int, ...]:
        """Which noise-floor seeds already measured **every** problem, completely.

        A seed is all-or-nothing here, and that follows from what a floor is
        for rather than from convenience. ``NoiseFloorRunner.run`` refuses a
        seed that measured a strict subset of the corpus — the spread across
        seeds would be part run-to-run variance and part "which problems ran"
        — so "this seed is done" can only mean *every* problem in
        ``problem_ids`` has a complete attempt under
        :meth:`noise_floor_seed_dir`. A seed with some problems finished is
        not listed; it is driven, and the per-problem skip inside
        ``run_attempts`` is what keeps its finished attempts from being paid
        for twice.

        ``identities`` is keyed by seed because each seed run has its own
        ``run_id`` (``<run_id>-seed-<n>``); the eval-set hash is shared.
        """
        completed: list[int] = []
        for seed, identity in identities.items():
            output_dir = self.noise_floor_seed_dir(seed)
            stored = [
                await self.load_stored_attempt(problem_id, output_dir=output_dir, identity=identity)
                for problem_id in problem_ids
            ]
            if stored and all(item.is_complete for item in stored):
                completed.append(seed)
        return tuple(sorted(completed))

    def _read_stored_attempt(
        self,
        problem_id: str,
        output_dir: Path,
        identity: RunIdentity,
    ) -> StoredAttempt:
        """The blocking half of :meth:`load_stored_attempt`, run in a thread."""

        def absent(reason: str) -> StoredAttempt:
            return StoredAttempt(
                problem_id=problem_id,
                disposition=AttemptDisposition.ABSENT,
                reason=reason,
            )

        checkpoint_path = self.attempt_checkpoint_path(problem_id, output_dir=output_dir)
        # ``ValueError`` rather than ``json.JSONDecodeError``: the decode error
        # is one subclass, ``UnicodeDecodeError`` (a checkpoint whose bytes are
        # not UTF-8) is another, and a checkpoint that cannot be read is
        # *absent* whichever way it failed. This function is a resume probe;
        # nothing it hits may take the round down.
        try:
            raw = _read_json(checkpoint_path)
        except (OSError, ValueError) as exc:
            return absent(f"{checkpoint_path} could not be read: {exc}")
        if raw is None:
            return absent("no checkpoint on disk")
        if not isinstance(raw, dict):
            return absent(f"{checkpoint_path} is not an attempt checkpoint")
        try:
            attempt = decode_attempt_checkpoint(raw)
        except (KeyError, TypeError, ValueError, ContractViolationError) as exc:
            return absent(f"{checkpoint_path} could not be decoded: {type(exc).__name__}: {exc}")

        stored_identity = RunIdentity(
            round_id=attempt.round_id,
            seed=attempt.seed,
            eval_set_hash=str(raw.get("eval_set_hash", "")),
            config_digest=str(raw.get("config_digest", "")),
        )
        if attempt.problem_id != problem_id or stored_identity != identity:
            return absent(
                f"checkpoint belongs to a different run "
                f"(problem_id={attempt.problem_id!r}, round_id={stored_identity.round_id!r}, "
                f"seed={stored_identity.seed!r}, "
                f"eval_set_hash={stored_identity.eval_set_hash!r}, "
                f"config_digest={stored_identity.config_digest!r}; "
                f"this run is round_id={identity.round_id!r}, seed={identity.seed!r}, "
                f"eval_set_hash={identity.eval_set_hash!r}, "
                f"config_digest={identity.config_digest!r})"
            )

        log_path = output_dir / "attempts" / f"{problem_id}.json"
        steps: tuple[StepLog, ...] = ()
        escalation_ids: tuple[str, ...] = ()
        log_raw: Any = None
        try:
            log_raw = _read_json(log_path)
        except (OSError, ValueError):
            log_raw = None
        # A log that names another attempt is a previous generation's record
        # of this problem, not this attempt's; its steps must not be spliced
        # onto a resumed attempt, and it must not count towards completeness.
        if isinstance(log_raw, dict) and log_raw.get("attempt_id") != attempt.attempt_id:
            log_raw = None
        if isinstance(log_raw, dict):
            try:
                steps = tuple(
                    _decode_step_log(entry)
                    for entry in log_raw.get("steps", [])
                    if isinstance(entry, dict)
                )
            except (KeyError, TypeError, ValueError):
                steps = ()
            escalation_ids = tuple(
                str(value) for value in log_raw.get("escalation_ids", []) if isinstance(value, str)
            )

        def found(
            disposition: AttemptDisposition, *, reason: str, log: Path | None
        ) -> StoredAttempt:
            return StoredAttempt(
                problem_id=problem_id,
                disposition=disposition,
                attempt=attempt,
                steps=steps,
                escalation_ids=escalation_ids,
                attempt_log_path=log,
                reason=reason,
            )

        if attempt.is_terminal:
            if not isinstance(log_raw, dict):
                return absent(
                    f"terminal checkpoint ({attempt.state.value}) with no readable attempt "
                    f"log for attempt {attempt.attempt_id!r} at {log_path}; the attempt did "
                    "not finish reporting"
                )
            summary_path = output_dir / "attempts" / problem_id / "metrics.json"
            try:
                summary_raw = _read_json(summary_path)
            except (OSError, ValueError):
                summary_raw = None
            if summary_raw is None:
                return absent(
                    f"terminal checkpoint ({attempt.state.value}) with no summary at "
                    f"{summary_path}; verify calls that directory INCOMPLETE and so does this"
                )
            if (
                not isinstance(summary_raw, dict)
                or summary_raw.get("attempt_id") != attempt.attempt_id
            ):
                return absent(
                    f"terminal checkpoint ({attempt.state.value}) for attempt "
                    f"{attempt.attempt_id!r} but the summary at {summary_path} names "
                    f"{summary_raw.get('attempt_id') if isinstance(summary_raw, dict) else None!r}"
                    "; a finished record of some other attempt does not finish this one"
                )
            return found(
                AttemptDisposition.COMPLETE,
                reason=f"terminal state {attempt.state.value} with a finished record on disk",
                log=log_path,
            )

        if attempt.state is AttemptState.ESCALATED:
            if not attempt.escalation_id:
                return absent("ESCALATED checkpoint carries no escalation_id to re-open")
            request_path = output_dir / "escalations" / f"{attempt.escalation_id}.json"
            try:
                request_raw = _read_json(request_path)
            except (OSError, ValueError) as exc:
                return absent(
                    f"ESCALATED checkpoint names request {attempt.escalation_id!r} but "
                    f"{request_path} could not be read: {exc}"
                )
            if request_raw is None:
                return absent(
                    f"ESCALATED checkpoint names request {attempt.escalation_id!r} but "
                    f"{request_path} is missing"
                )
            request = decode_escalation_request(request_raw, attempt_id=attempt.attempt_id)
            if request is None or request.request_id != attempt.escalation_id:
                return absent(
                    f"ESCALATED checkpoint names request {attempt.escalation_id!r} but "
                    f"{request_path} does not decode as a request for attempt "
                    f"{attempt.attempt_id!r}; there is no question to re-enter, so the "
                    "attempt is driven again rather than lost"
                )
            return found(
                AttemptDisposition.AWAITING_DECISION,
                reason=f"suspended on operator request {attempt.escalation_id}",
                log=log_path if isinstance(log_raw, dict) else None,
            )

        if attempt.is_resumable:
            return found(
                AttemptDisposition.RESUMABLE,
                reason=f"interrupted in {attempt.state.value} at step {attempt.step_index}",
                log=log_path if isinstance(log_raw, dict) else None,
            )

        return absent(f"checkpoint state {attempt.state.value} is neither terminal nor resumable")

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

    async def load_round_record(self, round_index: int) -> dict[str, Any] | None:
        """The raw ``round-NN/round.json`` document, or ``None`` if none is there.

        Raw rather than decoded, so the caller can read ``run_id`` off a file
        that may not decode in full; :func:`decode_round_record` is the
        strict half.
        """
        path = self.round_dir(round_index) / "round.json"
        try:
            raw = await asyncio.to_thread(_read_json, path)
        except (OSError, ValueError) as exc:
            raise ContractViolationError(f"{path} could not be read: {exc}") from exc
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ContractViolationError(f"{path} is not a round record")
        return raw

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
