"""LessonStore.evict_expired — 60d TTL with pin exemption per ADR 0005 §1+§3.

Eviction rule:
- Created > 60d ago AND (unpinned OR pinned_until_ms < now) → evicted.
- Created <= 60d ago → kept regardless of pin state.
- Pinned-and-current → kept regardless of age.
- Lapsed pin (pinned_until_ms < now) reverts to TTL-eligible.
"""

from __future__ import annotations

from turing.learning.lessons import Lesson, LessonStore

DAY_MS = 24 * 60 * 60 * 1000
TTL_60D_MS = 60 * DAY_MS


def _lesson(
    *,
    task_id: str,
    created_at_ms: int,
    pinned_until_ms: int | None = None,
) -> Lesson:
    return Lesson(
        specialty="research-summarize",
        task_id=task_id,
        text=task_id,
        embedding=(0.1, 0.2),
        created_at_ms=created_at_ms,
        pinned_until_ms=pinned_until_ms,
    )


def test_recent_unpinned_lesson_survives() -> None:
    store = LessonStore()
    now = 1_000_000_000_000
    store.add(_lesson(task_id="recent", created_at_ms=now - 1 * DAY_MS))

    evicted = store.evict_expired(now_ms=now, ttl_ms=TTL_60D_MS)

    assert evicted == 0
    assert store.get_by_task_id("recent") is not None


def test_old_unpinned_lesson_evicted() -> None:
    store = LessonStore()
    now = 1_000_000_000_000
    store.add(_lesson(task_id="ancient", created_at_ms=now - 61 * DAY_MS))

    evicted = store.evict_expired(now_ms=now, ttl_ms=TTL_60D_MS)

    assert evicted == 1
    assert len(store) == 0


def test_old_pinned_lesson_survives() -> None:
    """Pinned-and-current lesson survives even past TTL (ADR 0005 §3)."""
    store = LessonStore()
    now = 1_000_000_000_000
    store.add(
        _lesson(
            task_id="pinned-old",
            created_at_ms=now - 90 * DAY_MS,
            pinned_until_ms=now + 10 * DAY_MS,  # pin still active
        )
    )

    evicted = store.evict_expired(now_ms=now, ttl_ms=TTL_60D_MS)

    assert evicted == 0
    assert store.get_by_task_id("pinned-old") is not None


def test_lapsed_pin_re_enters_eviction_pool() -> None:
    """Pin lapsed before now → TTL applies as if unpinned (ADR 0005 §3)."""
    store = LessonStore()
    now = 1_000_000_000_000
    store.add(
        _lesson(
            task_id="lapsed",
            created_at_ms=now - 90 * DAY_MS,
            pinned_until_ms=now - 1 * DAY_MS,  # pin expired yesterday
        )
    )

    evicted = store.evict_expired(now_ms=now, ttl_ms=TTL_60D_MS)

    assert evicted == 1
    assert len(store) == 0


def test_eviction_is_idempotent() -> None:
    """Running the sweep twice in a row evicts the same set, then nothing."""
    store = LessonStore()
    now = 1_000_000_000_000
    store.add(_lesson(task_id="ancient", created_at_ms=now - 61 * DAY_MS))
    store.add(_lesson(task_id="recent", created_at_ms=now - 1 * DAY_MS))

    first = store.evict_expired(now_ms=now, ttl_ms=TTL_60D_MS)
    second = store.evict_expired(now_ms=now, ttl_ms=TTL_60D_MS)

    assert first == 1
    assert second == 0
    assert len(store) == 1
