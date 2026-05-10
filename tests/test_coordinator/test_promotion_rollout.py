"""K-worker rollout: stage → 1-worker confirm → fleet, halt on regression."""

from __future__ import annotations

import pytest

from turing.coordinator.promotion import (
    LiveEvalReport,
    RegressionHaltedError,
    RolloutCoordinator,
    RolloutState,
)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def notify(self, kind: str, payload: dict) -> None:
        self.calls.append((kind, payload))


def test_initial_state_is_staged() -> None:
    coord = RolloutCoordinator(
        fleet=("w1", "w2", "w3"),
        baseline_score=0.70,
        candidate_version="rs@v2",
        notifier=_RecordingNotifier(),
    )
    assert coord.state is RolloutState.STAGED
    # Picks the first worker as the canary.
    assert coord.canary_worker == "w1"


def test_canary_confirmation_advances_to_fleet_rollout() -> None:
    coord = RolloutCoordinator(
        fleet=("w1", "w2", "w3"),
        baseline_score=0.70,
        candidate_version="rs@v2",
        notifier=_RecordingNotifier(),
        regression_threshold=0.05,
    )
    coord.report_live_eval(LiveEvalReport(worker_id="w1", score=0.73, version="rs@v2"))
    assert coord.state is RolloutState.FLEET_ROLLOUT
    # Remaining workers are queued.
    assert set(coord.pending_workers()) == {"w2", "w3"}


def test_canary_regression_halts_rollout_and_notifies() -> None:
    notifier = _RecordingNotifier()
    coord = RolloutCoordinator(
        fleet=("w1", "w2", "w3"),
        baseline_score=0.70,
        candidate_version="rs@v2",
        notifier=notifier,
        regression_threshold=0.05,
    )
    # Canary scores 5 points below baseline → regression.
    with pytest.raises(RegressionHaltedError):
        coord.report_live_eval(LiveEvalReport(worker_id="w1", score=0.60, version="rs@v2"))
    assert coord.state is RolloutState.HALTED
    # Operator notified through the configured Discord path.
    assert any(kind == "regression_halted" for kind, _ in notifier.calls)


def test_fleet_worker_regression_mid_rollout_halts() -> None:
    notifier = _RecordingNotifier()
    coord = RolloutCoordinator(
        fleet=("w1", "w2", "w3"),
        baseline_score=0.70,
        candidate_version="rs@v2",
        notifier=notifier,
        regression_threshold=0.05,
    )
    # Canary OK
    coord.report_live_eval(LiveEvalReport(worker_id="w1", score=0.73, version="rs@v2"))
    # w2 drops a regression mid-rollout
    with pytest.raises(RegressionHaltedError):
        coord.report_live_eval(LiveEvalReport(worker_id="w2", score=0.60, version="rs@v2"))
    assert coord.state is RolloutState.HALTED


def test_all_workers_confirmed_completes_rollout() -> None:
    coord = RolloutCoordinator(
        fleet=("w1", "w2", "w3"),
        baseline_score=0.70,
        candidate_version="rs@v2",
        notifier=_RecordingNotifier(),
    )
    for w in ("w1", "w2", "w3"):
        coord.report_live_eval(LiveEvalReport(worker_id=w, score=0.73, version="rs@v2"))
    assert coord.state is RolloutState.COMPLETED
    assert coord.pending_workers() == ()


def test_report_for_wrong_version_ignored() -> None:
    """If a worker reports an eval for a different adapter version, that's
    not the candidate we're rolling out — silently ignore so a stale worker
    can't accidentally halt a fresh rollout."""
    coord = RolloutCoordinator(
        fleet=("w1", "w2"),
        baseline_score=0.70,
        candidate_version="rs@v2",
        notifier=_RecordingNotifier(),
        regression_threshold=0.05,
    )
    # No exception, no state change.
    coord.report_live_eval(LiveEvalReport(worker_id="w1", score=0.10, version="rs@v1"))
    assert coord.state is RolloutState.STAGED


def test_unknown_worker_report_ignored() -> None:
    coord = RolloutCoordinator(
        fleet=("w1", "w2"),
        baseline_score=0.70,
        candidate_version="rs@v2",
        notifier=_RecordingNotifier(),
        regression_threshold=0.05,
    )
    coord.report_live_eval(LiveEvalReport(worker_id="w-unknown", score=0.10, version="rs@v2"))
    assert coord.state is RolloutState.STAGED
