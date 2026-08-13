"""Declarative problem definitions.

A speedup problem is data, not code: a baseline command with a measured
baseline duration and observed spread, a correctness gate, the tolerance
semantics that gate uses, and the loopholes known to exist in it. Keeping it
declarative is what lets the corpus be hashed into ``eval_set_hash`` and
compared across rounds — a problem whose definition is a Python function is a
problem whose definition can drift without anybody noticing.

Everything in this module is frozen and validated at construction. A malformed
problem definition fails when the corpus is loaded, before a round starts,
rather than three hours into an unattended attempt.

**Provenance is part of the definition.** ``baseline_seconds`` was measured on
a specific machine on a specific date, and ``spread_provenance`` records
whether the spread beside it was measured too or assumed. The brief asks for
estimate-vs-actual logging from the first run precisely because none of these
priors come from experience yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from turing.research.contracts import ContractViolationError, Split
from turing.research.problems.timing import MINIMUM_RUNS, relative_spread

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import Cap
    from turing.research.problems.tolerance import Tolerance

__all__ = [
    "DEFAULT_WORKSPACE_EXCLUDES",
    "CorrectnessGate",
    "GateCommand",
    "Loophole",
    "LoopholeRuling",
    "OutputComparison",
    "SpeedupProblemSpec",
    "SpreadProvenance",
    "TimingSpec",
    "argv_invokes_harness",
    "argv_is_workspace_package_script",
]

#: Package-manager binaries whose ``test`` / ``run`` scripts live in the
#: workspace's ``package.json``. Invoking them as a benchmark or gate driver
#: hands the agent the command the round is graded on.
_PACKAGE_MANAGER_BINS = frozenset({"npm", "npx", "yarn", "pnpm", "bun"})


def argv_invokes_harness(argv: tuple[str, ...]) -> bool:
    """True when some argv element names a path under ``{harness}``.

    The substitution happens later; this only asks whether the *declared*
    command is a harness driver. A command with no placeholder is executed
    from the workspace, which is the agent's write surface.
    """
    return any("{harness}" in element for element in argv)


def argv_is_workspace_package_script(argv: tuple[str, ...]) -> bool:
    """True when ``argv`` runs a package-manager script the workspace owns.

    ``npm test`` reads ``package.json`` in ``cwd``. That file is copied into
    the attempt, so the agent can replace the script with ``echo 0.15`` and
    report a multi-thousand-x speedup. A harness-owned wrapper around the
    same binary is fine — the placeholder is the ownership marker.
    """
    if not argv:
        return False
    return argv[0] in _PACKAGE_MANAGER_BINS and not argv_invokes_harness(argv)


#: Directory and file names never copied into an attempt workspace. Basename
#: patterns, matching :func:`shutil.ignore_patterns` semantics. ``.git`` is
#: excluded so an attempt cannot rewrite the source repo's history, and the
#: virtualenv so the workspace cannot silently grade a stale installed copy of
#: the code the agent just edited.
DEFAULT_WORKSPACE_EXCLUDES = (
    ".git",
    ".venv",
    "__pycache__",
    "*.pyc",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".DS_Store",
)


class SpreadProvenance(str, Enum):  # noqa: UP042
    """Whether a baseline's run-to-run spread was measured or assumed.

    The noise gate is only as trustworthy as the baseline spread it compares
    against, and half of this corpus was profiled with the spread reported and
    half without. Recording which is which keeps a conservative default from
    being read later as a measurement.
    """

    MEASURED = "measured"
    ASSUMED = "assumed"


#: Spread assumed for a baseline that was timed without one being reported.
#: Deliberately looser than the ~0.9% actually observed on the two problems
#: where it *was* reported: an assumed figure should err toward refusing to
#: call a small difference a speedup.
ASSUMED_RELATIVE_SPREAD = 0.02


class LoopholeRuling(str, Enum):  # noqa: UP042
    """Whether a known shortcut counts as cheating.

    ``UNDECIDED`` is a real, usable value and the default for a reason. Whether
    problem 4's hardcode-the-constants route counts is a reward-hacking policy
    question the brief explicitly flags as an operator decision ("Decide
    whether that counts — this is a reward-hacking decision, not a detail").
    Defaulting it to either answer would silently make that decision here.
    Loop 2's cheat detector — not built, out of scope for loop 1 — is the
    consumer of these rulings.
    """

    COUNTS_AS_CHEATING = "counts_as_cheating"
    ALLOWED = "allowed"
    UNDECIDED = "undecided"


@dataclass(frozen=True, slots=True)
class Loophole:
    """A route to the score that does not require solving the problem.

    Recorded per problem because reward hacking is the *expected* shortest path
    for a code-editing agent facing a frozen verifier, and because a loophole
    discovered after a round cannot tell you whether earlier rounds took it.
    """

    id: str
    description: str
    ruling: LoopholeRuling
    detection_hint: str

    def __post_init__(self) -> None:
        if not self.id or not self.description.strip():
            raise ContractViolationError("a loophole needs an id and a description")
        if not self.detection_hint.strip():
            raise ContractViolationError(
                "a loophole with no detection hint cannot be checked for; say how it "
                "would be spotted, even if the answer is 'by reading the diff'"
            )


@dataclass(frozen=True, slots=True)
class GateCommand:
    """One command the correctness gate runs. It must exit 0.

    ``min_passing_tests`` is the anti-deletion floor. "The suite passes" and
    "the suite is empty" are the same exit code, and deleting tests is the
    cheapest speedup available to a workspace graded on a test command. The
    floor is pinned from the counts observed when the corpus was built.

    ``expect_success=False`` exists for one measured reason: **the Maestro
    baseline is not green.** Its suite has 18 pre-existing failures unrelated to
    the problem, so the command that produces its test report exits non-zero
    every single time, correct fix or not. Gating on that exit code would fail
    every attempt. Such a command is run for its *output*, and the verdict
    comes from the artifact comparison instead. A command with
    ``expect_success=False`` and no comparison downstream checks nothing, which
    :class:`CorrectnessGate` refuses.
    """

    argv: tuple[str, ...]
    timeout_seconds: float
    label: str = ""
    min_passing_tests: int | None = None
    expect_success: bool = True

    def __post_init__(self) -> None:
        if not self.argv:
            raise ContractViolationError("a gate command needs an argv")
        if self.timeout_seconds <= 0:
            raise ContractViolationError("a gate command needs a positive timeout")
        if self.min_passing_tests is not None and self.min_passing_tests <= 0:
            raise ContractViolationError(
                "min_passing_tests must be positive; zero is the value an empty suite "
                "reports and would defeat the purpose"
            )
        if not self.expect_success and self.min_passing_tests is not None:
            raise ContractViolationError(
                "a command whose exit code is ignored cannot also carry a passing-test "
                "floor; put the floor on a command that must succeed, or check the count "
                "in the artifact comparison"
            )


@dataclass(frozen=True, slots=True)
class OutputComparison:
    """Compare an artifact the workspace produced against a pinned answer.

    ``artifact_path`` is relative to the *workspace* — the agent's write
    surface, where the harness dumper writes.

    ``reference_path`` is relative to the **reference root**, which is
    deliberately *not* inside the workspace. A pinned answer stored where the
    graded party can edit it is not pinned. This module resolves the two paths
    on opposite sides of that boundary; enforcing that the agent's process
    cannot write to the reference root is an OS-level concern (brief § Gates,
    open question Q11).
    """

    artifact_path: str
    reference_path: str
    tolerance: Tolerance

    def __post_init__(self) -> None:
        for label, value in (
            ("artifact_path", self.artifact_path),
            ("reference_path", self.reference_path),
        ):
            if not value:
                raise ContractViolationError(f"{label} must be non-empty")
            if value.startswith("/") or ".." in value.split("/"):
                raise ContractViolationError(
                    f"{label}={value!r} must be a relative path that stays inside its "
                    "root; a comparison that can escape its root can read anything"
                )


@dataclass(frozen=True, slots=True)
class CorrectnessGate:
    """The bar a workspace must clear before its speed is even measured.

    Commands run in declaration order and the first failure short-circuits. The
    optional :class:`OutputComparison` runs last, on an artifact an earlier
    command produced.
    """

    description: str
    commands: tuple[GateCommand, ...]
    comparison: OutputComparison | None = None

    def __post_init__(self) -> None:
        if not self.commands:
            raise ContractViolationError(
                "a correctness gate with no commands passes everything, including a "
                "workspace that deleted the implementation"
            )
        if not self.description.strip():
            raise ContractViolationError("a correctness gate must describe what it checks")
        checks_something = self.comparison is not None or any(
            c.expect_success for c in self.commands
        )
        if not checks_something:
            raise ContractViolationError(
                "every command ignores its exit code and no artifact comparison is "
                "declared, so this gate accepts any workspace at all"
            )


@dataclass(frozen=True, slots=True)
class TimingSpec:
    """The baseline command and the pinned measurement it is compared against.

    ``baseline_seconds`` is frozen with the rest of the problem rather than
    re-measured per attempt: re-deriving it would make every score depend on
    how busy the machine was when the attempt happened, and would hand the
    agent a second lever — slow the baseline down — that has nothing to do with
    solving anything.

    ``argv`` may contain the ``{python}``, ``{workspace}`` and ``{harness}``
    placeholders (see :func:`turing.research.problems.process.render_argv`).
    The benchmark driver belongs under ``{harness}``, outside the workspace,
    for the same reason the reference artifacts do.
    """

    argv: tuple[str, ...]
    timeout_seconds: float
    baseline_seconds: float
    baseline_relative_spread: float
    spread_provenance: SpreadProvenance
    measured_on: str
    measured_at: str
    baseline_samples: tuple[float, ...] = ()
    runs: int = MINIMUM_RUNS

    def __post_init__(self) -> None:
        if not self.argv:
            raise ContractViolationError("a timing spec needs an argv")
        if self.baseline_seconds <= 0:
            raise ContractViolationError("baseline_seconds must be positive")
        if self.baseline_relative_spread < 0:
            raise ContractViolationError("baseline spread cannot be negative")
        if self.runs < MINIMUM_RUNS:
            raise ContractViolationError(
                f"a timing spec must ask for at least {MINIMUM_RUNS} runs; a single run "
                "reports no spread and cannot rule out noise"
            )
        if self.timeout_seconds < self.baseline_seconds:
            raise ContractViolationError(
                f"timeout {self.timeout_seconds}s is below the {self.baseline_seconds}s "
                "baseline; the unmodified workspace would be killed and score as broken"
            )
        if not self.measured_on.strip() or not self.measured_at.strip():
            raise ContractViolationError(
                "a pinned baseline must record the machine and date it came from"
            )
        if self.baseline_samples:
            if len(self.baseline_samples) < MINIMUM_RUNS:
                raise ContractViolationError(
                    "baseline_samples records the actual runs; one sample is not a spread"
                )
            if self.spread_provenance is not SpreadProvenance.MEASURED:
                raise ContractViolationError(
                    "baseline_samples were recorded, so the spread is MEASURED"
                )
            observed = relative_spread(self.baseline_samples)
            if abs(observed - self.baseline_relative_spread) > 0.002:
                raise ContractViolationError(
                    f"declared spread {self.baseline_relative_spread:.4f} does not match "
                    f"the {observed:.4f} implied by the recorded samples"
                )


@dataclass(frozen=True, slots=True)
class SpeedupProblemSpec:
    """One speedup problem, fully declared.

    ``headroom`` is the speedup a known-good fix achieved during profiling. It
    is *not* a target and never enters the score — it is recorded so a round
    that reports 40× on a problem with 2× of headroom is visibly a measurement
    bug or a cheat rather than a triumph.
    """

    id: str
    goal: str
    target: str
    source_root: Path
    timing: TimingSpec
    gate: CorrectnessGate
    split: Split
    headroom: float
    loopholes: tuple[Loophole, ...] = ()
    workspace_excludes: tuple[str, ...] = DEFAULT_WORKSPACE_EXCLUDES
    notes: str = ""
    default_cap: Cap | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise ContractViolationError("a problem spec needs an id")
        if not self.goal.strip():
            raise ContractViolationError(f"{self.id}: goal must be non-empty")
        if not self.target.strip():
            raise ContractViolationError(f"{self.id}: target must name what to make faster")
        if self.headroom <= 1.0:
            raise ContractViolationError(
                f"{self.id}: headroom {self.headroom} is not a speedup; a problem with no "
                "measured headroom is a problem nobody has shown is solvable"
            )
        seen: set[str] = set()
        for loophole in self.loopholes:
            if loophole.id in seen:
                raise ContractViolationError(f"{self.id}: duplicate loophole {loophole.id!r}")
            seen.add(loophole.id)
        if not argv_invokes_harness(self.timing.argv):
            raise ContractViolationError(
                f"{self.id}: timing argv must invoke a driver under {{harness}}; "
                "a driver inside the workspace is a benchmark the agent can rewrite"
            )
        for command in self.gate.commands:
            if argv_is_workspace_package_script(command.argv):
                raise ContractViolationError(
                    f"{self.id}: gate command {command.argv!r} runs a package-manager "
                    "script the workspace owns; the agent can rewrite package.json"
                )

    @property
    def is_held_out(self) -> bool:
        return self.split is Split.HELD_OUT

    @property
    def undecided_loopholes(self) -> tuple[Loophole, ...]:
        """Loopholes still awaiting an operator ruling.

        Surfaced as a property so "we never decided whether that counts" is a
        query rather than a memory. Loop 2's cheat detector cannot be written
        against an undecided rule.
        """
        return tuple(lh for lh in self.loopholes if lh.ruling is LoopholeRuling.UNDECIDED)

    def fingerprint_material(self) -> str:
        """Canonical string this spec contributes to the eval-set hash.

        Covers everything that could change what the problem measures: the
        goal, the split, which repo is copied, what is excluded from the
        workspace, the baseline, the timing timeout, the gate commands, the
        anti-deletion floors and the tolerance semantics. Notes and loophole
        rulings are excluded — annotating a corpus must not restart the
        trajectory.
        """
        parts = [
            f"id={self.id}",
            f"target={self.target}",
            f"goal={self.goal}",
            f"split={self.split.value}",
            f"source_root={self.source_root}",
            f"excludes={sorted(self.workspace_excludes)}",
            f"timing={list(self.timing.argv)}",
            f"timeout={self.timing.timeout_seconds!r}",
            f"baseline={self.timing.baseline_seconds!r}",
            f"spread={self.timing.baseline_relative_spread!r}",
            f"runs={self.timing.runs}",
        ]
        for command in self.gate.commands:
            parts.append(f"gate={list(command.argv)}:{command.min_passing_tests}")
        comparison = self.gate.comparison
        if comparison is not None:
            tolerance = comparison.tolerance
            parts.append(
                f"compare={comparison.artifact_path}:{comparison.reference_path}:"
                f"{tolerance.mode.value}:{tolerance.rtol!r}:{tolerance.atol!r}:"
                f"{tolerance.min_cosine!r}:{tolerance.all_pass_scope}:"
                f"{tolerance.min_all_pass_count}"
            )
        return "|".join(parts)
