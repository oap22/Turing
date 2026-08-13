"""**LOOP-2 SEAM — nothing in loop 1 calls this.**

This module is the *only* sanctioned path from a round's results into a
self-edit, and it exists now, unused, so that the self-edit step cannot be
written without it. Loop 1 is the frozen-scaffold baseline: nothing
self-modifies, there is no cheat detector, and there is no rollback. Those
three are loop-2 items. The frozen error taxonomy is not: ADR 0011 R4
gates it *before round 0*, because aggregate stats are only comparable if
failures are categorised the same way every round. This module still
leaves the taxonomy slot empty.

**What the self-edit step is permitted to see** (brief, § "The self-edit step —
what the agent may look at"): *aggregate stats plus a few sampled
trajectories*, and **practice split only**.

* *Aggregate stats* — per-problem score and correctness, where time and steps
  went, and an **error taxonomy**. Per ADR R4 that taxonomy is a before-
  round-0 prerequisite (the brief also gated it before the first self-edit;
  the ADR takes the stricter reading). :class:`SelfEditInputs` carries a
  slot for it; nothing here authors or freezes one, and loop 1 leaves it
  empty.
* *A handful of full trajectories* — the contents of the per-attempt JSON logs
  written by :class:`~turing.research.loop.trajectory.TrajectoryStore`, inlined
  so the agent never holds a path into ``round-NN/attempts/``.

**Why the channel is exactly this wide.** Full-trajectory access to everything
is the widest channel and the easiest way to encode task-specific answers into
skills; failures-only is blind to what made successes work; letting the agent
choose makes the channel vary per round and wrecks attribution. This width is
rich enough to find real patterns and narrow enough to limit memorisation.

**Why held-out never enters.** Without the split, the agent reads round *N*'s
results on the whole corpus, edits its skills, and is re-scored on the same
corpus — at which point "got better at ML research" is indistinguishable from
"memorised eleven problems". The practice-vs-held-out gap is the direct
evidence of memorisation and only exists if the held-out side stays unseen.
:func:`collect_self_edit_inputs` filters on
:attr:`~turing.research.contracts.Problem.is_practice` and re-checks the cells
it took from :meth:`RoundRecord.self_edit_visible_scores`; a held-out cell
reaching it is a :class:`ContractViolationError`, not a warning.

What loop 2 still has to build before calling any of this: harness isolation
enforced outside the agent's process, the cheat detector, and rollback.
The frozen error taxonomy is a before-round-0 item (ADR R4), not a loop-2
blocker; the slot on :class:`SelfEditInputs` is empty until it exists.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from turing.research.contracts import ContractViolationError, Split

if TYPE_CHECKING:
    from turing.research.contracts import RoundRecord, TypeScore
    from turing.research.loop.runner import AttemptOutcome

logger = structlog.get_logger(__name__)

__all__ = ["SelfEditInputs", "SelfEditStep", "collect_self_edit_inputs"]


@dataclass(frozen=True, slots=True)
class SelfEditInputs:
    """Everything a loop-2 self-edit step may read about a finished round.

    Deliberately a closed record rather than a handle to the round directory:
    an agent given the directory would have held-out trajectories one
    ``glob`` away. Sampled trajectories are therefore the log payloads, not
    ``Path`` s into ``round-NN/attempts/``.
    """

    round_index: int
    run_id: str
    eval_set_hash: str
    scaffold_git_sha: str
    practice_scores: tuple[TypeScore, ...]
    sampled_trajectories: tuple[Mapping[str, Any], ...]
    per_problem: Mapping[str, Mapping[str, Any]]
    #: Frozen failure categories. Before-round-0 prerequisite (ADR R4); empty here.
    error_taxonomy: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for ts in self.practice_scores:
            if ts.split is not Split.PRACTICE:
                raise ContractViolationError(
                    f"held-out cell {ts.problem_type.value}/{ts.split.value} reached a "
                    "self-edit summary; the practice-vs-held-out gap is the only evidence "
                    "of memorisation and it does not survive the agent seeing both"
                )
        object.__setattr__(self, "practice_scores", tuple(self.practice_scores))
        object.__setattr__(
            self, "sampled_trajectories", _freeze_sampled_trajectories(self.sampled_trajectories)
        )
        object.__setattr__(self, "per_problem", MappingProxyType(dict(self.per_problem)))
        object.__setattr__(self, "error_taxonomy", MappingProxyType(dict(self.error_taxonomy)))


class SelfEditStep(Protocol):
    """**The attach point for loop 2.** Nothing implements this yet.

    An implementation proposes a scaffold edit (skill files in the
    ``turing-skills`` repo, where the git SHA *is* the scaffold version, a diff
    is the answer to "what changed", and ``git revert`` is rollback) from
    ``inputs`` alone, and returns the new SHA for the next round's
    :class:`~turing.research.contracts.EngineIdentity`.
    """

    async def propose(self, inputs: SelfEditInputs) -> str:
        """Return the ``turing-skills`` SHA the next round should run at."""
        ...


def collect_self_edit_inputs(
    record: RoundRecord,
    outcomes: Sequence[AttemptOutcome],
    *,
    sample_limit: int = 3,
) -> SelfEditInputs:
    """Assemble the practice-only summary. **Loop 1 never calls this.**

    Args:
        record: the finished round. Cells come from
            :meth:`RoundRecord.self_edit_visible_scores`, which drops held-out
            by construction.
        outcomes: the round's attempts. Held-out attempts are filtered out
            here, so no held-out trajectory path or per-problem stat survives.
        sample_limit: how many full trajectories to hand over. "A handful" is
            the brief's width; a larger number is a wider channel into
            memorisation and should be argued for, not defaulted into.

    Raises:
        ContractViolationError: ``sample_limit`` below 1, or a held-out cell
            somehow present.
    """
    if sample_limit < 1:
        raise ContractViolationError("a self-edit summary with no trajectories is not the design")
    practice = [o for o in outcomes if o.problem.is_practice]
    dropped = len(outcomes) - len(practice)
    # Lowest-scoring practice attempts first: the informative ones are where
    # the scaffold struggled. Deterministic order so a round is reproducible.
    ranked = sorted(
        practice,
        key=lambda o: (
            o.best_result.passed_correctness if o.best_result else False,
            o.best_result.score if o.best_result else float("-inf"),
            o.problem.id,
        ),
    )
    sampled = tuple(
        _load_sampled_trajectory(o.attempt_log_path)
        for o in ranked[:sample_limit]
        if o.attempt_log_path is not None
    )
    per_problem: dict[str, dict[str, Any]] = {
        o.problem.id: {
            "problem_type": o.problem.problem_type.value,
            "final_state": o.attempt.state.value,
            "score": None if o.best_result is None else o.best_result.score,
            "passed_correctness": (
                None if o.best_result is None else o.best_result.passed_correctness
            ),
            "steps": o.attempt.consumed.steps,
            "tokens": o.attempt.consumed.tokens,
            "wall_clock_seconds": o.attempt.consumed.wall_clock_seconds,
            "escalations": o.escalation_count,
        }
        for o in practice
    }
    logger.info(
        "research.self_edit_seam.collected",
        round_index=record.round_index,
        practice_problems=len(practice),
        held_out_dropped=dropped,
        sampled_trajectories=len(sampled),
    )
    return SelfEditInputs(
        round_index=record.round_index,
        run_id=record.run_id,
        eval_set_hash=record.eval_set_hash,
        scaffold_git_sha=record.engine.scaffold_git_sha,
        practice_scores=record.self_edit_visible_scores(),
        sampled_trajectories=sampled,
        per_problem=per_problem,
    )


def _freeze_sampled_trajectories(sampled: Sequence[object]) -> tuple[Mapping[str, Any], ...]:
    """Refuse paths. A Path into ``attempts/`` has held-out logs one glob away."""
    if isinstance(sampled, (Path, str, bytes)) or not isinstance(sampled, (tuple, list)):
        raise ContractViolationError(
            "sampled trajectories are payloads, not paths; a path into "
            "round-NN/attempts/ has held-out logs one glob away"
        )
    frozen: list[Mapping[str, Any]] = []
    for item in sampled:
        if isinstance(item, Path):
            raise ContractViolationError(
                "sampled trajectories are payloads, not paths; a path into "
                "round-NN/attempts/ has held-out logs one glob away"
            )
        if not isinstance(item, Mapping):
            raise ContractViolationError(
                f"sampled trajectory must be a mapping, got {type(item).__qualname__}"
            )
        frozen.append(MappingProxyType(dict(item)))
    return tuple(frozen)


def _load_sampled_trajectory(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ContractViolationError(
            f"sampled trajectory {path} is missing; the self-edit summary cannot "
            "invent a log it did not read"
        ) from None
    if not isinstance(payload, dict):
        raise ContractViolationError(
            f"sampled trajectory {path} is not an object; a non-object log cannot "
            "be a closed record"
        )
    return MappingProxyType(payload)
