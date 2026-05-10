"""RemoteCritic — Critic adapter that scores via a NATS judge worker.

ADR 0004 / issue #117 slice B. Wraps `SubtaskDispatchClient`: serializes the
truncated episode into a `SubtaskDispatch(kind=CRITIC_SCORE, specialty="judge")`,
awaits the result, and parses a `CriticScore` from the worker output.

Truncation runs on the coordinator side (head/tail per ADR 0004 §6) so the
judge prompt never carries the full trajectory. The wire format for the
output is JSON: `{"correctness", "efficiency", "specialty_fit", "critique"}`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from turing.coordinator.dispatch import (
    SourceInput,
    SubtaskDispatch,
    SubtaskKind,
)
from turing.learning.critic.critic import CriticScore
from turing.learning.critic.truncate import truncate_episode

if TYPE_CHECKING:
    from collections.abc import Callable

    from turing.coordinator.dispatch import SubtaskDispatchClient
    from turing.coordinator.lifecycle.episode_store import Episode

JUDGE_SPECIALTY = "judge"
_DEFAULT_DEADLINE_MS = 60_000  # 60s budget for a judge dispatch


def episode_to_judge_payload(episode: Episode) -> dict[str, object]:
    """JSON shape the judge worker receives in the dispatch prompt."""
    truncated = truncate_episode(episode)
    return {
        "subtask_id": truncated.subtask_id,
        "specialty": truncated.specialty,
        "input_text": truncated.input_text,
        "trajectory": list(truncated.trajectory),
        "output_text": truncated.output_text,
        "outcome": truncated.outcome.value
        if hasattr(truncated.outcome, "value")
        else str(truncated.outcome),
        "success": truncated.success,
    }


def parse_critic_score(output: str) -> CriticScore:
    """Parse the wire JSON the judge worker returns into a CriticScore."""
    data = json.loads(output)
    return CriticScore(
        correctness=float(data["correctness"]),
        efficiency=float(data["efficiency"]),
        specialty_fit=float(data["specialty_fit"]),
        critique=str(data.get("critique", "")),
    )


class RemoteCritic:
    """Critic implementation that dispatches CRITIC_SCORE subtasks to a judge worker."""

    def __init__(
        self,
        *,
        dispatch_client: SubtaskDispatchClient,
        now_ms: Callable[[], int],
        deadline_ms_budget: int = _DEFAULT_DEADLINE_MS,
        subtask_id_factory: Callable[[Episode], str] | None = None,
    ) -> None:
        self._client = dispatch_client
        self._now_ms = now_ms
        self._deadline_budget = deadline_ms_budget
        self._make_subtask_id = subtask_id_factory or (lambda ep: f"critic-{ep.subtask_id}")

    async def score(self, episode: Episode) -> CriticScore:
        payload = episode_to_judge_payload(episode)
        envelope = SubtaskDispatch(
            subtask_id=self._make_subtask_id(episode),
            task_id=episode.task_id,
            specialty=JUDGE_SPECIALTY,
            prompt=json.dumps(payload),
            source_inputs=[SourceInput(id="episode", text=json.dumps(payload))],
            deadline_ms=self._now_ms() + self._deadline_budget,
            kind=SubtaskKind.CRITIC_SCORE.value,
        )
        result = await self._client.dispatch(
            envelope,
            deadline_ms=envelope.deadline_ms,
        )
        if result.status != "COMPLETED":
            raise RuntimeError(
                f"judge dispatch for {episode.subtask_id!r} returned "
                f"status={result.status!r}: {result.error!r}"
            )
        return parse_critic_score(result.output)
