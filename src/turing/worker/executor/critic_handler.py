"""Judge-worker kind handler for `SubtaskKind.CRITIC_SCORE`.

Issue #117 slice B. The coordinator's `RemoteCritic` dispatches a JSON
payload describing a truncated episode; the judge LLM evaluates it and
returns a `CriticScore`. Empty output (TIMED_OUT/FAILED upstream) collapses
to a low-score critique referencing the failure mode rather than crashing.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from turing.learning.critic.critic import CriticScore

if TYPE_CHECKING:
    from turing.coordinator.dispatch import SubtaskDispatch

JudgeFn = Callable[[dict[str, object]], Awaitable[CriticScore]]


def _failure_score(payload: dict[str, object]) -> CriticScore:
    outcome = str(payload.get("outcome", "unknown"))
    return CriticScore(
        correctness=0.0,
        efficiency=0.0,
        specialty_fit=0.0,
        critique=f"empty output ({outcome}); upstream worker did not produce a usable result",
    )


def make_critic_handler(
    judge_fn: JudgeFn,
) -> Callable[[SubtaskDispatch], Awaitable[dict[str, object]]]:
    """Build a kind handler returning a `{output, status, ...}` dict.

    The wrapper handles the empty-output short-circuit so each judge_fn
    implementation doesn't have to. Output is JSON-encoded so RemoteCritic's
    `parse_critic_score` can reconstruct the value.
    """

    async def handle(envelope: SubtaskDispatch) -> dict[str, object]:
        payload = json.loads(envelope.prompt)
        output_text = str(payload.get("output_text", "")).strip()
        if not output_text:
            score = _failure_score(payload)
        else:
            score = await judge_fn(payload)
        return {
            "status": "COMPLETED",
            "output": json.dumps(
                {
                    "correctness": score.correctness,
                    "efficiency": score.efficiency,
                    "specialty_fit": score.specialty_fit,
                    "critique": score.critique,
                }
            ),
        }

    return handle
