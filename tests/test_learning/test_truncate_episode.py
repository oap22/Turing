"""truncate_episode — head/tail truncation at dispatch time per ADR 0004 §3.

The canonical EpisodeStore stays untouched; truncation is applied only when
preparing the dispatch payload sent to the judge worker. Each trajectory
step keeps 2KB head + 2KB tail; final output keeps 8KB head + 8KB tail.
A clear marker delimits truncated regions so the judge prompt can read
"this was the start, this was the end" without confusing them with
contiguous content.
"""

from __future__ import annotations

from turing.coordinator.lifecycle.episode_store import Episode
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.learning.critic.truncate import (
    OUTPUT_HEAD_TAIL_BYTES,
    STEP_HEAD_TAIL_BYTES,
    TRUNCATION_MARKER,
    truncate_episode,
)


def _episode(
    *,
    trajectory: tuple[str, ...],
    output_text: str = "out",
) -> Episode:
    return Episode(
        task_id="t1",
        subtask_id="st1",
        worker_id="w1",
        specialty="research-summarize",
        model_version="qwen2.5:14b",
        adapter_version="v1",
        input_text="prompt",
        trajectory=trajectory,
        output_text=output_text,
        success=True,
        latency_ms=100,
        tokens_used=10,
        outcome=SubtaskState.COMPLETED,
        critic_score=0.0,
        recorded_at_ms=1000,
    )


def test_short_episode_passes_through_unmodified() -> None:
    ep = _episode(trajectory=("step a", "step b"), output_text="brief output")
    truncated = truncate_episode(ep)
    assert truncated.trajectory == ("step a", "step b")
    assert truncated.output_text == "brief output"
    assert TRUNCATION_MARKER not in "".join(truncated.trajectory)
    assert TRUNCATION_MARKER not in truncated.output_text


def test_long_trajectory_step_is_head_tail_truncated() -> None:
    huge_step = "X" * 50_000  # 50KB single step
    ep = _episode(trajectory=(huge_step,))
    truncated = truncate_episode(ep)
    only_step = truncated.trajectory[0]

    # Truncated to roughly 2 * STEP_HEAD_TAIL_BYTES + marker length.
    expected_max = 2 * STEP_HEAD_TAIL_BYTES + len(TRUNCATION_MARKER) + 100
    assert len(only_step) < expected_max
    # Head and tail of original survive.
    assert only_step.startswith("X" * STEP_HEAD_TAIL_BYTES)
    assert only_step.endswith("X" * STEP_HEAD_TAIL_BYTES)
    # Marker appears once between head and tail.
    assert only_step.count(TRUNCATION_MARKER) == 1


def test_long_output_text_is_head_tail_truncated() -> None:
    huge_output = "Y" * 50_000  # 50KB
    ep = _episode(trajectory=("ok",), output_text=huge_output)
    truncated = truncate_episode(ep)

    expected_max = 2 * OUTPUT_HEAD_TAIL_BYTES + len(TRUNCATION_MARKER) + 100
    assert len(truncated.output_text) < expected_max
    assert truncated.output_text.startswith("Y" * OUTPUT_HEAD_TAIL_BYTES)
    assert truncated.output_text.endswith("Y" * OUTPUT_HEAD_TAIL_BYTES)
    assert truncated.output_text.count(TRUNCATION_MARKER) == 1


def test_canonical_episode_is_not_mutated() -> None:
    """ADR 0004 §3: truncation is at dispatch time only; canonical store untouched."""
    huge = "Z" * 50_000
    ep = _episode(trajectory=(huge,), output_text=huge)
    truncate_episode(ep)
    # Original episode untouched.
    assert ep.trajectory == (huge,)
    assert ep.output_text == huge


def test_each_step_truncated_independently() -> None:
    """A multi-step trajectory truncates each step on its own budget."""
    s1 = "A" * 50_000
    s2 = "B" * 50_000
    ep = _episode(trajectory=(s1, s2))
    truncated = truncate_episode(ep)
    assert len(truncated.trajectory) == 2
    assert truncated.trajectory[0].startswith("A" * STEP_HEAD_TAIL_BYTES)
    assert truncated.trajectory[1].startswith("B" * STEP_HEAD_TAIL_BYTES)
