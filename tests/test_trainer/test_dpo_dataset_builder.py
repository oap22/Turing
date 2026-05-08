"""DPO dataset builder: (winner, loser) pairs → JSONL."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.learning.trainer import DPODatasetBuilder

if TYPE_CHECKING:
    from pathlib import Path


def _ep(
    *,
    subtask_id: str,
    input_text: str = "summarise this paper",
    output_text: str = "answer",
    critic_score: float = 0.5,
    user_score: float = 0.0,
    specialty: str = "research-summarize",
) -> Episode:
    # The episode model didn't ship a user_score field; we attach via
    # critic_score for now and the builder reads either signal. The
    # builder's contract documents how it picks winners/losers.
    _ = user_score  # forward-compat marker
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
        success=True,
        latency_ms=10,
        tokens_used=10,
        outcome=SubtaskState.COMPLETED,
        critic_score=critic_score,
        recorded_at_ms=0,
    )


def test_critic_derived_pair_for_same_input(tmp_path: Path) -> None:
    """Two episodes with the same input but different critic scores form a
    natural preference pair: high score is the winner, low score the loser."""
    store = EpisodeStore()
    store.record(
        _ep(subtask_id="s-win", input_text="summarise A", output_text="good", critic_score=0.9)
    )
    store.record(
        _ep(subtask_id="s-lose", input_text="summarise A", output_text="bad", critic_score=0.2)
    )

    builder = DPODatasetBuilder(episode_store=store, min_score_gap=0.3)
    info = builder.build(
        specialty="research-summarize",
        out_path=tmp_path / "pairs.jsonl",
    )

    assert info.pair_count == 1
    pair = json.loads((tmp_path / "pairs.jsonl").read_text(encoding="utf-8").strip())
    assert pair["prompt"] == "summarise A"
    assert pair["chosen"] == "good"
    assert pair["rejected"] == "bad"


def test_score_gap_below_threshold_skipped(tmp_path: Path) -> None:
    store = EpisodeStore()
    store.record(_ep(subtask_id="s-win", input_text="x", output_text="a", critic_score=0.55))
    store.record(_ep(subtask_id="s-lose", input_text="x", output_text="b", critic_score=0.50))

    builder = DPODatasetBuilder(episode_store=store, min_score_gap=0.3)
    info = builder.build(
        specialty="research-summarize",
        out_path=tmp_path / "pairs.jsonl",
    )
    assert info.pair_count == 0


def test_pairs_only_from_same_input(tmp_path: Path) -> None:
    """Different inputs are not comparable — no pair emitted."""
    store = EpisodeStore()
    store.record(_ep(subtask_id="s-1", input_text="A", critic_score=0.9))
    store.record(_ep(subtask_id="s-2", input_text="B", critic_score=0.2))

    builder = DPODatasetBuilder(episode_store=store, min_score_gap=0.3)
    info = builder.build(
        specialty="research-summarize",
        out_path=tmp_path / "pairs.jsonl",
    )
    assert info.pair_count == 0


def test_pairs_only_from_same_specialty(tmp_path: Path) -> None:
    store = EpisodeStore()
    store.record(
        _ep(subtask_id="s-1", input_text="x", critic_score=0.9, specialty="research-summarize")
    )
    store.record(
        _ep(subtask_id="s-2", input_text="x", critic_score=0.2, specialty="code-debug")
    )

    builder = DPODatasetBuilder(episode_store=store, min_score_gap=0.3)
    info = builder.build(
        specialty="research-summarize",
        out_path=tmp_path / "pairs.jsonl",
    )
    assert info.pair_count == 0


def test_dataset_sha256_is_content_addressed(tmp_path: Path) -> None:
    """Two builds over the same store produce byte-identical pairs JSONL
    and therefore the same sha256 — the content-addressed cache key
    pattern from slice 24's TrainingJobBuilder."""
    store = EpisodeStore()
    store.record(_ep(subtask_id="s-1", input_text="x", output_text="a", critic_score=0.9))
    store.record(_ep(subtask_id="s-2", input_text="x", output_text="b", critic_score=0.2))

    builder = DPODatasetBuilder(episode_store=store, min_score_gap=0.3)
    a = builder.build(specialty="research-summarize", out_path=tmp_path / "a.jsonl")
    b = builder.build(specialty="research-summarize", out_path=tmp_path / "b.jsonl")
    assert a.dataset_sha256 == b.dataset_sha256
    assert len(a.dataset_sha256) == 64


def test_failed_episodes_never_pair(tmp_path: Path) -> None:
    """A FAILED episode is not training data, even when its critic score
    contrasts cleanly with a COMPLETED one. Belt-and-braces against the
    same path slice 22's archive_failed_training writes to."""
    store = EpisodeStore()
    store.record(_ep(subtask_id="s-1", input_text="x", critic_score=0.9))
    store.record(
        Episode(
            task_id="t-2",
            subtask_id="s-2",
            worker_id="w1",
            specialty="research-summarize",
            model_version="qwen2.5-7b",
            adapter_version="base",
            input_text="x",
            trajectory=("act",),
            output_text="failed run",
            success=False,
            latency_ms=10,
            tokens_used=10,
            outcome=SubtaskState.FAILED,
            critic_score=0.95,  # high score on a FAILED run is a smell
            recorded_at_ms=0,
        )
    )
    builder = DPODatasetBuilder(episode_store=store, min_score_gap=0.0)
    info = builder.build(
        specialty="research-summarize",
        out_path=tmp_path / "pairs.jsonl",
    )
    assert info.pair_count == 0
