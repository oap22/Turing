"""Subtask lifecycle state machine.

Source of truth for `tasks.<id>.events` projection. Designed so a coordinator
restart can replay the event stream and arrive at exactly the same state —
hence the deterministic `replay` classmethod and idempotent `apply`.

Transition table (PRD lifecycle section):

    PENDING       → DISPATCHED, REJECTED
    DISPATCHED    → RUNNING, FAILED, TIMED_OUT, REJECTED
    RUNNING       → COMPLETED, FAILED, TIMED_OUT, NEEDS_SUBTASK
    NEEDS_SUBTASK → RUNNING, COMPLETED, FAILED

Re-applying the current state is a no-op (idempotency, story 15). Any
transition into a terminal state freezes the subtask — subsequent events
arriving for it are silently dropped, so a spurious re-dispatch after my
crash cannot double-execute completed work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


class InvalidTransitionError(Exception):
    """Raised when an event would move a subtask along a disallowed edge."""


class SubtaskState(Enum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    REJECTED = "REJECTED"
    NEEDS_SUBTASK = "NEEDS_SUBTASK"

    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES

    def is_retryable(self) -> bool:
        return self in _RETRYABLE_STATES


_TERMINAL_STATES = frozenset(
    {
        SubtaskState.COMPLETED,
        SubtaskState.FAILED,
        SubtaskState.TIMED_OUT,
        SubtaskState.REJECTED,
    }
)
_RETRYABLE_STATES = frozenset({SubtaskState.FAILED, SubtaskState.TIMED_OUT})

_ALLOWED: dict[SubtaskState, frozenset[SubtaskState]] = {
    SubtaskState.PENDING: frozenset(
        {SubtaskState.DISPATCHED, SubtaskState.REJECTED}
    ),
    SubtaskState.DISPATCHED: frozenset(
        {
            SubtaskState.RUNNING,
            SubtaskState.FAILED,
            SubtaskState.TIMED_OUT,
            SubtaskState.REJECTED,
        }
    ),
    SubtaskState.RUNNING: frozenset(
        {
            SubtaskState.COMPLETED,
            SubtaskState.FAILED,
            SubtaskState.TIMED_OUT,
            SubtaskState.NEEDS_SUBTASK,
        }
    ),
    SubtaskState.NEEDS_SUBTASK: frozenset(
        {SubtaskState.RUNNING, SubtaskState.COMPLETED, SubtaskState.FAILED}
    ),
}


@dataclass(frozen=True)
class SubtaskEvent:
    subtask_id: str
    new_state: SubtaskState
    ts_ms: int


class TaskLifecycle:
    def __init__(self) -> None:
        self._states: dict[str, SubtaskState] = {}

    @classmethod
    def replay(cls, events: Iterable[SubtaskEvent]) -> TaskLifecycle:
        lifecycle = cls()
        for event in events:
            lifecycle.apply(event)
        return lifecycle

    def state_of(self, subtask_id: str) -> SubtaskState:
        return self._states[subtask_id]

    def apply(self, event: SubtaskEvent) -> None:
        current = self._states.get(event.subtask_id)

        # First sighting — accept any state as the seed.
        if current is None:
            self._states[event.subtask_id] = event.new_state
            return

        # Idempotency: same state arriving again is a no-op.
        if current is event.new_state:
            return

        # Terminal subtasks are frozen — drop spurious events silently rather
        # than reviving a completed unit of work.
        if current.is_terminal():
            return

        if event.new_state not in _ALLOWED.get(current, frozenset()):
            raise InvalidTransitionError(
                f"{current.value} → {event.new_state.value} is not allowed"
            )

        self._states[event.subtask_id] = event.new_state
