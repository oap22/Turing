"""EpisodeStore — closed subtasks become rows here, queryable by Phase A/B/C.

This is the in-memory deep module. Persistence (SQLite projection of the
JetStream `tasks.<id>.events` subject) is layered above. Tests pin behaviour:
ordering, filtering, REJECTED tagging, idempotency on `subtask_id`.

The episode schema mirrors PRD story 33.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from turing.coordinator.lifecycle.lifecycle import SubtaskState


@dataclass(frozen=True)
class Episode:
    task_id: str
    subtask_id: str
    worker_id: str
    specialty: str
    model_version: str
    adapter_version: str
    input_text: str
    trajectory: tuple[str, ...]
    output_text: str
    success: bool
    latency_ms: int
    tokens_used: int
    outcome: SubtaskState
    critic_score: float
    recorded_at_ms: int
    # Per-specialty prompt version that produced this episode. Required for
    # sound A/B comparisons during nightly prompt evolution (slice 19, #21).
    # Defaults to "" so episodes recorded before the evolver shipped remain
    # constructible.
    prompt_version: str = ""


class EpisodeStore:
    def __init__(self) -> None:
        self._rows: dict[str, Episode] = {}

    def record(self, episode: Episode) -> None:
        if not episode.outcome.is_terminal():
            raise ValueError(
                f"episode outcome must be terminal, got {episode.outcome.value}"
            )
        # First write wins so a retry storm cannot overwrite the original
        # outcome row that downstream training corpora may have already read.
        self._rows.setdefault(episode.subtask_id, episode)

    def update_critic_score(self, *, subtask_id: str, critic_score: float) -> None:
        """Backfill an episode's critic_score after async critic scoring.

        Outcome and trajectory remain immutable; only the critic-derived score
        can be updated post-hoc.
        """
        existing = self._rows.get(subtask_id)
        if existing is None:
            raise KeyError(f"no episode for subtask_id={subtask_id!r}")
        self._rows[subtask_id] = replace(existing, critic_score=critic_score)

    def query(
        self,
        *,
        specialty: str,
        min_score: float = 0.0,
        positive_only: bool = False,
        limit: int | None = None,
    ) -> list[Episode]:
        results = [
            ep
            for ep in self._rows.values()
            if ep.specialty == specialty
            and ep.critic_score >= min_score
            and (not positive_only or ep.outcome is not SubtaskState.REJECTED)
        ]
        results.sort(key=lambda e: e.recorded_at_ms, reverse=True)
        if limit is not None:
            results = results[:limit]
        return results
