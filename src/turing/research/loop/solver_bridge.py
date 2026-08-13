"""Adapts the within-project solver to the round runner's step protocol.

Two packages were built in parallel. The runner drives
:meth:`~turing.research.loop.protocols.Solver.step` and owns the cap, the
verifier, and termination. The solver's :meth:`~turing.research.solver.Solver.run`
is a whole attempt loop that would compete with that. This bridge is the
honest join: one runner step is one :meth:`Solver.propose_and_apply`, and
the runner still verifies, still charges, still decides when to stop.

The backend adapter
(:class:`~turing.research.backends.adapter.ProposalAdapter`) is a separate
seam — messages in, :class:`~turing.research.solver.models.Proposal` out —
and is what lets a :class:`~turing.research.backends.fake.FakeBackend` drive
the real solver at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turing.research.contracts import EscalationReason
from turing.research.loop.protocols import SolverStep
from turing.research.solver.models import ProposalContext

if TYPE_CHECKING:
    from turing.research.contracts import Attempt
    from turing.research.loop.protocols import SolverTask
    from turing.research.solver import Solver

__all__ = ["SolverBridge"]


class SolverBridge:
    """:class:`~turing.research.loop.protocols.Solver` over a real
    :class:`~turing.research.solver.Solver`.
    """

    def __init__(self, solver: Solver) -> None:
        self._solver = solver

    @property
    def solver(self) -> Solver:
        return self._solver

    async def step(self, task: SolverTask, attempt: Attempt) -> SolverStep:
        """Propose and apply one change in ``attempt.workspace_path``.

        Builds a :class:`~turing.research.solver.models.ProposalContext` from
        the task and the attempt — goal, scores, remaining cap, never a
        verifier handle. Tokens on the returned step are the backend's
        accounted usage for that call.
        """
        context = ProposalContext(
            problem_id=task.id,
            problem_type=task.problem_type,
            goal=task.goal,
            score_scale=task.score_scale,
            workspace=attempt.workspace_path,
            iteration_index=attempt.step_index,
            seed=attempt.seed,
            remaining=attempt.remaining,
            last_result=attempt.result,
            best_result=attempt.result,
        )
        proposal = await self._solver.propose_and_apply(context, resume_token=attempt.resume_token)
        escalate = EscalationReason.NO_VIABLE_APPROACH if proposal.no_viable_approach else None
        made_progress = bool(proposal.edits or proposal.commands) and not (
            proposal.no_viable_approach
        )
        return SolverStep(
            tokens=proposal.tokens,
            note=proposal.rationale,
            escalate=escalate,
            made_progress=made_progress,
            resume_token=self._solver.backend_resume_token(),
        )
