"""The timing harness — repeat, report spread, refuse noise.

A speedup problem's score is a ratio of two wall-clock measurements, and a
wall-clock measurement taken once is not a measurement. Everything here exists
to make one rule mechanical:

    **A solution whose speedup is within run-to-run noise is not a speedup.**

The rule matters more here than in ordinary benchmarking. The corpus is scored
continuously and the scores feed a marginal-gain-versus-noise-floor comparison
at the round level; if a 1.01× thermal fluctuation is allowed to enter as a
score, the noise floor is measuring the benchmark's jitter rather than the
scaffold's variance, and driving function #2 stops meaning anything.

How the refusal works:

1. Each candidate is timed at least twice (:class:`TimingHarness` refuses to be
   constructed with fewer runs — one run reports no spread, so there is nothing
   to compare a difference against).
2. The candidate's relative spread ``(max - min) / median`` is combined with
   the *baseline's* pinned relative spread in quadrature, the standard
   first-order propagation for a quotient.
3. A floor of :data:`MINIMUM_NOISE_BAND` is applied, because two runs that
   happen to agree to five decimal places do not license a claim that a 0.2%
   difference is real on a laptop with turbo, thermal throttling and a
   background OS.
4. If ``|speedup - 1|`` does not clear that band, :attr:`SpeedupMeasurement.
   reported_speedup` is exactly ``1.0`` — "no measured change" — and the raw
   ratio is kept alongside it for the record rather than discarded.

Regressions are treated symmetrically: a *significant* slowdown is reported as
a speedup below 1.0, because "the solution made it worse" is a real result. An
insignificant one is also flattened to 1.0.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from turing.research.contracts import ContractViolationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from turing.research.problems.process import CommandRunner

logger = structlog.get_logger("turing.research.problems.timing")

__all__ = [
    "MINIMUM_NOISE_BAND",
    "SpeedupMeasurement",
    "TimingHarness",
    "TimingMeasurement",
    "relative_spread",
]

#: Floor on the combined relative noise band, as a fraction. No wall-clock
#: comparison on a thermally managed laptop is trustworthy below half a
#: percent, however tight two particular runs happened to land.
MINIMUM_NOISE_BAND = 0.005

#: Fewest runs a timing measurement may consist of. One run has no spread.
MINIMUM_RUNS = 2


def relative_spread(samples: Sequence[float]) -> float:
    """``(max - min) / median`` — the spread figure the brief reports as a %.

    Returns 0.0 for a single sample: no spread is *observable*, which is not
    the same as no spread existing, and is exactly why
    :class:`TimingHarness` will not run one.
    """
    if not samples:
        raise ContractViolationError("cannot take the spread of no samples")
    if len(samples) == 1:
        return 0.0
    median = statistics.median(samples)
    if median <= 0:
        raise ContractViolationError(f"non-positive median duration {median!r}")
    return (max(samples) - min(samples)) / median


@dataclass(frozen=True, slots=True)
class TimingMeasurement:
    """Repeated wall-clock timings of one command.

    ``failures`` is non-empty when a run did not exit cleanly. A measurement
    with any failed run is not usable — the successful runs are not a sample of
    the same thing the failed one was doing — so :attr:`ok` requires both
    enough samples and no failures.
    """

    samples: tuple[float, ...]
    failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(s < 0 for s in self.samples):
            raise ContractViolationError("a duration cannot be negative")
        object.__setattr__(self, "samples", tuple(self.samples))
        object.__setattr__(self, "failures", tuple(self.failures))

    @property
    def runs(self) -> int:
        return len(self.samples)

    @property
    def ok(self) -> bool:
        """True when this measurement may be used to compute a ratio."""
        return not self.failures and self.runs >= MINIMUM_RUNS and self.median > 0

    @property
    def best(self) -> float:
        return min(self.samples)

    @property
    def worst(self) -> float:
        return max(self.samples)

    @property
    def median(self) -> float:
        """The statistic used for the ratio.

        Median rather than best-of-N: best-of-N is biased low and rewards a
        solution that is fast only when the machine is quiet, which is
        precisely the difference the noise rule is trying to exclude.
        """
        if not self.samples:
            return 0.0
        return statistics.median(self.samples)

    @property
    def relative_spread(self) -> float:
        if not self.samples:
            return 0.0
        return relative_spread(self.samples)

    def as_measurements(self, prefix: str) -> dict[str, float]:
        """Flatten into the float-only mapping ``VerificationResult`` accepts."""
        if not self.samples:
            return {f"{prefix}_runs": 0.0}
        return {
            f"{prefix}_runs": float(self.runs),
            f"{prefix}_median_seconds": self.median,
            f"{prefix}_best_seconds": self.best,
            f"{prefix}_worst_seconds": self.worst,
            f"{prefix}_relative_spread": self.relative_spread,
        }


@dataclass(frozen=True, slots=True)
class TimingHarness:
    """Times a command repeatedly and reports the spread.

    Refuses fewer than :data:`MINIMUM_RUNS` runs at construction rather than at
    use, so a problem definition asking for a single run fails when the corpus
    is loaded — before a round starts — instead of producing an
    unfalsifiable number hours later.
    """

    runner: CommandRunner
    runs: int = MINIMUM_RUNS
    warmup_runs: int = 0

    def __post_init__(self) -> None:
        if self.runs < MINIMUM_RUNS:
            raise ContractViolationError(
                f"a timing measurement needs at least {MINIMUM_RUNS} runs to report a "
                f"spread; got {self.runs}. A speedup that cannot be distinguished from "
                "run-to-run noise is not a speedup."
            )
        if self.warmup_runs < 0:
            raise ContractViolationError("warmup_runs cannot be negative")

    async def measure(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env_overrides: Mapping[str, str] | None = None,
    ) -> TimingMeasurement:
        """Run ``argv`` ``runs`` times and collect the durations.

        Warm-up runs are executed and discarded first when configured. A
        warm-up failure counts as a failure: the command is broken either way.
        """
        samples: list[float] = []
        failures: list[str] = []
        for index in range(self.warmup_runs + self.runs):
            result = await self.runner(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env_overrides=env_overrides,
            )
            if not result.succeeded:
                failures.append(f"run {index}: {result.summary()}")
                continue
            if index >= self.warmup_runs:
                samples.append(result.seconds)
        measurement = TimingMeasurement(samples=tuple(samples), failures=tuple(failures))
        logger.debug(
            "research.timing.measured",
            argv=tuple(argv),
            runs=measurement.runs,
            median=round(measurement.median, 4),
            relative_spread=round(measurement.relative_spread, 5),
            failures=len(measurement.failures),
        )
        return measurement


@dataclass(frozen=True, slots=True)
class SpeedupMeasurement:
    """A candidate timing compared against a pinned baseline, noise-gated.

    ``baseline_seconds`` and ``baseline_relative_spread`` come from the problem
    definition, not from a run: the baselines were measured once on the machine
    the corpus was built for and are frozen with everything else about the
    problem. Re-deriving them per attempt would make the score depend on how
    busy the laptop was when the attempt started.
    """

    baseline_seconds: float
    baseline_relative_spread: float
    candidate: TimingMeasurement
    significance_multiplier: float = 1.0
    minimum_noise_band: float = MINIMUM_NOISE_BAND

    def __post_init__(self) -> None:
        if self.baseline_seconds <= 0:
            raise ContractViolationError("baseline duration must be positive")
        if self.baseline_relative_spread < 0:
            raise ContractViolationError("baseline spread cannot be negative")
        if self.significance_multiplier <= 0:
            raise ContractViolationError("significance multiplier must be positive")
        if not self.candidate.ok:
            raise ContractViolationError(
                "cannot compute a speedup from an unusable timing measurement; "
                f"runs={self.candidate.runs} failures={len(self.candidate.failures)}"
            )

    @property
    def raw_speedup(self) -> float:
        """The unfiltered ratio. Kept for the record, never used as the score."""
        return self.baseline_seconds / self.candidate.median

    @property
    def noise_band(self) -> float:
        """Combined relative uncertainty on the ratio, floored and scaled.

        First-order propagation for a quotient: relative uncertainties add in
        quadrature. Near a ratio of 1 the absolute and relative bands coincide,
        which is the only region where the comparison is close, so the band is
        compared directly against ``|speedup - 1|``.
        """
        propagated = math.hypot(self.baseline_relative_spread, self.candidate.relative_spread)
        return max(propagated, self.minimum_noise_band) * self.significance_multiplier

    @property
    def within_noise(self) -> bool:
        """True when the measured difference is indistinguishable from jitter."""
        return abs(self.raw_speedup - 1.0) <= self.noise_band

    @property
    def reported_speedup(self) -> float:
        """The score. Exactly ``1.0`` when the difference is within noise.

        Flattening rather than reporting the raw ratio is the whole point: a
        number that survives into a round record is a claim, and a claim
        smaller than the instrument's resolution should not be made. A
        *significant* regression is reported honestly as a ratio below 1.
        """
        if self.within_noise:
            return 1.0
        return self.raw_speedup

    def as_measurements(self) -> dict[str, float]:
        """Everything a reader needs to re-check this judgement."""
        data = {
            "baseline_seconds": self.baseline_seconds,
            "baseline_relative_spread": self.baseline_relative_spread,
            "raw_speedup": self.raw_speedup,
            "reported_speedup": self.reported_speedup,
            "noise_band": self.noise_band,
            "within_noise": 1.0 if self.within_noise else 0.0,
        }
        data.update(self.candidate.as_measurements("candidate"))
        return data

    def detail(self) -> str:
        """One human-readable sentence about the verdict."""
        if self.within_noise:
            return (
                f"raw {self.raw_speedup:.4f}x is within the {self.noise_band:.4f} "
                f"noise band (baseline spread {self.baseline_relative_spread:.4f}, "
                f"candidate spread {self.candidate.relative_spread:.4f}); "
                "reported as 1.0000x — no measured change"
            )
        direction = "speedup" if self.raw_speedup > 1 else "regression"
        return (
            f"{self.raw_speedup:.4f}x {direction} over {self.candidate.runs} runs "
            f"(median {self.candidate.median:.4f}s vs baseline "
            f"{self.baseline_seconds:.4f}s), clears the {self.noise_band:.4f} noise band"
        )
