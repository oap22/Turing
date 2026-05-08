"""Subtask dispatch — coordinator-side client for the worker pool.

See `docs/adr/0002-subtask-dispatch.md` for the wire contract.
"""

from __future__ import annotations

from .client import SubtaskDispatchClient, SubtaskTimeoutError
from .envelopes import (
    ENVELOPE_VERSION,
    SourceInput,
    SubtaskDispatch,
    SubtaskKind,
    TaskResult,
)

__all__ = [
    "ENVELOPE_VERSION",
    "SourceInput",
    "SubtaskDispatch",
    "SubtaskDispatchClient",
    "SubtaskKind",
    "SubtaskTimeoutError",
    "TaskResult",
]
