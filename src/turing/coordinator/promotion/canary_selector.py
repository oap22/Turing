"""CanarySelector — K=1 round-robin canary worker selection per ADR 0007.

Picks the *next* worker after ``last_canary_worker_id`` in stable
lexicographic order. Wraps to the first worker at end. A single-worker
specialty returns that worker idempotently; an unknown last-canary
(decommissioned worker) falls back to the lex-first worker.

Pure function: no state. Persistence of ``last_canary_worker_id`` is
the caller's job (``AdapterRegistry`` per ADR 0007 §1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class EmptyFleetError(Exception):
    """Raised when no workers are advertised for the specialty."""


class CanarySelector:
    def next(
        self,
        *,
        fleet: Sequence[str],
        last_canary_worker_id: str | None,
    ) -> str:
        if not fleet:
            raise EmptyFleetError("specialty has no workers to canary on")

        ordered = sorted(fleet)
        if last_canary_worker_id is None or last_canary_worker_id not in ordered:
            return ordered[0]

        idx = ordered.index(last_canary_worker_id)
        return ordered[(idx + 1) % len(ordered)]
