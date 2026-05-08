"""Synthesis-thumb attribution restricted to consumed-upstream subtasks.

Per ADR 0006 §3: synthesis-thumb fractional credit (±0.3) goes only to
non-synthesis subtasks whose ``output_key ∈ synthesis.consumed_keys``.
``consumed_keys`` is populated by the workspace_io tool wrapper on
each ``.read(key)`` call.

Static DAG attribution (credit everyone in depends_on) is rejected — it
over-credits researchers whose output synthesis ignored.
"""

from __future__ import annotations

import pytest

from turing.coordinator.episode_rewards import (
    EpisodeRewardsStore,
    RewardSource,
    write_synthesis_thumb,
)
from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState


def _episode(
    *,
    subtask_id: str,
    specialty: str,
    output_key: str,
    consumed_keys: tuple[str, ...] = (),
) -> Episode:
    return Episode(
        task_id="t1",
        subtask_id=subtask_id,
        worker_id="w1",
        specialty=specialty,
        model_version="qwen2.5:14b",
        adapter_version="v1",
        input_text="in",
        trajectory=("step",),
        output_text="out",
        success=True,
        latency_ms=100,
        tokens_used=10,
        outcome=SubtaskState.COMPLETED,
        critic_score=0.0,
        recorded_at_ms=1000,
        output_key=output_key,
        consumed_keys=consumed_keys,
    )


def test_synthesis_thumb_credits_only_consumed_upstreams() -> None:
    store = EpisodeRewardsStore()
    synthesis = _episode(
        subtask_id="syn",
        specialty="synthesis",
        output_key="workspace://t1/synthesis",
        consumed_keys=("workspace://t1/r1", "workspace://t1/r2"),
    )
    upstreams = [
        _episode(subtask_id="r1", specialty="research-deep", output_key="workspace://t1/r1"),
        _episode(subtask_id="r2", specialty="research-deep", output_key="workspace://t1/r2"),
        # r3 produced output but synthesis didn't read it.
        _episode(subtask_id="r3", specialty="research-deep", output_key="workspace://t1/r3"),
    ]

    write_synthesis_thumb(
        store,
        synthesis_episode=synthesis,
        upstream_episodes=upstreams,
        positive=True,
        recorded_at_ms=2000,
        discord_message_id="msg-99",
        discord_user_id="user-1",
    )

    # Synthesis itself: full ±1.0.
    assert store.effective_reward("syn") == pytest.approx(1.0)
    # r1 and r2 consumed → +0.3 each.
    assert store.effective_reward("r1") == pytest.approx(0.3)
    assert store.effective_reward("r2") == pytest.approx(0.3)
    # r3 not consumed → no credit.
    assert store.effective_reward("r3") == pytest.approx(0.0)


def test_synthesis_thumb_down_credits_negative() -> None:
    store = EpisodeRewardsStore()
    synthesis = _episode(
        subtask_id="syn",
        specialty="synthesis",
        output_key="workspace://t1/synthesis",
        consumed_keys=("workspace://t1/r1",),
    )
    upstreams = [
        _episode(subtask_id="r1", specialty="research-deep", output_key="workspace://t1/r1"),
    ]
    write_synthesis_thumb(
        store,
        synthesis_episode=synthesis,
        upstream_episodes=upstreams,
        positive=False,
        recorded_at_ms=2000,
    )
    assert store.effective_reward("syn") == pytest.approx(-1.0)
    assert store.effective_reward("r1") == pytest.approx(-0.3)


def test_event_records_source_and_discord_metadata() -> None:
    store = EpisodeRewardsStore()
    synthesis = _episode(
        subtask_id="syn",
        specialty="synthesis",
        output_key="workspace://t1/synthesis",
        consumed_keys=("workspace://t1/r1",),
    )
    upstreams = [
        _episode(subtask_id="r1", specialty="research-deep", output_key="workspace://t1/r1"),
    ]
    write_synthesis_thumb(
        store,
        synthesis_episode=synthesis,
        upstream_episodes=upstreams,
        positive=True,
        recorded_at_ms=2000,
        discord_message_id="msg-99",
        discord_user_id="user-1",
    )
    syn_event = store.events_for("syn")[0]
    assert syn_event.source is RewardSource.SYNTHESIS_THUMB_FRACTIONAL
    assert syn_event.discord_user_id == "user-1"
    assert syn_event.discord_message_id == "msg-99"
