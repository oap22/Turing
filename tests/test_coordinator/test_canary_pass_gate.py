"""CanaryPassGate — ε-tolerant non-regression check per ADR 0007 §4.

Pass: ``canary_score >= prior_live_canary_score - epsilon_pp``.
First-ever promotion (no prior incumbent) passes any successful score.
ε defaults to 0.5pp; configurable.
"""

from __future__ import annotations

import pytest

from turing.coordinator.promotion.canary_pass_gate import CanaryPassGate


def test_pass_when_within_epsilon_below_prior() -> None:
    """ε=0.5pp tolerance: a score 0.3pp below prior still passes."""
    gate = CanaryPassGate(epsilon_pp=0.5)
    result = gate.evaluate(canary_score=69.7, prior_live_canary_score=70.0)
    assert result.passed is True
    assert result.delta_pp == pytest.approx(-0.3)


def test_fail_when_below_epsilon_below_prior() -> None:
    """A score 0.6pp below prior (beyond ε=0.5pp) fails."""
    gate = CanaryPassGate(epsilon_pp=0.5)
    result = gate.evaluate(canary_score=69.4, prior_live_canary_score=70.0)
    assert result.passed is False
    assert result.delta_pp == pytest.approx(-0.6)


def test_pass_when_strictly_above_prior() -> None:
    gate = CanaryPassGate(epsilon_pp=0.5)
    result = gate.evaluate(canary_score=72.5, prior_live_canary_score=70.0)
    assert result.passed is True
    assert result.delta_pp == pytest.approx(2.5)


def test_first_ever_promotion_passes_with_no_prior() -> None:
    """First promotion in a specialty has no incumbent to compare against
    — any non-None score passes per ADR 0007 §4."""
    gate = CanaryPassGate(epsilon_pp=0.5)
    result = gate.evaluate(canary_score=68.0, prior_live_canary_score=None)
    assert result.passed is True
    assert result.delta_pp is None


def test_epsilon_is_configurable() -> None:
    """A tighter ε rejects what a looser ε would accept."""
    tight = CanaryPassGate(epsilon_pp=0.1)
    loose = CanaryPassGate(epsilon_pp=1.0)
    # Score 0.5pp below prior.
    assert tight.evaluate(canary_score=69.5, prior_live_canary_score=70.0).passed is False
    assert loose.evaluate(canary_score=69.5, prior_live_canary_score=70.0).passed is True
