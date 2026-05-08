"""K-worker rollout coordinator with halt-on-regression.

A staged adapter goes to one canary worker first. The canary re-runs its
eval at its actual quantization (story 39); if confirmed, fan out to the
rest of the fleet. Any worker reporting a live-eval regression vs baseline
halts the rollout and pushes a notification through the operator's Discord
path.

The coordinator is a pure state machine plus a notifier. Real-worker
dispatch (telling a worker to actually load the adapter and re-run eval)
lives in the runtime-bus integration; this module owns the *decision*
of when to advance + when to halt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class RolloutState(str, Enum):  # noqa: UP042
    """Use ``(str, Enum)`` to match the rest of coordinator/* (training_approval,
    budget, etc.); ``StrEnum`` is a project-wide refactor for a later slice."""

    STAGED = "staged"
    FLEET_ROLLOUT = "fleet_rollout"
    COMPLETED = "completed"
    HALTED = "halted"


class RegressionHaltedError(Exception):
    """Raised when a worker's live eval falls below baseline minus threshold."""


@dataclass(frozen=True)
class LiveEvalReport:
    worker_id: str
    score: float
    version: str


class _Notifier(Protocol):
    def notify(self, kind: str, payload: dict) -> None: ...


class RolloutCoordinator:
    def __init__(
        self,
        *,
        fleet: Sequence[str],
        baseline_score: float,
        candidate_version: str,
        notifier: _Notifier,
        regression_threshold: float = 0.05,
    ) -> None:
        if not fleet:
            raise ValueError("fleet must be non-empty")
        self._fleet = tuple(fleet)
        self._baseline = baseline_score
        self._version = candidate_version
        self._notifier = notifier
        self._threshold = regression_threshold
        self._state = RolloutState.STAGED
        self._confirmed: set[str] = set()

    @property
    def state(self) -> RolloutState:
        return self._state

    @property
    def canary_worker(self) -> str:
        return self._fleet[0]

    def pending_workers(self) -> tuple[str, ...]:
        return tuple(w for w in self._fleet if w not in self._confirmed)

    def report_live_eval(self, report: LiveEvalReport) -> None:
        # Stale-version reports must not halt a fresh rollout.
        if report.version != self._version:
            return
        if report.worker_id not in self._fleet:
            return
        if self._state in (RolloutState.HALTED, RolloutState.COMPLETED):
            return

        # Regression check applies to every worker, canary or fleet.
        if report.score < self._baseline - self._threshold:
            self._state = RolloutState.HALTED
            self._notifier.notify(
                "regression_halted",
                {
                    "worker_id": report.worker_id,
                    "candidate_version": report.version,
                    "score": report.score,
                    "baseline_score": self._baseline,
                    "threshold": self._threshold,
                },
            )
            raise RegressionHaltedError(
                f"worker {report.worker_id} regressed: "
                f"{report.score:.4f} < baseline {self._baseline:.4f} - "
                f"{self._threshold:.4f}"
            )

        self._confirmed.add(report.worker_id)
        if (
            self._state is RolloutState.STAGED
            and report.worker_id == self.canary_worker
        ):
            self._state = RolloutState.FLEET_ROLLOUT
        if self._confirmed >= set(self._fleet):
            self._state = RolloutState.COMPLETED
