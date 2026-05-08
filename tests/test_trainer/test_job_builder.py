"""TrainingJobBuilder: episode query → dataset JSONL with content-addressed dedup."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.learning.trainer import TrainingJobBuilder

if TYPE_CHECKING:
    from pathlib import Path


def _ep(
    *,
    subtask_id: str,
    specialty: str = "research-summarize",
    input_text: str = "summarise this paper",
    output_text: str = "the paper says X",
    critic_score: float = 0.8,
    outcome: SubtaskState = SubtaskState.COMPLETED,
) -> Episode:
    return Episode(
        task_id=f"t-{subtask_id}",
        subtask_id=subtask_id,
        worker_id="w1",
        specialty=specialty,
        model_version="qwen2.5-7b",
        adapter_version="base",
        input_text=input_text,
        trajectory=("act",),
        output_text=output_text,
        success=outcome is SubtaskState.COMPLETED,
        latency_ms=10,
        tokens_used=10,
        outcome=outcome,
        critic_score=critic_score,
        recorded_at_ms=0,
    )


def test_builds_jsonl_from_episode_store(tmp_path: Path) -> None:
    store = EpisodeStore()
    store.record(_ep(subtask_id="s-1", input_text="paper A", output_text="answer A"))
    store.record(_ep(subtask_id="s-2", input_text="paper B", output_text="answer B"))

    builder = TrainingJobBuilder(episode_store=store, min_critic_score=0.0)
    out_path = tmp_path / "dataset.jsonl"
    info = builder.build(
        specialty="research-summarize", top_k=5, out_path=out_path
    )
    assert info.example_count == 2
    lines = [
        json.loads(line)
        for line in out_path.read_text(encoding="utf-8").splitlines()
    ]
    assert {row["input"] for row in lines} == {"paper A", "paper B"}


def test_filters_below_critic_score(tmp_path: Path) -> None:
    store = EpisodeStore()
    store.record(_ep(subtask_id="s-1", critic_score=0.9))
    store.record(_ep(subtask_id="s-2", critic_score=0.3))

    builder = TrainingJobBuilder(episode_store=store, min_critic_score=0.7)
    out_path = tmp_path / "dataset.jsonl"
    info = builder.build(
        specialty="research-summarize", top_k=10, out_path=out_path
    )
    assert info.example_count == 1


def test_filters_failed_outcomes(tmp_path: Path) -> None:
    """A FAILED episode is not training data even if its critic score is high."""
    store = EpisodeStore()
    store.record(_ep(subtask_id="s-1", critic_score=0.9))
    store.record(
        _ep(subtask_id="s-2", critic_score=0.95, outcome=SubtaskState.FAILED)
    )

    builder = TrainingJobBuilder(episode_store=store, min_critic_score=0.7)
    out_path = tmp_path / "dataset.jsonl"
    info = builder.build(
        specialty="research-summarize", top_k=10, out_path=out_path
    )
    assert info.example_count == 1


def test_dedup_by_content_hash(tmp_path: Path) -> None:
    """Two episodes with byte-identical (input, output) pairs collapse to one."""
    store = EpisodeStore()
    store.record(_ep(subtask_id="s-1", input_text="dup", output_text="same"))
    store.record(_ep(subtask_id="s-2", input_text="dup", output_text="same"))
    store.record(_ep(subtask_id="s-3", input_text="other", output_text="same"))

    builder = TrainingJobBuilder(episode_store=store, min_critic_score=0.0)
    out_path = tmp_path / "dataset.jsonl"
    info = builder.build(
        specialty="research-summarize", top_k=10, out_path=out_path
    )
    assert info.example_count == 2
    assert info.duplicates_dropped == 1


def test_dataset_hash_is_content_addressed(tmp_path: Path) -> None:
    """Same episode set → same dataset hash. The hash makes 'we already
    trained this exact dataset' a one-line check upstream."""
    store = EpisodeStore()
    store.record(_ep(subtask_id="s-1"))
    store.record(_ep(subtask_id="s-2"))

    builder = TrainingJobBuilder(episode_store=store, min_critic_score=0.0)

    a = builder.build(
        specialty="research-summarize", top_k=10, out_path=tmp_path / "a.jsonl"
    )
    b = builder.build(
        specialty="research-summarize", top_k=10, out_path=tmp_path / "b.jsonl"
    )
    assert a.dataset_sha256 == b.dataset_sha256
    assert len(a.dataset_sha256) == 64


def test_specialty_isolation(tmp_path: Path) -> None:
    store = EpisodeStore()
    store.record(_ep(subtask_id="s-1", specialty="research-summarize"))
    store.record(_ep(subtask_id="s-2", specialty="code-debug"))

    builder = TrainingJobBuilder(episode_store=store, min_critic_score=0.0)
    info = builder.build(
        specialty="research-summarize",
        top_k=10,
        out_path=tmp_path / "ds.jsonl",
    )
    assert info.example_count == 1
