"""CanaryPassGate — ε-tolerant non-regression check per ADR 0007 §4.

Pass: ``canary_score >= prior_live_canary_score - epsilon_pp``. The
offline gate already verified ≥2pp improvement at trainer precision;
quantization typically costs 0-2pp. Demanding the full delta to survive
at quantization double-counts the bar; demanding strict non-regression
falsely halts adapters whose "no real change" looks like sample noise
on small eval sets.

ε = "the offline improvement survived quantization within sampling
noise." Default 0.5pp; configurable via ``TURING_CANARY_EPSILON_PP``
at the wiring site.

First-ever promotion has no incumbent to compare against; any
successful score passes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CanaryPassResult:
    passed: bool
    delta_pp: float | None  # None when there's no prior incumbent
    epsilon_pp: float


class CanaryPassGate:
    def __init__(self, *, epsilon_pp: float = 0.5) -> None:
        self._epsilon_pp = epsilon_pp

    def evaluate(
        self,
        *,
        canary_score: float,
        prior_live_canary_score: float | None,
    ) -> CanaryPassResult:
        if prior_live_canary_score is None:
            return CanaryPassResult(passed=True, delta_pp=None, epsilon_pp=self._epsilon_pp)
        delta_pp = canary_score - prior_live_canary_score
        passed = delta_pp >= -self._epsilon_pp
        return CanaryPassResult(passed=passed, delta_pp=delta_pp, epsilon_pp=self._epsilon_pp)
