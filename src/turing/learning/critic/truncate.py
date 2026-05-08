"""Episode truncation for the dispatch payload (ADR 0004 §3).

The canonical ``EpisodeStore`` keeps full untruncated trajectories — the
trainer corpus needs them. The judge worker only needs *behavior shape*,
so we head/tail-truncate per-field at dispatch time only.

Budgets (per ADR 0004 §3):
- Each trajectory step: 2KB head + 2KB tail.
- Final output: 8KB head + 8KB tail.

A short marker between head and tail makes the truncated region visible
to the critic prompt — "this was the start, this was the end" — without
confusing them with contiguous content.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turing.coordinator.lifecycle.episode_store import Episode

STEP_HEAD_TAIL_BYTES = 2048
OUTPUT_HEAD_TAIL_BYTES = 8192
TRUNCATION_MARKER = "\n…[TRUNCATED]…\n"


def _truncate_field(text: str, *, head_tail: int) -> str:
    """Return ``text`` if short enough; otherwise head + marker + tail."""
    if len(text) <= 2 * head_tail + len(TRUNCATION_MARKER):
        return text
    return text[:head_tail] + TRUNCATION_MARKER + text[-head_tail:]


def truncate_episode(episode: Episode) -> Episode:
    """Return a copy of ``episode`` with trajectory + output truncated.

    Pure: input episode is not mutated. Suitable for use at the dispatch
    boundary; the canonical ``EpisodeStore`` row stays untouched.
    """
    truncated_trajectory = tuple(
        _truncate_field(step, head_tail=STEP_HEAD_TAIL_BYTES)
        for step in episode.trajectory
    )
    truncated_output = _truncate_field(
        episode.output_text, head_tail=OUTPUT_HEAD_TAIL_BYTES
    )
    return replace(
        episode,
        trajectory=truncated_trajectory,
        output_text=truncated_output,
    )
