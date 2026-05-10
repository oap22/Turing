"""End-to-end dry run: eval-delta gate → stage → 1-worker confirm → fleet."""

from __future__ import annotations

import pytest

from turing.coordinator.promotion import (
    EvalDeltaTooSmallError,
    LiveEvalReport,
    PromotionGate,
    RegressionHaltedError,
    RolloutCoordinator,
    RolloutState,
)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def notify(self, kind: str, payload: dict) -> None:
        self.calls.append((kind, payload))


def test_dry_run_happy_path_advances_through_states() -> None:
    """The acceptance-criterion dry-run: a fake adapter that beats baseline
    by 3% goes through eval-delta → stage → canary confirm → fleet rollout
    → completed without ever halting."""
    gate = PromotionGate(min_delta=0.02)
    notifier = _RecordingNotifier()

    # Step 1: eval-delta gate — candidate beats baseline by enough.
    decision = gate.check_eval_delta(baseline_score=0.70, candidate_score=0.73)
    assert decision.delta == pytest.approx(0.03)

    # Step 2: stage — RolloutCoordinator starts in STAGED.
    rollout = RolloutCoordinator(
        fleet=("w1", "w2", "w3"),
        baseline_score=decision.baseline_score,
        candidate_version="rs@v2",
        notifier=notifier,
    )
    assert rollout.state is RolloutState.STAGED

    # Step 3: canary worker confirms.
    rollout.report_live_eval(LiveEvalReport(worker_id="w1", score=0.73, version="rs@v2"))
    assert rollout.state is RolloutState.FLEET_ROLLOUT

    # Step 4: fleet rollout — every remaining worker confirms.
    for w in ("w2", "w3"):
        rollout.report_live_eval(LiveEvalReport(worker_id=w, score=0.73, version="rs@v2"))
    assert rollout.state is RolloutState.COMPLETED
    # No regression notifications during a clean rollout.
    assert not [c for c in notifier.calls if c[0] == "regression_halted"]


def test_dry_run_eval_delta_below_threshold_never_stages() -> None:
    """A candidate that doesn't beat the held-out gate doesn't even reach
    rollout — eval-delta is the choke point."""
    gate = PromotionGate(min_delta=0.02)

    with pytest.raises(EvalDeltaTooSmallError):
        gate.check_eval_delta(baseline_score=0.70, candidate_score=0.71)
    # No RolloutCoordinator is constructed — the candidate is rejected
    # before any worker hears about it.


def test_dry_run_synthetic_regression_halts_and_notifies() -> None:
    """Acceptance-criterion: Discord notification path tested with a
    synthetic regression."""
    notifier = _RecordingNotifier()
    rollout = RolloutCoordinator(
        fleet=("w1", "w2"),
        baseline_score=0.70,
        candidate_version="rs@v2",
        notifier=notifier,
        regression_threshold=0.05,
    )

    # Synthetic regression: canary returns a score 10 points below baseline.
    with pytest.raises(RegressionHaltedError):
        rollout.report_live_eval(LiveEvalReport(worker_id="w1", score=0.55, version="rs@v2"))

    assert rollout.state is RolloutState.HALTED
    halt_calls = [c for c in notifier.calls if c[0] == "regression_halted"]
    assert len(halt_calls) == 1
    payload = halt_calls[0][1]
    assert payload["worker_id"] == "w1"
    assert payload["candidate_version"] == "rs@v2"
    assert payload["score"] == 0.55
