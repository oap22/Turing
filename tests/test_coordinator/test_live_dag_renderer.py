"""Snapshot tests for the live-DAG Discord renderer (slice 15/26, #17).

The renderer is pure: it takes a :class:`LiveDAGView` and returns the
message-body string. Discord posting is the caller's job.
"""

from __future__ import annotations

from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.live_dag_renderer import (
    LiveDAGView,
    SubtaskView,
    render_live_dag,
)


def _view(
    *,
    subtasks: tuple[SubtaskView, ...],
    synthesis_state: SubtaskState | None = None,
    synthesis_text: str = "",
    user_prompt: str = "Compare LoRA vs DPO trade-offs.",
) -> LiveDAGView:
    return LiveDAGView(
        task_id="tsk_demo",
        user_prompt=user_prompt,
        subtasks=subtasks,
        synthesis_state=synthesis_state,
        synthesis_text=synthesis_text,
    )


def test_initial_state_all_pending() -> None:
    view = _view(
        subtasks=(
            SubtaskView(id="st_a", title="research-discover", state=SubtaskState.PENDING),
            SubtaskView(id="st_b", title="research-summarize", state=SubtaskState.PENDING),
        ),
    )
    body = render_live_dag(view)

    assert "tsk_demo" in body
    assert "Compare LoRA vs DPO" in body
    # Both subtasks rendered as pending checklist items
    assert body.count("⬜") == 2
    assert "st_a" in body and "st_b" in body
    assert "synthesis pending" in body.lower()


def test_running_subtask_shows_worker_and_adapter() -> None:
    view = _view(
        subtasks=(
            SubtaskView(
                id="st_a",
                title="research-discover",
                state=SubtaskState.RUNNING,
                worker_id="pi-alpha",
                adapter_version="v3",
            ),
            SubtaskView(id="st_b", title="research-summarize", state=SubtaskState.PENDING),
        ),
    )
    body = render_live_dag(view)

    assert "🔄" in body
    assert "pi-alpha" in body
    assert "v3" in body


def test_completed_and_failed_emoji() -> None:
    view = _view(
        subtasks=(
            SubtaskView(id="st_a", title="research-discover", state=SubtaskState.COMPLETED),
            SubtaskView(id="st_b", title="research-summarize", state=SubtaskState.FAILED),
        ),
    )
    body = render_live_dag(view)

    assert "✅" in body
    assert "❌" in body


def test_synthesis_text_streams_at_bottom() -> None:
    view = _view(
        subtasks=(
            SubtaskView(id="st_a", title="research-discover", state=SubtaskState.COMPLETED),
            SubtaskView(id="st_b", title="research-summarize", state=SubtaskState.COMPLETED),
        ),
        synthesis_state=SubtaskState.COMPLETED,
        synthesis_text="LoRA is cheaper to train; DPO yields stronger preference alignment.",
    )
    body = render_live_dag(view)

    assert body.rstrip().endswith(
        "LoRA is cheaper to train; DPO yields stronger preference alignment."
    )
    assert "synthesis pending" not in body.lower()


def test_state_transition_snapshot_is_stable() -> None:
    """Snapshot test across a 2-subtask DAG running in parallel."""
    pending = _view(
        subtasks=(
            SubtaskView(id="st_a", title="research-discover", state=SubtaskState.PENDING),
            SubtaskView(id="st_b", title="research-summarize", state=SubtaskState.PENDING),
        ),
    )
    running = _view(
        subtasks=(
            SubtaskView(
                id="st_a",
                title="research-discover",
                state=SubtaskState.RUNNING,
                worker_id="pi-alpha",
                adapter_version="v3",
            ),
            SubtaskView(
                id="st_b",
                title="research-summarize",
                state=SubtaskState.RUNNING,
                worker_id="pi-bravo",
                adapter_version="v1",
            ),
        ),
    )
    done = _view(
        subtasks=(
            SubtaskView(
                id="st_a",
                title="research-discover",
                state=SubtaskState.COMPLETED,
                worker_id="pi-alpha",
                adapter_version="v3",
            ),
            SubtaskView(
                id="st_b",
                title="research-summarize",
                state=SubtaskState.COMPLETED,
                worker_id="pi-bravo",
                adapter_version="v1",
            ),
        ),
        synthesis_state=SubtaskState.COMPLETED,
        synthesis_text="Final answer.",
    )

    p, r, d = render_live_dag(pending), render_live_dag(running), render_live_dag(done)

    # Each transition strictly grows or replaces emoji; not identical.
    assert p != r != d
    # Headers are stable
    for body in (p, r, d):
        assert body.startswith("**Task**")
        assert "tsk_demo" in body
