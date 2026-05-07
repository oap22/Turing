"""WorkerToolGate — defence-in-depth subset check enforced worker-side.

The coordinator's scheduler already filters by `required_tools ⊆ manifest.tools`.
This gate runs the same check on the worker the moment a subtask arrives, so
a coordinator bug or supply-chain compromise cannot trick a research worker
into being told to run shell.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from turing.coordinator.scheduler import Subtask


class ToolGateError(Exception):
    """Raised when a subtask requires a tool the worker doesn't advertise."""


class WorkerToolGate:
    def __init__(self, *, advertised_tools: Iterable[str]) -> None:
        self._advertised = frozenset(advertised_tools)

    def check(self, subtask: Subtask) -> None:
        unadvertised = [t for t in subtask.required_tools if t not in self._advertised]
        if unadvertised:
            raise ToolGateError(
                f"subtask {subtask.subtask_id!r} requires tools "
                f"{unadvertised!r} not in worker manifest"
            )
