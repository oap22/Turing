"""Tests for TaskLifecycle and SubtaskState.

The lifecycle is the projection of `tasks.<id>.events`. It must:
- enforce the documented state machine (PRD lifecycle section)
- be idempotent on subtask_id (story 15)
- replay deterministically from an event log (story 14)
"""

from __future__ import annotations

import pytest

from turing.coordinator.lifecycle import (
    InvalidTransitionError,
    SubtaskEvent,
    SubtaskState,
    TaskLifecycle,
)


def _events(*pairs: tuple[str, SubtaskState]) -> list[SubtaskEvent]:
    return [
        SubtaskEvent(subtask_id=sid, new_state=st, ts_ms=i * 1000)
        for i, (sid, st) in enumerate(pairs)
    ]


def test_initial_apply_sets_pending() -> None:
    lifecycle = TaskLifecycle()
    lifecycle.apply(SubtaskEvent("sub-1", SubtaskState.PENDING, ts_ms=0))

    assert lifecycle.state_of("sub-1") is SubtaskState.PENDING


def test_full_happy_path_dispatched_running_completed() -> None:
    lifecycle = TaskLifecycle()
    for ev in _events(
        ("sub-1", SubtaskState.PENDING),
        ("sub-1", SubtaskState.DISPATCHED),
        ("sub-1", SubtaskState.RUNNING),
        ("sub-1", SubtaskState.COMPLETED),
    ):
        lifecycle.apply(ev)

    assert lifecycle.state_of("sub-1") is SubtaskState.COMPLETED


def test_invalid_transition_raises() -> None:
    lifecycle = TaskLifecycle()
    lifecycle.apply(SubtaskEvent("sub-1", SubtaskState.PENDING, ts_ms=0))

    with pytest.raises(InvalidTransitionError):
        lifecycle.apply(SubtaskEvent("sub-1", SubtaskState.COMPLETED, ts_ms=1))


def test_re_applying_same_state_is_idempotent_no_op() -> None:
    lifecycle = TaskLifecycle()
    lifecycle.apply(SubtaskEvent("sub-1", SubtaskState.PENDING, ts_ms=0))
    lifecycle.apply(SubtaskEvent("sub-1", SubtaskState.PENDING, ts_ms=1))

    assert lifecycle.state_of("sub-1") is SubtaskState.PENDING


def test_replay_from_event_log_recovers_terminal_state() -> None:
    events = _events(
        ("sub-1", SubtaskState.PENDING),
        ("sub-1", SubtaskState.DISPATCHED),
        ("sub-1", SubtaskState.RUNNING),
        ("sub-1", SubtaskState.COMPLETED),
        ("sub-2", SubtaskState.PENDING),
        ("sub-2", SubtaskState.DISPATCHED),
        ("sub-2", SubtaskState.RUNNING),
        ("sub-2", SubtaskState.FAILED),
    )

    lifecycle = TaskLifecycle.replay(events)

    assert lifecycle.state_of("sub-1") is SubtaskState.COMPLETED
    assert lifecycle.state_of("sub-2") is SubtaskState.FAILED


@pytest.mark.parametrize(
    "state,is_terminal",
    [
        (SubtaskState.COMPLETED, True),
        (SubtaskState.FAILED, True),
        (SubtaskState.TIMED_OUT, True),
        (SubtaskState.REJECTED, True),
        (SubtaskState.NEEDS_SUBTASK, False),
        (SubtaskState.RUNNING, False),
        (SubtaskState.PENDING, False),
        (SubtaskState.DISPATCHED, False),
    ],
)
def test_terminal_classification(state: SubtaskState, is_terminal: bool) -> None:
    assert state.is_terminal() is is_terminal


@pytest.mark.parametrize(
    "state,retryable",
    [
        (SubtaskState.FAILED, True),
        (SubtaskState.TIMED_OUT, True),
        (SubtaskState.REJECTED, False),
        (SubtaskState.COMPLETED, False),
        (SubtaskState.NEEDS_SUBTASK, False),
    ],
)
def test_retry_eligibility(state: SubtaskState, retryable: bool) -> None:
    assert state.is_retryable() is retryable


def test_dispatch_after_completion_is_idempotent_no_op() -> None:
    """Story 15: re-dispatch after my crash must not double-execute."""
    lifecycle = TaskLifecycle()
    for ev in _events(
        ("sub-1", SubtaskState.PENDING),
        ("sub-1", SubtaskState.DISPATCHED),
        ("sub-1", SubtaskState.RUNNING),
        ("sub-1", SubtaskState.COMPLETED),
    ):
        lifecycle.apply(ev)

    # A spurious re-dispatch arrives after recovery.
    lifecycle.apply(SubtaskEvent("sub-1", SubtaskState.DISPATCHED, ts_ms=99))

    assert lifecycle.state_of("sub-1") is SubtaskState.COMPLETED


def test_running_can_request_extension_via_needs_subtask() -> None:
    lifecycle = TaskLifecycle()
    for ev in _events(
        ("sub-1", SubtaskState.PENDING),
        ("sub-1", SubtaskState.DISPATCHED),
        ("sub-1", SubtaskState.RUNNING),
        ("sub-1", SubtaskState.NEEDS_SUBTASK),
    ):
        lifecycle.apply(ev)

    assert lifecycle.state_of("sub-1") is SubtaskState.NEEDS_SUBTASK
