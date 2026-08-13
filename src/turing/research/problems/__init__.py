"""Concrete verifiers and the problem corpus (loop 1).

Each verifier here subclasses :class:`turing.research.contracts.Verifier` and
is frozen at approval time — nothing in the solver may construct or replace
one.

The package is organised around one seam, :class:`ProblemAdapter`: a problem
family is a thing that can enumerate its problems and materialise a workspace
for one attempt at one of them. Everything family-specific lives behind that.
The core never imports a concrete adapter, so *"Kaggle is one translator behind
the interface, not the interface itself"* is enforced by the import graph
rather than by intention.

Modules:

* :mod:`~turing.research.problems.adapter` — the seam, an adapter registry, and
  the corpus fingerprint that becomes ``RoundRecord.eval_set_hash``.
* :mod:`~turing.research.problems.spec` — declarative problem definitions.
* :mod:`~turing.research.problems.tolerance` — how "the answer did not change"
  is decided, per problem, with the measurement behind each choice recorded.
* :mod:`~turing.research.problems.timing` — repeat, report spread, and refuse
  to call a difference within run-to-run noise a speedup.
* :mod:`~turing.research.problems.process` — argv-only subprocess execution.
* :mod:`~turing.research.problems.speedup` — the working speedup adapter and
  verifier.
* :mod:`~turing.research.problems.catalog` — the six measured speedup problems.
* :mod:`~turing.research.problems.kaggle` — an explicit stub carrying the
  pre-registered, feasibility-only selection criterion.

**Scope: loop 1.** Nothing here reads or edits the scaffold. The
:class:`~turing.research.problems.spec.Loophole` records attached to each
problem are the input loop 2's cheat detector will need; loop 1 only carries
them, and :attr:`~turing.research.problems.spec.SpeedupProblemSpec.
undecided_loopholes` keeps the unanswered policy questions visible.
"""

from __future__ import annotations

from turing.research.problems.adapter import (
    AdapterRegistry,
    ProblemAdapter,
    WorkspaceMaterialisationError,
    bind_eval_set_hash,
    fingerprint_corpus,
)
from turing.research.problems.catalog import (
    DEFAULT_MAESTRO_REPO,
    DEFAULT_SPLITS,
    DEFAULT_TURING_REPO,
    MEASURED_AT,
    MEASURED_ON,
    speedup_specs,
)
from turing.research.problems.kaggle import (
    KAGGLE_CORPUS_SIZE,
    KAGGLE_SELECTION_CRITERION,
    KaggleAdapter,
)
from turing.research.problems.process import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
    count_passing_tests,
    render_argv,
    run_command,
)
from turing.research.problems.spec import (
    ASSUMED_RELATIVE_SPREAD,
    DEFAULT_WORKSPACE_EXCLUDES,
    CorrectnessGate,
    GateCommand,
    Loophole,
    LoopholeRuling,
    OutputComparison,
    SpeedupProblemSpec,
    SpreadProvenance,
    TimingSpec,
)
from turing.research.problems.speedup import (
    HARNESS_FAILURE_KEY,
    SpeedupAdapter,
    SpeedupVerifier,
)
from turing.research.problems.timing import (
    MINIMUM_NOISE_BAND,
    SpeedupMeasurement,
    TimingHarness,
    TimingMeasurement,
    relative_spread,
)
from turing.research.problems.tolerance import (
    ComparisonOutcome,
    Tolerance,
    ToleranceMode,
    compare,
)

__all__ = [
    "ASSUMED_RELATIVE_SPREAD",
    "DEFAULT_MAESTRO_REPO",
    "DEFAULT_SPLITS",
    "DEFAULT_TURING_REPO",
    "DEFAULT_WORKSPACE_EXCLUDES",
    "HARNESS_FAILURE_KEY",
    "KAGGLE_CORPUS_SIZE",
    "KAGGLE_SELECTION_CRITERION",
    "MEASURED_AT",
    "MEASURED_ON",
    "MINIMUM_NOISE_BAND",
    "AdapterRegistry",
    "CommandResult",
    "CommandRunner",
    "ComparisonOutcome",
    "CorrectnessGate",
    "GateCommand",
    "KaggleAdapter",
    "Loophole",
    "LoopholeRuling",
    "OutputComparison",
    "ProblemAdapter",
    "SpeedupAdapter",
    "SpeedupMeasurement",
    "SpeedupProblemSpec",
    "SpeedupVerifier",
    "SpreadProvenance",
    "SubprocessCommandRunner",
    "TimingHarness",
    "TimingMeasurement",
    "TimingSpec",
    "Tolerance",
    "ToleranceMode",
    "WorkspaceMaterialisationError",
    "bind_eval_set_hash",
    "compare",
    "count_passing_tests",
    "fingerprint_corpus",
    "relative_spread",
    "render_argv",
    "run_command",
    "speedup_specs",
]
