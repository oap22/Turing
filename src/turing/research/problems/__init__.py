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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # See turing/research/loop/__init__.py for why this package resolves its
    # public names lazily instead of importing every submodule up front:
    # importing this package (a step Python always takes before importing any
    # submodule of it, e.g. turing.research.problems.adapter) would otherwise
    # force turing.research.problems.kaggle to load unconditionally even
    # though nothing on the speedup path — the corpus this package's own
    # __all__ is overwhelmingly about — touches it.
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

_SUBMODULE_BY_NAME: dict[str, str] = {
    "AdapterRegistry": "adapter",
    "ProblemAdapter": "adapter",
    "WorkspaceMaterialisationError": "adapter",
    "bind_eval_set_hash": "adapter",
    "fingerprint_corpus": "adapter",
    "DEFAULT_MAESTRO_REPO": "catalog",
    "DEFAULT_SPLITS": "catalog",
    "DEFAULT_TURING_REPO": "catalog",
    "MEASURED_AT": "catalog",
    "MEASURED_ON": "catalog",
    "speedup_specs": "catalog",
    "KAGGLE_CORPUS_SIZE": "kaggle",
    "KAGGLE_SELECTION_CRITERION": "kaggle",
    "KaggleAdapter": "kaggle",
    "CommandResult": "process",
    "CommandRunner": "process",
    "SubprocessCommandRunner": "process",
    "count_passing_tests": "process",
    "render_argv": "process",
    "run_command": "process",
    "ASSUMED_RELATIVE_SPREAD": "spec",
    "DEFAULT_WORKSPACE_EXCLUDES": "spec",
    "CorrectnessGate": "spec",
    "GateCommand": "spec",
    "Loophole": "spec",
    "LoopholeRuling": "spec",
    "OutputComparison": "spec",
    "SpeedupProblemSpec": "spec",
    "SpreadProvenance": "spec",
    "TimingSpec": "spec",
    "HARNESS_FAILURE_KEY": "speedup",
    "SpeedupAdapter": "speedup",
    "SpeedupVerifier": "speedup",
    "MINIMUM_NOISE_BAND": "timing",
    "SpeedupMeasurement": "timing",
    "TimingHarness": "timing",
    "TimingMeasurement": "timing",
    "relative_spread": "timing",
    "ComparisonOutcome": "tolerance",
    "Tolerance": "tolerance",
    "ToleranceMode": "tolerance",
    "compare": "tolerance",
}


def __getattr__(name: str) -> Any:
    """Resolve a public name by importing its defining submodule on first use.

    Keeps every name in ``__all__`` importable from ``turing.research.problems``
    without paying for all eight submodules — including ``kaggle``, which
    nothing on the speedup path touches — just to import this package, which
    Python always does before importing any submodule of it.
    """
    submodule_name = _SUBMODULE_BY_NAME.get(name)
    if submodule_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    submodule = importlib.import_module(f"{__name__}.{submodule_name}")
    value = getattr(submodule, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
