"""Reward attribution from operator thumbs feedback (slice 15/26, #17).

Three pure, source-agnostic functions that map a feedback event to a list of
:class:`RewardAssignment` rows. The caller (the webui queue/chat reward
emitter or a background task) is responsible for persisting these into the
episode store. ADR 0010 moved the input surface off Discord; the attribution
logic is unchanged.

Routing rules (PRD lifecycle, story 4):

* **Subtask-thread thumbs** → that subtask's episode at full weight.
* **Main-message thumbs** → synthesis episode at full weight, plus
  fractional credit (±0.3) to every non-synthesis subtask whose
  workspace key the synthesis read.
* **No feedback in 24h** → the critic's score becomes the reward for each
  subtask that has one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

SUBTASK_THUMB_WEIGHT: float = 1.0
SYNTHESIS_THUMB_WEIGHT: float = 1.0
SUBTASK_FRACTIONAL_FROM_SYNTHESIS: float = 0.3
FEEDBACK_TIMEOUT_MS: int = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class RewardAssignment:
    subtask_id: str
    score: float


def attribute_subtask_thumb(*, subtask_id: str, positive: bool) -> list[RewardAssignment]:
    sign = 1.0 if positive else -1.0
    return [RewardAssignment(subtask_id=subtask_id, score=sign * SUBTASK_THUMB_WEIGHT)]


def attribute_main_thumb(
    *,
    synthesis_subtask_id: str,
    synthesis_input_subtasks: Iterable[str],
    positive: bool,
) -> list[RewardAssignment]:
    sign = 1.0 if positive else -1.0
    out: list[RewardAssignment] = [
        RewardAssignment(
            subtask_id=synthesis_subtask_id,
            score=sign * SYNTHESIS_THUMB_WEIGHT,
        )
    ]
    for sid in synthesis_input_subtasks:
        out.append(
            RewardAssignment(
                subtask_id=sid,
                score=sign * SUBTASK_FRACTIONAL_FROM_SYNTHESIS,
            )
        )
    return out


def critic_fallback_rewards(
    *,
    recorded_at_ms: dict[str, int],
    critic_scores: dict[str, float],
    now_ms: int,
    timeout_ms: int = FEEDBACK_TIMEOUT_MS,
) -> list[RewardAssignment]:
    """For any subtask whose feedback window has elapsed, fall back to its critic score."""
    out: list[RewardAssignment] = []
    for sid, ts in recorded_at_ms.items():
        if now_ms - ts >= timeout_ms and sid in critic_scores:
            out.append(RewardAssignment(subtask_id=sid, score=critic_scores[sid]))
    return out
