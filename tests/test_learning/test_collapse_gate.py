"""Tests for the collapse-aware eval gate (ADR 0009 §4).

Held-out mean improvement is necessary but not sufficient: the gate also
refuses a candidate that collapses output diversity or distribution-tail
coverage, which erode before mean accuracy drops.
"""

from __future__ import annotations

import pytest

from turing.learning.eval_set import (
    CollapseAwareEvalGate,
    DiversityCollapseError,
    EvalRun,
    MeanDeltaTooSmallError,
    TailCollapseError,
    distinct_token_ratio,
)


def test_distinct_token_ratio_bounds() -> None:
    assert distinct_token_ratio([]) == 0.0
    assert distinct_token_ratio(["a b c"]) == pytest.approx(1.0)  # all unique
    assert distinct_token_ratio(["a a a a"]) == pytest.approx(0.25)  # collapsed


def test_passes_when_better_and_diverse() -> None:
    baseline = EvalRun(
        scores=(0.5, 0.6, 0.7),
        outputs=("alpha beta", "gamma delta", "epsilon zeta"),
    )
    candidate = EvalRun(
        scores=(0.7, 0.8, 0.75),
        outputs=("eta theta", "iota kappa", "lambda mu"),
    )
    gate = CollapseAwareEvalGate(min_delta=0.02)

    decision = gate.check(baseline=baseline, candidate=candidate)

    assert decision.mean_delta > 0.02
    assert decision.candidate_diversity >= decision.baseline_diversity - 0.1


def test_refuses_when_mean_delta_too_small() -> None:
    baseline = EvalRun(scores=(0.7,), outputs=("a b c",))
    candidate = EvalRun(scores=(0.705,), outputs=("d e f",))  # +0.005 < 0.02
    gate = CollapseAwareEvalGate(min_delta=0.02)

    with pytest.raises(MeanDeltaTooSmallError):
        gate.check(baseline=baseline, candidate=candidate)


def test_refuses_diversity_collapse_despite_higher_mean() -> None:
    """Candidate scores higher but collapses to one repeated phrasing."""
    baseline = EvalRun(
        scores=(0.5, 0.5, 0.5),
        outputs=("unique alpha", "distinct beta", "varied gamma"),
    )
    candidate = EvalRun(
        scores=(0.9, 0.9, 0.9),  # mean way up...
        outputs=("same same", "same same", "same same"),  # ...but collapsed
    )
    gate = CollapseAwareEvalGate(min_delta=0.02, max_diversity_drop=0.10)

    with pytest.raises(DiversityCollapseError):
        gate.check(baseline=baseline, candidate=candidate)


def test_refuses_tail_collapse_despite_higher_mean() -> None:
    """Candidate's mean rises by nailing the easy cases but zeroes the tail."""
    baseline = EvalRun(
        scores=(0.4, 0.4, 0.4, 0.4),  # mean 0.4, full tail coverage
        outputs=("a b", "c d", "e f", "g h"),
    )
    candidate = EvalRun(
        scores=(1.0, 1.0, 0.0, 0.0),  # mean 0.5 (+0.1) but half the tail lost
        outputs=("i j", "k l", "m n", "o p"),
    )
    gate = CollapseAwareEvalGate(min_delta=0.02, max_tail_drop=0.05)

    with pytest.raises(TailCollapseError):
        gate.check(baseline=baseline, candidate=candidate)


def test_eval_run_validates_shape() -> None:
    with pytest.raises(ValueError):
        EvalRun(scores=(0.5,), outputs=("a", "b"))  # mismatched lengths
    with pytest.raises(ValueError):
        EvalRun(scores=(), outputs=())  # empty
