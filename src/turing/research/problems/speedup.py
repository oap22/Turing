"""The speedup family: verifier and adapter.

A speedup problem asks for one thing — make this measurably faster without
changing what it computes — and is graded in a fixed order:

1. **Correctness first.** Every gate command must exit 0, report at least its
   pinned number of passing tests, and (where declared) produce an artifact
   that matches the pinned reference under the problem's tolerance.
2. **Speed second, and only if step 1 passed.** The ordering is deliberate and
   load-bearing twice over. A fast-but-wrong workspace must never produce a
   speedup number, because a number that exists is a number that will
   eventually be quoted without its correctness flag attached. And the fastest
   way to make a benchmark fast is to break it, so timing a workspace that
   failed its gate would spend 24 seconds measuring a lie.

A failed gate scores ``0.0`` on the speedup-ratio scale with
``passed_correctness=False``. Zero rather than "no score" because the scale is
higher-is-better with ``1.0`` meaning "unchanged": a broken workspace is worse
than having done nothing, and the score should say so.

**Verifiers never raise for a runtime condition.** A missing reference file, a
benchmark that will not start, a crashed test runner — all come back as a
result with ``passed_correctness=False`` and :data:`HARNESS_FAILURE_KEY` set in
``raw_measurements``. The loop reads that flag and escalates
(``EscalationReason.HARNESS_FAILURE`` / ``VERIFIER_UNRUNNABLE``) rather than
crashing an unattended round, and the agent — which may not quit — gets an
operator decision instead of a stack trace.

**Loop-2 seam.** Nothing here inspects or edits the scaffold. The
:class:`~turing.research.problems.spec.Loophole` records attached to each spec
are the input loop 2's cheat detector will need; loop 1 only carries them.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from turing.research.contracts import (
    HARNESS_FAILURE_KEY,
    SCORE_SCALE_SPEEDUP,
    Problem,
    ProblemType,
    VerificationResult,
    Verifier,
)
from turing.research.problems.adapter import WorkspaceMaterialisationError
from turing.research.problems.catalog import speedup_specs
from turing.research.problems.process import (
    SubprocessCommandRunner,
    passing_test_counts,
    pythonpath_for_workspace,
    render_argv,
)
from turing.research.problems.timing import SpeedupMeasurement, TimingHarness
from turing.research.problems.tolerance import ComparisonOutcome, compare

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.problems.process import CommandRunner
    from turing.research.problems.spec import SpeedupProblemSpec
    from turing.research.problems.timing import TimingMeasurement

logger = structlog.get_logger("turing.research.problems.speedup")

__all__ = [
    "HARNESS_FAILURE_KEY",
    "SpeedupAdapter",
    "SpeedupVerifier",
]


@dataclass(frozen=True, slots=True)
class _GateOutcome:
    """Internal: what running the correctness gate found."""

    passed: bool
    detail: str
    harness_failure: bool = False
    measurements: dict[str, float] = field(default_factory=dict)


def _stays_inside(candidate: Path, root: Path) -> bool:
    """True when ``candidate`` resolves to something under ``root``.

    Symlinks are followed deliberately: the question is where the bytes come
    from, not what the path looks like.
    """
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except OSError:  # pragma: no cover - resolve() on a broken mount
        return False


async def _read_json(path: Path) -> tuple[Any, str]:
    """Load JSON off the filesystem without blocking the loop.

    Returns ``(payload, "")`` on success or ``(None, reason)`` on failure. A
    caller must distinguish the two by the reason string, not by the payload —
    ``null`` is valid JSON.
    """
    try:
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except OSError as exc:
        return None, f"could not read {path}: {exc}"
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError as exc:
        return None, f"{path} is not valid JSON: {exc}"


@dataclass(frozen=True)
class SpeedupVerifier(Verifier):
    """Grades one speedup problem: correctness gate, then timing, then a ratio.

    Frozen, like every verifier — the agent may never edit, relax or regenerate
    the bar it is graded on, and the whole experiment is void if it can. Note
    what is *not* here: no method that mutates the spec, no way to lower a
    tolerance, no path from a workspace back into the gate definition. The
    verifier reads the workspace and reads the reference root; it writes
    neither.
    """

    spec: SpeedupProblemSpec
    harness_root: Path
    reference_root: Path
    python_executable: str
    runner: CommandRunner
    significance_multiplier: float = 1.0

    async def verify(self, workspace: Path) -> VerificationResult:
        """Grade ``workspace``: correctness first, speed only if it earns it."""
        gate = await self._run_gate(workspace)
        if not gate.passed:
            measurements = dict(gate.measurements)
            measurements["gate_passed"] = 0.0
            if gate.harness_failure:
                measurements[HARNESS_FAILURE_KEY] = 1.0
            logger.info(
                "research.speedup.gate_failed",
                problem_id=self.problem_id,
                harness_failure=gate.harness_failure,
            )
            return self._result(
                score=0.0,
                passed_correctness=False,
                detail=gate.detail,
                measurements=measurements,
            )

        measurement = await self._time(workspace)
        base: dict[str, float] = dict(gate.measurements)
        base["gate_passed"] = 1.0
        base["headroom"] = self.spec.headroom
        if not measurement.ok:
            base.update(measurement.as_measurements("candidate"))
            base[HARNESS_FAILURE_KEY] = 1.0
            detail = (
                "the correctness gate passed but the benchmark did not produce a usable "
                f"measurement ({measurement.runs} run(s), "
                f"{len(measurement.failures)} failure(s)): "
                f"{'; '.join(measurement.failures) or 'no successful runs'}"
            )
            logger.warning(
                "research.speedup.benchmark_unusable",
                problem_id=self.problem_id,
                runs=measurement.runs,
                failures=len(measurement.failures),
            )
            return self._result(
                score=0.0, passed_correctness=False, detail=detail, measurements=base
            )

        speedup = SpeedupMeasurement(
            baseline_seconds=self.spec.timing.baseline_seconds,
            baseline_relative_spread=self.spec.timing.baseline_relative_spread,
            candidate=measurement,
            significance_multiplier=self.significance_multiplier,
        )
        base.update(speedup.as_measurements())
        logger.info(
            "research.speedup.verified",
            problem_id=self.problem_id,
            reported_speedup=round(speedup.reported_speedup, 4),
            within_noise=speedup.within_noise,
        )
        return self._result(
            score=speedup.reported_speedup,
            passed_correctness=True,
            detail=f"{self.spec.gate.description} — {speedup.detail()}",
            measurements=base,
        )

    # -- internals -------------------------------------------------------- #

    def _result(
        self,
        *,
        score: float,
        passed_correctness: bool,
        detail: str,
        measurements: dict[str, float],
    ) -> VerificationResult:
        return VerificationResult(
            problem_id=self.problem_id,
            verifier_id=self.verifier_id,
            score=score,
            passed_correctness=passed_correctness,
            score_scale=self.score_scale,
            raw_measurements=measurements,
            detail=detail,
        )

    def _render(self, argv: tuple[str, ...], workspace: Path) -> tuple[str, ...]:
        return render_argv(
            argv,
            workspace=workspace,
            harness_root=self.harness_root,
            python_executable=self.python_executable,
        )

    def _import_env(self, workspace: Path) -> dict[str, str]:
        """Force harness scripts to import the workspace copy, not this checkout.

        Artifact dumpers are the same class of process as the benchmark: a
        plain ``{python} {harness}/...`` script. Without this, a dumper would
        still emit original-repo artifacts while timing measured the copy.
        """
        return {"PYTHONPATH": pythonpath_for_workspace(workspace)}

    async def _run_gate(self, workspace: Path) -> _GateOutcome:
        measurements: dict[str, float] = {}
        for index, command in enumerate(self.spec.gate.commands):
            result = await self.runner(
                self._render(command.argv, workspace),
                cwd=workspace,
                timeout_seconds=command.timeout_seconds,
                env_overrides=self._import_env(workspace),
            )
            label = command.label or f"command {index}"
            if result.timed_out:
                return _GateOutcome(
                    passed=False,
                    detail=f"correctness gate timed out at {label}: {result.summary()}",
                    measurements=measurements,
                )
            if command.expect_success and not result.succeeded:
                return _GateOutcome(
                    passed=False,
                    detail=f"correctness gate failed at {label}: {result.summary()}\n"
                    f"{result.stderr or result.stdout}",
                    measurements=measurements,
                )
            if command.min_passing_tests is not None:
                found = passing_test_counts(result.stdout, result.stderr)
                if len(found) > 1:
                    return _GateOutcome(
                        passed=False,
                        detail=(
                            f"{label} printed {len(found)} passing-test summaries "
                            f"{found}; the workspace owns stdout, and an extra "
                            "summary is how a conftest atexit spoofs the "
                            "anti-deletion floor. The floor is unchecked, so "
                            "the gate fails"
                        ),
                        measurements=measurements,
                    )
                if not found:
                    return _GateOutcome(
                        passed=False,
                        harness_failure=True,
                        detail=(
                            f"{label} exited 0 but reported no passing-test count; the "
                            "anti-deletion floor cannot be checked, so the run is not "
                            "verified"
                        ),
                        measurements=measurements,
                    )
                observed = found[0]
                measurements[f"gate_{index}_tests_passed"] = float(observed)
                if observed < command.min_passing_tests:
                    return _GateOutcome(
                        passed=False,
                        detail=(
                            f"{label} reported {observed} passing tests, below the pinned "
                            f"floor of {command.min_passing_tests}; tests were removed or "
                            "skipped rather than kept passing"
                        ),
                        measurements=measurements,
                    )

        comparison_outcome = await self._run_comparison(workspace)
        if comparison_outcome is None:
            return _GateOutcome(
                passed=True, detail=self.spec.gate.description, measurements=measurements
            )
        outcome, harness_failure = comparison_outcome
        measurements.update(outcome.as_measurements())
        return _GateOutcome(
            passed=outcome.passed,
            harness_failure=harness_failure,
            detail=f"{outcome.mode.value} comparison: {outcome.detail}",
            measurements=measurements,
        )

    async def _run_comparison(self, workspace: Path) -> tuple[ComparisonOutcome, bool] | None:
        """Run the declared artifact comparison, or ``None`` when there is none.

        The second tuple element flags a *harness* failure — a missing pinned
        reference is the harness being broken, while a missing artifact is the
        workspace failing to produce one, and those escalate differently.
        """
        comparison = self.spec.gate.comparison
        if comparison is None:
            return None
        reference, reference_error = await _read_json(
            self.reference_root / comparison.reference_path
        )
        if reference_error:
            return (
                ComparisonOutcome(
                    passed=False,
                    mode=comparison.tolerance.mode,
                    detail=f"pinned reference unavailable — {reference_error}",
                ),
                True,
            )
        artifact = workspace / comparison.artifact_path
        if not _stays_inside(artifact, workspace):
            # A symlink from the artifact path to the pinned answer would make
            # any workspace compare equal to the reference. Cheap to plant,
            # invisible in the exit codes, and it would silently pass every
            # attempt — so the artifact has to be a real file inside the
            # agent's own workspace.
            return (
                ComparisonOutcome(
                    passed=False,
                    mode=comparison.tolerance.mode,
                    detail=(
                        f"artifact {comparison.artifact_path!r} resolves outside the "
                        "workspace; a comparison artifact must be produced in the "
                        "workspace, not linked to something else"
                    ),
                ),
                False,
            )
        candidate, candidate_error = await _read_json(artifact)
        if candidate_error:
            return (
                ComparisonOutcome(
                    passed=False,
                    mode=comparison.tolerance.mode,
                    detail=f"workspace artifact unusable — {candidate_error}",
                ),
                False,
            )
        return compare(comparison.tolerance, candidate, reference), False

    async def _time(self, workspace: Path) -> TimingMeasurement:
        harness = TimingHarness(runner=self.runner, runs=self.spec.timing.runs)
        return await harness.measure(
            self._render(self.spec.timing.argv, workspace),
            cwd=workspace,
            timeout_seconds=self.spec.timing.timeout_seconds,
            env_overrides=self._import_env(workspace),
        )


@dataclass(frozen=True, slots=True)
class SpeedupAdapter:
    """:class:`~turing.research.problems.adapter.ProblemAdapter` for the speedup family.

    ``harness_root`` holds the benchmark drivers and artifact dumpers;
    ``reference_root`` holds the pinned answers. Both are *outside* the
    attempt workspace, which is the point: a benchmark or a pinned answer the
    graded party can edit grades nothing. This class resolves the paths on the
    correct side of that boundary. Enforcing it — separate user, separate
    mount, separate process — is an OS-level job the brief tracks as Q11 and
    marks a loop-2 blocker.
    """

    harness_root: Path
    reference_root: Path
    specs: tuple[SpeedupProblemSpec, ...] = field(default_factory=speedup_specs)
    python_executable: str = sys.executable
    runner: CommandRunner = field(default_factory=SubprocessCommandRunner)
    significance_multiplier: float = 1.0

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for spec in self.specs:
            if spec.id in seen:
                raise WorkspaceMaterialisationError(f"duplicate problem id {spec.id!r}")
            seen.add(spec.id)
            for label, path in (
                ("harness_root", self.harness_root),
                ("reference_root", self.reference_root),
            ):
                if _stays_inside(path, spec.source_root):
                    raise WorkspaceMaterialisationError(
                        f"{spec.id}: {label} {path} sits inside source_root "
                        f"{spec.source_root}; copying the source would hand the "
                        "agent the harness or the pinned answers"
                    )

    @property
    def problem_type(self) -> ProblemType:
        return ProblemType.SPEEDUP

    def load(self) -> tuple[Problem, ...]:
        """Build one :class:`Problem` per spec, each with its frozen verifier."""
        return tuple(self._build(spec) for spec in self.specs)

    def spec_for(self, problem_id: str) -> SpeedupProblemSpec:
        for spec in self.specs:
            if spec.id == problem_id:
                return spec
        raise WorkspaceMaterialisationError(f"{problem_id!r} is not a speedup problem")

    def eval_set_material(self) -> tuple[str, ...]:
        """Per-spec canonical strings for :func:`fingerprint_corpus`.

        Passing these into the eval-set hash is what makes a quietly-loosened
        tolerance, a re-pinned baseline, or a widened significance band show up
        as a *different eval set* rather than as an improvement.
        ``significance_multiplier`` lives on the adapter, not the spec, and
        still belongs here: it is the band that decides whether a difference
        counts as a speedup.
        """
        return (
            f"significance_multiplier={self.significance_multiplier!r}",
            *(spec.fingerprint_material() for spec in self.specs),
        )

    def harness_identity(self) -> tuple[str, ...]:
        """Canonical strings naming the instrument, for ``RoundConfig.harness_identity``.

        ``python_executable`` is the interpreter every timing command and
        gate runs under. It is not part of the corpus — the same specs, the
        same baselines and the same tolerances hold whichever interpreter
        times them — so it does not belong in :meth:`eval_set_material`; but
        it *is* part of how the measurement was taken, so it belongs in the
        round's ``config_digest``, where a swapped interpreter re-drives a
        stored attempt instead of reusing it.
        """
        return (f"python_executable={self.python_executable}",)

    def missing_harness_scripts(self) -> tuple[Path, ...]:
        """Declared ``{harness}`` paths that do not exist yet.

        The corpus declares the benchmark drivers it needs; writing them means
        running the 21.9 s, 24.5 s and 507 s baselines, which is a separate
        job. This turns "those do not exist yet" into a mechanical pre-round
        check instead of something discovered mid-attempt.
        """
        missing: list[Path] = []
        for spec in self.specs:
            argvs = [spec.timing.argv, *(c.argv for c in spec.gate.commands)]
            for argv in argvs:
                for element in argv:
                    if "{harness}" not in element:
                        continue
                    candidate = self.harness_root / element.replace("{harness}/", "").replace(
                        "{harness}", ""
                    )
                    if not candidate.exists() and candidate not in missing:
                        missing.append(candidate)
        return tuple(missing)

    async def materialise_workspace(self, problem: Problem, destination: Path) -> Path:
        """Copy the problem's source repo into a fresh working directory.

        Copying rather than working in place is what keeps attempts independent
        and leaves the repos the baselines were measured in untouched. ``.git``
        is among the excluded names, so an attempt cannot rewrite history it
        was never given. Turing workspaces also drop the catalog, the brief,
        and the tests that restate the floors — those are how the eval set
        would otherwise leak into the write surface.
        """
        import shutil

        spec = self.spec_for(problem.id)
        source = spec.source_root
        if not source.is_dir():
            raise WorkspaceMaterialisationError(
                f"{problem.id}: source repo {source} does not exist; the speedup corpus "
                "is measured against real repositories on this machine"
            )
        if destination.exists() and any(destination.iterdir()):
            raise WorkspaceMaterialisationError(
                f"{problem.id}: {destination} already has contents; attempts must start "
                "from a fresh workspace or a previous attempt's solution leaks in"
            )
        await asyncio.to_thread(
            shutil.copytree,
            source,
            destination,
            ignore=shutil.ignore_patterns(*spec.workspace_excludes),
            symlinks=True,
            ignore_dangling_symlinks=True,
            dirs_exist_ok=True,
        )
        logger.info(
            "research.speedup.workspace_materialised",
            problem_id=problem.id,
            source=str(source),
            destination=str(destination),
        )
        return destination

    def _build(self, spec: SpeedupProblemSpec) -> Problem:
        verifier = SpeedupVerifier(
            verifier_id=f"{spec.id}.verifier",
            problem_id=spec.id,
            description=spec.gate.description,
            score_scale=SCORE_SCALE_SPEEDUP,
            spec=spec,
            harness_root=self.harness_root,
            reference_root=self.reference_root,
            python_executable=self.python_executable,
            runner=self.runner,
            significance_multiplier=self.significance_multiplier,
        )
        return Problem(
            id=spec.id,
            problem_type=ProblemType.SPEEDUP,
            goal=spec.goal,
            workspace_template=spec.source_root,
            verifier=verifier,
            split=spec.split,
            default_cap=spec.default_cap,
            tags=spec.tags,
            workspace_excludes=spec.workspace_excludes,
        )
