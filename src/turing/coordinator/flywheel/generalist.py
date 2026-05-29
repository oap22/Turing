"""Generalist-first adapter strategy (ADR 0009 §3, issue #264).

Phase 0 trains toward **one shared AI/ML-generalist adapter, replicated ×4** —
not four specialists. Early on, curated data is scarce; splitting it four ways
starves each adapter, and one adapter gives 4× throughput plus fault tolerance
(any homogeneous node does any task).

This module is a thin layer over the *existing* machinery: the per-specialty
:class:`~turing.coordinator.adapters.registry.AdapterRegistry` and the
canary→fleet :class:`~turing.coordinator.promotion.rollout.RolloutCoordinator`.
Crucially, it does **not** collapse the per-specialty structures away — the
registry is still keyed by ``(name, version)``, evals still live under
``evals/<specialty>/``, and lessons stay specialty-tagged — so a later split
into AI/ML sub-domain specialists is *config*, not a rewrite. Phase 0 simply
pins the one specialty value :data:`AI_ML_GENERALIST` across all four workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from turing.coordinator.promotion.rollout import LiveEvalReport, RolloutCoordinator

if TYPE_CHECKING:
    from collections.abc import Sequence

    from turing.coordinator.adapters.registry import AdapterRegistry
    from turing.coordinator.promotion.rollout import _Notifier

# The single specialty all four Jetson workers share in Phase 0.
AI_ML_GENERALIST = "ai-ml-generalist"
# The homogeneous fleet size ADR 0009 targets (4× Jetson Orin Nano Super).
GENERALIST_FLEET_SIZE = 4


@dataclass(frozen=True)
class GeneralistFleet:
    """The homogeneous worker fleet sharing one ``ai-ml-generalist`` specialty."""

    workers: tuple[str, ...]
    specialty: str = AI_ML_GENERALIST

    def __post_init__(self) -> None:
        if not self.workers:
            raise ValueError("fleet must have at least one worker")
        if len(set(self.workers)) != len(self.workers):
            raise ValueError("fleet worker ids must be unique")

    @property
    def size(self) -> int:
        return len(self.workers)


def specialty_eval_dir(specialty: str, *, root: Path = Path("evals")) -> Path:
    """Per-specialty eval directory (``evals/<specialty>/``).

    Kept specialty-keyed even though Phase 0 only exercises one specialty — the
    Phase B β-eval gate path is unchanged, so adding a second specialty later
    needs no rework.
    """
    return root / specialty


@dataclass
class GeneralistAdapterRollout:
    """Promotes one generalist adapter and replicates it to the whole fleet.

    Wraps the existing promotion flow: the adapter must already be ``STAGED`` in
    the :class:`AdapterRegistry`; :meth:`replicate` promotes it to ``LIVE``
    (recording the canary score) and returns a
    :class:`RolloutCoordinator` over **all** fleet workers. Feeding a passing
    :class:`LiveEvalReport` for every worker drives the rollout to ``COMPLETED``
    — the adapter is now replicated ×4.
    """

    fleet: GeneralistFleet
    registry: AdapterRegistry
    notifier: _Notifier
    regression_threshold: float = 0.05
    _coordinators: dict[str, RolloutCoordinator] = field(default_factory=dict, init=False)

    def replicate(
        self,
        *,
        name: str,
        version: str,
        baseline_score: float,
        canary_eval_score: float,
    ) -> RolloutCoordinator:
        """Promote the staged adapter to LIVE and start its fleet-wide rollout."""
        # Existing registry gate: STAGED → LIVE, recording the canary score.
        self.registry.promote(name=name, version=version, canary_eval_score=canary_eval_score)
        coordinator = RolloutCoordinator(
            fleet=self.fleet.workers,
            baseline_score=baseline_score,
            candidate_version=version,
            notifier=self.notifier,
            regression_threshold=self.regression_threshold,
        )
        self._coordinators[version] = coordinator
        return coordinator


def replicate_to_fleet(
    coordinator: RolloutCoordinator,
    *,
    fleet: GeneralistFleet,
    version: str,
    score: float,
) -> None:
    """Confirm a passing live-eval on **every** worker, in canary-first order.

    Helper for the homogeneous-fleet case: the canary worker is confirmed
    first (advancing STAGED → FLEET_ROLLOUT), then the rest, leaving the
    coordinator ``COMPLETED`` — i.e. the adapter replicated to all workers.
    """
    ordered: Sequence[str] = (
        coordinator.canary_worker,
        *(w for w in fleet.workers if w != coordinator.canary_worker),
    )
    for worker_id in ordered:
        coordinator.report_live_eval(
            LiveEvalReport(worker_id=worker_id, score=score, version=version)
        )
