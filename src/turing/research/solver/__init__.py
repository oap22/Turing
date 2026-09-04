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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # See turing/research/loop/__init__.py for why this package resolves its
    # public names lazily instead of importing every submodule up front:
    # SolverSettings alone (turing.research.solver.config) pulls in pydantic
    # and pydantic_settings, which most importers of this package — anything
    # that only needs Solver, the checkpoint stores or the models — never
    # touch.
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

_SUBMODULE_BY_NAME: dict[str, str] = {
    "InMemoryCheckpointStore": "checkpoints",
    "SqliteCheckpointStore": "checkpoints",
    "attempt_from_json": "checkpoints",
    "attempt_to_json": "checkpoints",
    "escalation_from_json": "checkpoints",
    "escalation_to_json": "checkpoints",
    "iteration_from_json": "checkpoints",
    "iteration_to_json": "checkpoints",
    "SolverSettings": "config",
    "CheckpointError": "errors",
    "ProposalError": "errors",
    "SolverError": "errors",
    "WorkspaceEscapeError": "errors",
    "WorkspaceTemplateError": "errors",
    "CommandOutcome": "models",
    "FileEdit": "models",
    "IterationPhase": "models",
    "IterationRecord": "models",
    "IterationSummary": "models",
    "ProgressSummary": "models",
    "Proposal": "models",
    "ProposalContext": "models",
    "SolverOutcome": "models",
    "SolverPolicy": "models",
    "better_result": "models",
    "summarise_progress": "models",
    "CheckpointStore": "protocols",
    "Clock": "protocols",
    "CommandRunner": "protocols",
    "EscalationChannel": "protocols",
    "ProposalBackend": "protocols",
    "ResumableBackend": "protocols",
    "SystemClock": "protocols",
    "Solver": "solver",
    "Workspace": "workspace",
    "WorkspaceManager": "workspace",
}


def __getattr__(name: str) -> Any:
    """Resolve a public name by importing its defining submodule on first use.

    Keeps every name in ``__all__`` importable from ``turing.research.solver``
    without paying for all six submodules — including ``config``, which pulls
    in pydantic and pydantic_settings for ``SolverSettings`` — just to import
    this package, which most importers only need for a handful of names.
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
