"""Canary REJECTED handler — hard-examples + Discord notify (#118 follow-up)."""

from __future__ import annotations

import pytest

from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.coordinator.promotion.canary_gate_runner import CanaryGateOutcome
from turing.coordinator.promotion.canary_rejection import (
    REGRESSION_HORIZON_MS,
    RejectionLog,
    format_rejection_notice,
    make_canary_rejected_handler,
)
from turing.coordinator.promotion.hard_examples import HardExample


def _outcome(*, hard_examples: tuple[HardExample, ...] = ()) -> CanaryGateOutcome:
    return CanaryGateOutcome(
        promoted=False,
        score=67.0,
        delta_pp=-3.0,
        status="regression",
        hard_examples=hard_examples,
    )


# ── RejectionLog ─────────────────────────────────────────────────────


def test_rejection_log_counts_within_horizon():
    log = RejectionLog()
    log.record(specialty="x", name="a", version="v1", ts_ms=1_000)
    log.record(specialty="x", name="a", version="v2", ts_ms=2_000)
    log.record(specialty="y", name="b", version="v1", ts_ms=2_000)

    assert log.count_within(specialty="x", now_ms=2_500, horizon_ms=2_000) == 2
    assert log.count_within(specialty="y", now_ms=2_500, horizon_ms=2_000) == 1
    # Outside horizon
    assert log.count_within(specialty="x", now_ms=10_000, horizon_ms=1_000) == 0


# ── Notice formatting ────────────────────────────────────────────────


def test_format_rejection_notice_includes_all_fields():
    body = format_rejection_notice(
        name="research",
        version="v3",
        specialty="research-deep",
        outcome=_outcome(hard_examples=(
            HardExample(subtask_id="c1", input_text="i", expected_text="e", actual_output="a", failure_reason="r"),
        )),
        prior_name="research",
        prior_version="v2",
        regressions_30d=4,
    )
    assert "research:v3" in body
    assert "research-deep" in body
    assert "-3.00pp" in body
    assert "research:v2" in body  # reverted-to label
    assert "Hard-examples batch: 1 cases" in body
    assert "Regressions this month for `research-deep`: 4" in body


def test_format_rejection_notice_handles_no_prior_live():
    body = format_rejection_notice(
        name="r",
        version="v1",
        specialty="x",
        outcome=CanaryGateOutcome(
            promoted=False, score=None, delta_pp=None, status="load_failed",
        ),
        prior_name=None,
        prior_version=None,
        regressions_30d=0,
    )
    assert "(no prior LIVE)" in body
    assert "n/a" in body


# ── on_rejected handler ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_writes_hard_examples_and_posts_notice():
    episode_store = EpisodeStore()
    log = RejectionLog()
    notifier_calls: list[tuple[str, dict]] = []

    class _Notifier:
        def notify(self, kind, payload):
            notifier_calls.append((kind, payload))

    posted: list[str] = []

    async def discord_post(body):
        posted.append(body)

    handler = make_canary_rejected_handler(
        episode_store=episode_store,
        rejection_log=log,
        notifier=_Notifier(),
        discord_post=discord_post,
        now_ms=lambda: 5_000,
    )

    outcome = _outcome(
        hard_examples=(
            HardExample(subtask_id="c1", input_text="q", expected_text="e", actual_output="a", failure_reason="wrong"),
            HardExample(subtask_id="c2", input_text="q2", expected_text="e2", actual_output="a2", failure_reason="missing"),
        )
    )
    await handler(outcome, "research", "v3", "research-deep")

    # Hard examples archived as FAILED episodes.
    archived = episode_store.all_episodes() if hasattr(episode_store, "all_episodes") else list(episode_store._rows.values())
    assert {e.subtask_id for e in archived} == {"c1", "c2"}
    assert all(e.specialty == "research-deep" for e in archived)

    # Notifier and Discord both called.
    assert notifier_calls and notifier_calls[0][0] == "training_failed"
    assert posted and "research:v3" in posted[0]
    assert "Regressions this month for `research-deep`: 1" in posted[0]
    # RejectionLog updated.
    assert log.count_within(
        specialty="research-deep", now_ms=5_000, horizon_ms=REGRESSION_HORIZON_MS
    ) == 1


@pytest.mark.asyncio
async def test_handler_no_hard_examples_skips_archive_but_still_posts():
    episode_store = EpisodeStore()
    log = RejectionLog()
    class _Notifier:
        def notify(self, kind, payload):
            raise AssertionError("no hard examples → notifier should not fire")

    posted: list[str] = []

    async def discord_post(body):
        posted.append(body)

    handler = make_canary_rejected_handler(
        episode_store=episode_store,
        rejection_log=log,
        notifier=_Notifier(),
        discord_post=discord_post,
        now_ms=lambda: 1_000,
    )
    await handler(_outcome(hard_examples=()), "r", "v1", "x")

    archived = (
        episode_store.all_episodes()
        if hasattr(episode_store, "all_episodes")
        else list(episode_store._rows.values())
    )
    assert archived == []
    assert posted, "Discord notice still goes out even with no hard examples"


@pytest.mark.asyncio
async def test_handler_works_without_discord_post():
    episode_store = EpisodeStore()
    log = RejectionLog()
    class _Notifier:
        def notify(self, *args, **kwargs):
            pass

    handler = make_canary_rejected_handler(
        episode_store=episode_store,
        rejection_log=log,
        notifier=_Notifier(),
        discord_post=None,
        now_ms=lambda: 1,
    )
    await handler(_outcome(), "r", "v1", "x")  # must not raise
