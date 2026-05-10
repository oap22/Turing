"""Nightly lessons extraction job + synthesis worker handler (issue #121)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.learning.lessons.nightly import (
    LESSON_TTL_MS,
    LessonCandidate,
    next_run_at,
    run_nightly_extraction,
    should_run_now,
)
from turing.learning.lessons.store import LessonStore
from turing.worker.executor.lessons_handler import (
    make_extract_lessons_handler,
    parse_candidates,
)

UTC = UTC


def _episode(*, subtask_id: str, specialty: str, score: float, age_ms: int = 0) -> Episode:
    return Episode(
        task_id=f"t-{subtask_id}",
        subtask_id=subtask_id,
        worker_id="w",
        specialty=specialty,
        model_version="m",
        adapter_version="a",
        input_text="i",
        trajectory=("s",),
        output_text="o",
        success=True,
        latency_ms=1,
        tokens_used=1,
        outcome=SubtaskState.COMPLETED,
        critic_score=score,
        recorded_at_ms=age_ms,
    )


# ── Nightly job ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_nightly_extraction_per_specialty():
    eps = EpisodeStore()
    for i in range(5):
        eps.record(_episode(subtask_id=f"r-{i}", specialty="research-summarize", score=0.9))
    for i in range(2):
        eps.record(_episode(subtask_id=f"c-{i}", specialty="critic", score=0.7))

    seen_calls: list[tuple[str, int]] = []

    async def extract_fn(specialty, episodes):
        seen_calls.append((specialty, len(episodes)))
        return [
            LessonCandidate(
                task_id=f"L-{specialty}-{i}",
                text=f"lesson {i}",
                embedding=(float(i), 0.0, 0.0),
            )
            for i in range(2)
        ]

    store = LessonStore()
    added = await run_nightly_extraction(
        specialties=["research-summarize", "critic"],
        episode_store=eps,
        lesson_store=store,
        extract_fn=extract_fn,
        now_ms=1_000_000,
    )
    assert added == 4
    assert {s for s, _ in seen_calls} == {"research-summarize", "critic"}
    # Lessons are stored with created_at_ms set and pinned_until_ms=None.
    res_lessons = list(store.query(specialty="research-summarize", embedding=(0.0, 0.0, 0.0), k=10))
    assert len(res_lessons) == 2
    assert all(lesson.created_at_ms == 1_000_000 for lesson in res_lessons)
    assert all(lesson.pinned_until_ms is None for lesson in res_lessons)


@pytest.mark.asyncio
async def test_run_nightly_skips_specialties_with_no_episodes():
    eps = EpisodeStore()
    extract_calls: list[str] = []

    async def extract_fn(specialty, _episodes):
        extract_calls.append(specialty)
        return []

    store = LessonStore()
    added = await run_nightly_extraction(
        specialties=["research-summarize"],
        episode_store=eps,
        lesson_store=store,
        extract_fn=extract_fn,
        now_ms=0,
    )
    assert added == 0
    assert extract_calls == []


@pytest.mark.asyncio
async def test_run_nightly_top_20_per_specialty():
    eps = EpisodeStore()
    # 25 episodes; query(limit=20) should clip to 20.
    for i in range(25):
        eps.record(_episode(subtask_id=f"r-{i}", specialty="x", score=0.5 + 0.01 * i))

    captured: list[int] = []

    async def extract_fn(_specialty, episodes):
        captured.append(len(episodes))
        return []

    await run_nightly_extraction(
        specialties=["x"],
        episode_store=eps,
        lesson_store=LessonStore(),
        extract_fn=extract_fn,
        now_ms=0,
    )
    assert captured == [20]


@pytest.mark.asyncio
async def test_extraction_runs_eviction_sweep():
    eps = EpisodeStore()
    eps.record(_episode(subtask_id="r-1", specialty="x", score=0.9))

    store = LessonStore()
    # Pre-load an old expired lesson; eviction should remove it.
    store.add(
        type(store)
        and __import__("turing.learning.lessons.lesson", fromlist=["Lesson"]).Lesson(
            specialty="x",
            task_id="ancient",
            text="evict me",
            embedding=(0.0,),
            created_at_ms=0,
            pinned_until_ms=None,
        )
    )

    async def extract_fn(_s, _e):
        return [LessonCandidate(task_id="new", text="fresh", embedding=(0.0, 0.0))]

    now = LESSON_TTL_MS + 10_000
    await run_nightly_extraction(
        specialties=["x"],
        episode_store=eps,
        lesson_store=store,
        extract_fn=extract_fn,
        now_ms=now,
    )
    # The fresh lesson stays; the ancient one is gone.
    remaining = list(store.query(specialty="x", embedding=(0.0, 0.0), k=10))
    task_ids = {lesson.task_id for lesson in remaining}
    assert "new" in task_ids
    assert "ancient" not in task_ids


@pytest.mark.asyncio
async def test_disabled_flag_is_noop():
    eps = EpisodeStore()
    eps.record(_episode(subtask_id="r-1", specialty="x", score=0.9))

    async def extract_fn(_s, _e):
        raise AssertionError("must not run when disabled")

    added = await run_nightly_extraction(
        specialties=["x"],
        episode_store=eps,
        lesson_store=LessonStore(),
        extract_fn=extract_fn,
        now_ms=0,
        enabled=False,
    )
    assert added == 0


# ── Missed-night recovery ────────────────────────────────────────────


def test_should_run_when_never_run_and_past_scheduled_hour():
    now = datetime(2026, 5, 8, 9, 30, tzinfo=UTC)
    assert should_run_now(last_run_ts=None, now=now)


def test_should_not_run_before_scheduled_hour():
    now = datetime(2026, 5, 8, 1, 30, tzinfo=UTC)
    assert not should_run_now(last_run_ts=None, now=now)


def test_should_run_when_last_run_was_36h_ago():
    last = datetime(2026, 5, 6, 21, 0, tzinfo=UTC)
    now = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    assert should_run_now(last_run_ts=last, now=now)


def test_should_not_double_run_same_calendar_day():
    last = datetime(2026, 5, 8, 2, 5, tzinfo=UTC)
    now = datetime(2026, 5, 8, 14, 0, tzinfo=UTC)
    assert not should_run_now(last_run_ts=last, now=now)


def test_next_run_after_today_tick_is_tomorrow():
    now = datetime(2026, 5, 8, 14, 0, tzinfo=UTC)
    expected = datetime(2026, 5, 9, 2, 0, tzinfo=UTC)
    assert next_run_at(now=now) == expected


def test_next_run_before_today_tick_is_today():
    now = datetime(2026, 5, 8, 1, 0, tzinfo=UTC)
    expected = datetime(2026, 5, 8, 2, 0, tzinfo=UTC)
    assert next_run_at(now=now) == expected


# ── Synthesis worker handler ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesis_handler_returns_candidates():
    captured: list[tuple[str, int]] = []

    async def extractor(specialty, episodes):
        captured.append((specialty, len(episodes)))
        return [
            LessonCandidate(task_id="L1", text="cite sources", embedding=(0.1, 0.2)),
            LessonCandidate(task_id="L2", text="be brief", embedding=(0.3, 0.4)),
        ]

    handler = make_extract_lessons_handler(extractor)

    payload = json.dumps(
        {
            "specialty": "research-summarize",
            "episodes": [{"subtask_id": "e1"}, {"subtask_id": "e2"}],
        }
    )

    class _Env:
        prompt = payload

    out = await handler(_Env())  # type: ignore[arg-type]
    assert captured == [("research-summarize", 2)]
    parsed = parse_candidates(out["output"])
    assert [c.task_id for c in parsed] == ["L1", "L2"]
    assert parsed[0].embedding == (0.1, 0.2)
