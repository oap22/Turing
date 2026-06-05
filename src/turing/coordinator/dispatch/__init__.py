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
from .grounding_fragment import (
    GROUNDING_FRAGMENT_KEY,
    grounding_to_fragment,
    merge_fragments,
    reasoning_from_result,
)

__all__ = [
    "ENVELOPE_VERSION",
    "GROUNDING_FRAGMENT_KEY",
    "SourceInput",
    "SubtaskDispatch",
    "SubtaskDispatchClient",
    "SubtaskKind",
    "SubtaskTimeoutError",
    "TaskResult",
    "grounding_to_fragment",
    "merge_fragments",
    "reasoning_from_result",
]
