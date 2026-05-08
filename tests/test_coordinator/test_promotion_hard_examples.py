"""Failed training run → hard-examples batch + Discord summary."""

from __future__ import annotations

from turing.coordinator.lifecycle.episode_store import EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.promotion import (
    HardExample,
    archive_failed_training,
)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def notify(self, kind: str, payload: dict) -> None:
        self.calls.append((kind, payload))


def _hard(*, subtask_id: str, error: str = "lr too high") -> HardExample:
    return HardExample(
        subtask_id=subtask_id,
        input_text="please summarise",
        expected_text="a one-line summary",
        actual_output="",
        failure_reason=error,
    )


def test_hard_examples_recorded_to_episode_store() -> None:
    store = EpisodeStore()
    notifier = _RecordingNotifier()
    examples = [_hard(subtask_id="s-1"), _hard(subtask_id="s-2")]

    archive_failed_training(
        run_id="run-1",
        specialty="research-summarize",
        examples=examples,
        episode_store=store,
        notifier=notifier,
    )

    rows = store.query(specialty="research-summarize")
    assert len(rows) == 2
    assert {r.subtask_id for r in rows} == {"s-1", "s-2"}
    # Hard-example rows are tagged FAILED so they don't accidentally
    # become positive training data.
    assert all(r.outcome is SubtaskState.FAILED for r in rows)


def test_discord_summary_posted_with_run_metadata() -> None:
    store = EpisodeStore()
    notifier = _RecordingNotifier()

    archive_failed_training(
        run_id="run-1",
        specialty="research-summarize",
        examples=[_hard(subtask_id="s-1")],
        episode_store=store,
        notifier=notifier,
    )

    summary_calls = [c for c in notifier.calls if c[0] == "training_failed"]
    assert len(summary_calls) == 1
    payload = summary_calls[0][1]
    assert payload["run_id"] == "run-1"
    assert payload["specialty"] == "research-summarize"
    assert payload["example_count"] == 1


def test_empty_example_list_still_notifies() -> None:
    """An empty hard-examples batch is still operator-visible — silent
    failures are exactly what this slice exists to prevent."""
    store = EpisodeStore()
    notifier = _RecordingNotifier()

    archive_failed_training(
        run_id="run-2",
        specialty="research-summarize",
        examples=[],
        episode_store=store,
        notifier=notifier,
    )
    assert any(c[0] == "training_failed" for c in notifier.calls)
