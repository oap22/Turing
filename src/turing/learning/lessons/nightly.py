"""Nightly lessons extraction job (issue #121, ADR 0005 §1).

For each specialty: pull top-20 critic-scored episodes from the last 24h,
hand them to a synthesis-worker `extract` callable, and store the returned
candidates in `LessonStore`. After extraction, evict expired lessons.

The extractor callable is injected — production wires it to a NATS-dispatched
synthesis worker (`SubtaskKind.EXTRACT_LESSONS`); tests pass a deterministic
fake. This lets the job's orchestration logic be unit-tested without a bus.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

from turing.learning.lessons.lesson import Lesson

if TYPE_CHECKING:
    from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
    from turing.learning.lessons.store import LessonStore


LESSON_TTL_MS = 60 * 24 * 60 * 60 * 1000
TOP_K_EPISODES = 20


@dataclass(frozen=True)
class LessonCandidate:
    """What the extractor returns per lesson — embeddings live alongside text."""

    task_id: str
    text: str
    embedding: tuple[float, ...]


# Extractor: (specialty, episodes) -> list of candidate lessons.
ExtractFn = Callable[[str, Sequence["Episode"]], Awaitable[list[LessonCandidate]]]


async def run_nightly_extraction(
    *,
    specialties: Sequence[str],
    episode_store: EpisodeStore,
    lesson_store: LessonStore,
    extract_fn: ExtractFn,
    now_ms: int,
    enabled: bool = True,
) -> int:
    """Run one pass of the nightly job. Returns lesson count added."""
    if not enabled:
        return 0
    added = 0
    for specialty in specialties:
        episodes = episode_store.query(
            specialty=specialty,
            min_score=0.0,
            positive_only=True,
            limit=TOP_K_EPISODES,
        )
        if not episodes:
            continue
        candidates = await extract_fn(specialty, episodes)
        for cand in candidates:
            lesson_store.add(
                Lesson(
                    specialty=specialty,
                    task_id=cand.task_id,
                    text=cand.text,
                    embedding=cand.embedding,
                    created_at_ms=now_ms,
                    pinned_until_ms=None,
                )
            )
            added += 1
    # Eviction sweep follows extraction in the same invocation so the store
    # never accumulates expired rows between runs.
    lesson_store.evict_expired(now_ms=now_ms, ttl_ms=LESSON_TTL_MS)
    return added


# ── Missed-night recovery ────────────────────────────────────────────


_NIGHTLY_HOUR = 2  # 02:00 local


def should_run_now(
    *,
    last_run_ts: datetime | None,
    now: datetime,
    nightly_hour: int = _NIGHTLY_HOUR,
) -> bool:
    """True when the nightly run is overdue and hasn't run today yet.

    Returns True if either:
      - `last_run_ts` is None (never run), and `now` is at or past today's
        scheduled time;
      - `last_run_ts` is on a strictly earlier calendar day and the most
        recent scheduled tick has passed.
    """
    scheduled_today = datetime.combine(now.date(), time(hour=nightly_hour), tzinfo=now.tzinfo)
    if now < scheduled_today:
        # Today's tick hasn't arrived yet.
        return False
    if last_run_ts is None:
        return True
    # Block re-running on the same calendar day even if the tick happened.
    return last_run_ts.date() < now.date()


def next_run_at(*, now: datetime, nightly_hour: int = _NIGHTLY_HOUR) -> datetime:
    """Next scheduled tick at or after `now`."""
    scheduled_today = datetime.combine(now.date(), time(hour=nightly_hour), tzinfo=now.tzinfo)
    if now < scheduled_today:
        return scheduled_today
    return scheduled_today + timedelta(days=1)


__all__ = [
    "LESSON_TTL_MS",
    "TOP_K_EPISODES",
    "ExtractFn",
    "LessonCandidate",
    "next_run_at",
    "run_nightly_extraction",
    "should_run_now",
    "timezone",  # convenience re-export to keep callers' imports tidy
]
