"""WorkspaceIO tool wrapper — populates Episode.consumed_keys (issue #116)."""

from __future__ import annotations

import pytest

from turing.coordinator.episode_rewards import EpisodeRewardsStore, write_synthesis_thumb
from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.workspace.client import InMemoryWorkspaceClient
from turing.coordinator.workspace.ref import WorkspaceRef
from turing.worker.tools.workspace_io import WorkspaceIO


def _episode(
    *,
    subtask_id: str,
    specialty: str,
    output_key: str = "",
    consumed_keys: tuple[str, ...] = (),
) -> Episode:
    return Episode(
        task_id="t1",
        subtask_id=subtask_id,
        worker_id="w1",
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
        critic_score=0.0,
        recorded_at_ms=0,
        output_key=output_key,
        consumed_keys=consumed_keys,
    )


def _client_with(task_id: str, pairs: dict[str, bytes]) -> InMemoryWorkspaceClient:
    client = InMemoryWorkspaceClient()
    for k, v in pairs.items():
        client.put(WorkspaceRef(task_id=task_id, key=k), v)
    return client


def test_read_returns_blob_and_appends_to_buffer() -> None:
    client = _client_with("t1", {"k1": b"alpha", "k2": b"beta"})
    io = WorkspaceIO(client=client, task_id="t1")
    assert io.read("k1") == b"alpha"
    assert io.read("k2") == b"beta"
    assert io.consumed_keys == ("k1", "k2")


def test_buffer_empty_when_no_reads() -> None:
    io = WorkspaceIO(client=InMemoryWorkspaceClient(), task_id="t1")
    assert io.consumed_keys == ()


def test_duplicate_reads_kept_in_order() -> None:
    client = _client_with("t1", {"k1": b"a"})
    io = WorkspaceIO(client=client, task_id="t1")
    io.read("k1")
    io.read("k1")
    assert io.consumed_keys == ("k1", "k1")


def test_reset_clears_buffer_between_subtasks() -> None:
    client = _client_with("t1", {"k1": b"a", "k2": b"b"})
    io = WorkspaceIO(client=client, task_id="t1")
    io.read("k1")
    io.reset()
    io.read("k2")
    assert io.consumed_keys == ("k2",)


def test_consumed_keys_flow_into_episode_and_attribution_credits_only_consumed() -> None:
    client = _client_with(
        "t1",
        {"r1": b"x", "r2": b"y", "r3": b"z"},
    )
    io = WorkspaceIO(client=client, task_id="t1")
    io.read("r1")
    io.read("r2")

    synthesis = _episode(
        subtask_id="syn",
        specialty="synthesis",
        output_key="synthesis",
        consumed_keys=io.consumed_keys,
    )
    upstreams = [
        _episode(subtask_id="r1", specialty="research-deep", output_key="r1"),
        _episode(subtask_id="r2", specialty="research-deep", output_key="r2"),
        _episode(subtask_id="r3", specialty="research-deep", output_key="r3"),
    ]

    rewards = EpisodeRewardsStore()
    write_synthesis_thumb(
        rewards,
        synthesis_episode=synthesis,
        upstream_episodes=upstreams,
        positive=True,
        recorded_at_ms=1,
    )

    assert rewards.effective_reward("r1") == pytest.approx(0.3)
    assert rewards.effective_reward("r2") == pytest.approx(0.3)
    assert rewards.effective_reward("r3") == pytest.approx(0.0)
