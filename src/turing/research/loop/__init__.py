"""The round runner and the measurement layer — loop 1's instrument.

One round is: every problem in the corpus, one attempt each, run unattended
against its frozen verifier, reduced to per-``(problem_type, split)`` cells,
and written out as a round record plus one row of ``trajectory.json``.

Read the modules in this order:

* :mod:`~turing.research.loop.protocols` — what the runner needs from a solver,
  and the three things it deliberately does not delegate (the cap, the
  verifier, and termination).
* :mod:`~turing.research.loop.metrics` — the four driving-function numbers, the
  seed-noise floor, and the refusals that keep them honest.
* :mod:`~turing.research.loop.trajectory` — the on-disk layout from
  ``driving-functions.md``, with mandatory lineage.
* :mod:`~turing.research.loop.runner` — :class:`RoundRunner` itself.
* :mod:`~turing.research.loop.solver_bridge` — the real
  :class:`~turing.research.solver.Solver` as a runner ``step``.
* :mod:`~turing.research.loop.noise_floor` — >=3 seeds, before round 0.
* :mod:`~turing.research.loop.escalation` — the loop's only interrupt.
* :mod:`~turing.research.loop.self_edit_seam` — **loop 2's attach point**,
  unused here.

Four properties this package exists to guarantee:

1. **Types are never averaged.** Every score is a cell; nothing returns a
   blended headline number, and ``RoundRecord`` has no aggregate attribute.
2. **No noise floor, no saturation verdict.** The refusal is a value that gets
   written into the trajectory, not an error that gets swallowed.
3. **Lineage is mandatory.** Parent round, eval-set hash and engine identity
   ride on every row, and rounds measured on different eval sets are flagged
   incomparable and get no delta at all.

The intended no-quit protocol is **not** in that list. A hopeless project
escalates to the operator (``continue`` / ``abandon`` / ``extend_cap``) at
the runner's call sites. The types only require a non-empty
``escalation_id`` for ``ABANDONED``; that is not an operator verdict, and
this package does not claim it is.
"""

from __future__ import annotations

from turing.research.loop.escalation import (
    FileDropDecisionInbox,
    NtfyEscalationNotifier,
    OperatorEscalationChannel,
    OperatorEscalationGate,
    build_operator_channel,
)
from turing.research.loop.metrics import (
    DEFAULT_SCORE_FLOORS,
    MIN_NOISE_FLOOR_SEEDS,
    Cell,
    CostBasis,
    NoiseFloor,
    NoiseFloorStatistic,
    SaturationAssessment,
    SaturationVerdict,
    ScoredProblem,
    assess_saturation,
    build_type_scores,
    compute_deltas,
    cost_per_unit_gain,
    floors_by_cell,
    measure_noise_floor,
    round_verdict,
)
from turing.research.loop.noise_floor import (
    NoiseFloorConfig,
    NoiseFloorReport,
    NoiseFloorRunner,
)
from turing.research.loop.protocols import (
    Clock,
    EscalationChannel,
    Solver,
    SolverStep,
    SolverTask,
    SystemClock,
    WorkspaceProvider,
)
from turing.research.loop.runner import (
    AttemptOutcome,
    PassCriterion,
    RoundConfig,
    RoundOutcome,
    RoundRunner,
)
from turing.research.loop.self_edit_seam import (
    SelfEditInputs,
    SelfEditStep,
    collect_self_edit_inputs,
)
from turing.research.loop.settings import ResearchLoopSettings
from turing.research.loop.solver_bridge import SolverBridge
from turing.research.loop.trajectory import (
    TRAJECTORY_SCHEMA_VERSION,
    AttemptLog,
    StepLog,
    TrajectoryStore,
    cell_key,
)
from turing.research.loop.workspace import CopyTreeWorkspaceProvider

__all__ = [
    "DEFAULT_SCORE_FLOORS",
    "MIN_NOISE_FLOOR_SEEDS",
    "TRAJECTORY_SCHEMA_VERSION",
    "AttemptLog",
    "AttemptOutcome",
    "Cell",
    "Clock",
    "CopyTreeWorkspaceProvider",
    "CostBasis",
    "EscalationChannel",
    "FileDropDecisionInbox",
    "NoiseFloor",
    "NoiseFloorConfig",
    "NoiseFloorReport",
    "NoiseFloorRunner",
    "NoiseFloorStatistic",
    "NtfyEscalationNotifier",
    "OperatorEscalationChannel",
    "OperatorEscalationGate",
    "PassCriterion",
    "ResearchLoopSettings",
    "RoundConfig",
    "RoundOutcome",
    "RoundRunner",
    "SaturationAssessment",
    "SaturationVerdict",
    "ScoredProblem",
    "SelfEditInputs",
    "SelfEditStep",
    "Solver",
    "SolverBridge",
    "SolverStep",
    "SolverTask",
    "StepLog",
    "SystemClock",
    "TrajectoryStore",
    "WorkspaceProvider",
    "assess_saturation",
    "build_operator_channel",
    "build_type_scores",
    "cell_key",
    "collect_self_edit_inputs",
    "compute_deltas",
    "cost_per_unit_gain",
    "floors_by_cell",
    "measure_noise_floor",
    "round_verdict",
]
