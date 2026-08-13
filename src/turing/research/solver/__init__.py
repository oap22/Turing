"""One project attempt: build a solution, score it, revise, until the cap.

Single run with iterative refinement against the problem's own frozen verifier
— the honest baseline for round 0, and deliberately not parallel attempts or
tree search. Every step checkpoints through
:class:`turing.research.contracts.Attempt` so an interrupted subscription
window costs the remainder of an iteration, not the attempt.

Wiring one up::

    solver = Solver(
        backend=backend,                      # turing.research.backends, adapted
        store=await SqliteCheckpointStore(settings.research_checkpoint_db).open(),
        workspaces=WorkspaceManager(settings.research_workspace_root),
        escalations=channel,                  # ntfy out, three-valued verdict back
        policy=settings.default_policy(target_score=2.0),
    )
    outcome = await solver.run(attempt, problem)
    if outcome.suspended:
        ...   # a human was asked; call run() again once they answer
    elif outcome.paused:
        ...   # the window closed; call solver.resume(attempt_id, problem)

What is **not** here, on purpose: scaffold self-modification, cheat detection
and rollback. Those are loop 2. The seam they attach to is the round boundary
above this module; the per-attempt journal this solver writes is the raw
material a self-edit summary would later read, after
:meth:`turing.research.contracts.RoundRecord.self_edit_visible_scores` has
dropped held-out results from it.
"""

from __future__ import annotations

from turing.research.solver.checkpoints import (
    InMemoryCheckpointStore,
    SqliteCheckpointStore,
    attempt_from_json,
    attempt_to_json,
    escalation_from_json,
    escalation_to_json,
    iteration_from_json,
    iteration_to_json,
)
from turing.research.solver.config import SolverSettings
from turing.research.solver.errors import (
    CheckpointError,
    ProposalError,
    SolverError,
    WorkspaceEscapeError,
    WorkspaceTemplateError,
)
from turing.research.solver.models import (
    CommandOutcome,
    FileEdit,
    IterationPhase,
    IterationRecord,
    IterationSummary,
    ProgressSummary,
    Proposal,
    ProposalContext,
    SolverOutcome,
    SolverPolicy,
    better_result,
    summarise_progress,
)
from turing.research.solver.protocols import (
    CheckpointStore,
    Clock,
    CommandRunner,
    EscalationChannel,
    ProposalBackend,
    ResumableBackend,
    SystemClock,
)
from turing.research.solver.solver import Solver
from turing.research.solver.workspace import Workspace, WorkspaceManager

__all__ = [
    "CheckpointError",
    "CheckpointStore",
    "Clock",
    "CommandOutcome",
    "CommandRunner",
    "EscalationChannel",
    "FileEdit",
    "InMemoryCheckpointStore",
    "IterationPhase",
    "IterationRecord",
    "IterationSummary",
    "ProgressSummary",
    "Proposal",
    "ProposalBackend",
    "ProposalContext",
    "ProposalError",
    "ResumableBackend",
    "Solver",
    "SolverError",
    "SolverOutcome",
    "SolverPolicy",
    "SolverSettings",
    "SqliteCheckpointStore",
    "SystemClock",
    "Workspace",
    "WorkspaceEscapeError",
    "WorkspaceManager",
    "WorkspaceTemplateError",
    "attempt_from_json",
    "attempt_to_json",
    "better_result",
    "escalation_from_json",
    "escalation_to_json",
    "iteration_from_json",
    "iteration_to_json",
    "summarise_progress",
]
