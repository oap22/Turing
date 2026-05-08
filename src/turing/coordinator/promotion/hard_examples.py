"""Failed-training salvage: hard examples → episode store + Discord summary.

When a training run fails (loss diverged, OOM, eval regression on the
held-out set), the cases that broke it become next-round training fodder.
This helper records each one as a FAILED episode and posts a one-line
Discord summary so the operator knows the run failed without having to
check logs.

Failed episodes are tagged with ``outcome=FAILED`` so they don't
accidentally become positive training data — the corpus builder filters
on outcome.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from turing.coordinator.lifecycle.episode_store import EpisodeStore


@dataclass(frozen=True)
class HardExample:
    subtask_id: str
    input_text: str
    expected_text: str
    actual_output: str
    failure_reason: str


class _Notifier(Protocol):
    def notify(self, kind: str, payload: dict) -> None: ...


def archive_failed_training(
    *,
    run_id: str,
    specialty: str,
    examples: Sequence[HardExample],
    episode_store: EpisodeStore,
    notifier: _Notifier,
) -> None:
    """Record every hard example as a FAILED episode and notify operator."""
    now_ms = int(time.time() * 1000)
    for ex in examples:
        episode_store.record(
            Episode(
                task_id=run_id,
                subtask_id=ex.subtask_id,
                worker_id="trainer",
                specialty=specialty,
                model_version="",
                adapter_version="",
                input_text=ex.input_text,
                trajectory=("training_run",),
                output_text=ex.actual_output,
                success=False,
                latency_ms=0,
                tokens_used=0,
                outcome=SubtaskState.FAILED,
                critic_score=0.0,
                recorded_at_ms=now_ms,
            )
        )
    notifier.notify(
        "training_failed",
        {
            "run_id": run_id,
            "specialty": specialty,
            "example_count": len(examples),
        },
    )
