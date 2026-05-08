"""EpisodeStore — closed subtasks become rows here, queryable by Phase A/B/C.

This is the in-memory deep module. Persistence (SQLite projection of the
JetStream `tasks.<id>.events` subject) is layered above. Tests pin behaviour:
ordering, filtering, REJECTED tagging, idempotency on `subtask_id`.

The episode schema mirrors PRD story 33.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from turing.coordinator.lifecycle.lifecycle import SubtaskState


class CriticStatus(str, Enum):  # noqa: UP042
    """Per-episode critic-scoring lifecycle (ADR 0004 §6).

    PENDING: written at episode close, eligible for backfill.
    SCORED: critic returned a score; ``critic_score`` is meaningful.
    FAILED: retry budget exhausted; backfill skips so a poison-pill
    episode never re-enters the queue.
    """

    PENDING = "pending"
    SCORED = "scored"
    FAILED = "failed"


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
    # Cents spent on cloud LLM calls for this subtask. Reportable via
    # ``SELECT task_id, cents_spent`` per slice 12 (#14). Defaults to 0 so
    # legacy rows recorded before the budget gate landed remain valid.
    cents_spent: int = 0
    # Workspace key this subtask wrote its output to (ADR 0006 §3).
    # Synthesis attribution credits upstream subtasks whose output_key
    # appears in synthesis.consumed_keys. Defaults to "" for legacy rows.
    output_key: str = ""
    # Workspace keys this subtask read via the workspace_io tool, populated
    # by the wrapper at runtime; immutable post-close. Defaults to () for
    # legacy rows pre-dating consumed-keys tracking.
    consumed_keys: tuple[str, ...] = ()


class EpisodeStore:
    def __init__(self) -> None:
        self._rows: dict[str, Episode] = {}
        self._critic_status: dict[str, CriticStatus] = {}

    def all_episodes(self) -> list[Episode]:
        """Snapshot of every recorded episode. Caller-owned; safe to iterate."""
        return list(self._rows.values())

    def record(self, episode: Episode) -> None:
        if not episode.outcome.is_terminal():
            raise ValueError(
                f"episode outcome must be terminal, got {episode.outcome.value}"
            )
        # First write wins so a retry storm cannot overwrite the original
        # outcome row that downstream training corpora may have already read.
        if episode.subtask_id not in self._rows:
            self._rows[episode.subtask_id] = episode
            self._critic_status[episode.subtask_id] = CriticStatus.PENDING

    def get(self, subtask_id: str) -> Episode:
        """Return the recorded episode; raise ``KeyError`` if absent."""
        return self._rows[subtask_id]

    def update_critic_score(self, *, subtask_id: str, critic_score: float) -> None:
        """Backfill an episode's critic_score after async critic scoring.

        Outcome and trajectory remain immutable; only the critic-derived score
        can be updated post-hoc.
        """
        existing = self._rows.get(subtask_id)
        if existing is None:
            raise KeyError(f"no episode for subtask_id={subtask_id!r}")
        self._rows[subtask_id] = replace(existing, critic_score=critic_score)
        self._critic_status[subtask_id] = CriticStatus.SCORED

    def mark_critic_failed(self, *, subtask_id: str) -> None:
        """Mark an episode's critic-scoring as permanently failed (ADR 0004 §6).

        Retry budget exhausted. ``critic_score`` stays at its existing value
        (typically the default 0.0); status is the durable signal.
        """
        if subtask_id not in self._rows:
            raise KeyError(f"no episode for subtask_id={subtask_id!r}")
        self._critic_status[subtask_id] = CriticStatus.FAILED

    def critic_status_of(self, subtask_id: str) -> CriticStatus:
        if subtask_id not in self._critic_status:
            raise KeyError(f"no episode for subtask_id={subtask_id!r}")
        return self._critic_status[subtask_id]

    def backfill_unscored(self, *, now_ms: int, horizon_ms: int) -> list[Episode]:
        """Episodes eligible for critic backfill per ADR 0004 §5.

        Filters: status PENDING, recorded within ``horizon_ms`` of ``now_ms``,
        outcome not REJECTED (audit-only per PRD safety contract).
        """
        cutoff = now_ms - horizon_ms
        results = [
            ep
            for sid, ep in self._rows.items()
            if self._critic_status.get(sid) is CriticStatus.PENDING
            and ep.recorded_at_ms > cutoff
            and ep.outcome is not SubtaskState.REJECTED
        ]
        results.sort(key=lambda e: e.recorded_at_ms)
        return results

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
