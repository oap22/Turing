"""The command-execution seam.

The solver can hand a proposal's commands to an injected runner and feed the
exit codes back into the next model call, but it ships without one. Executing
model-authored commands is the safety layer's job — shell gate, deny-list,
capability tokens — and the OS-level half of that is the separate ``turing``
macOS user that owns the workspace root. Nothing in this package runs a
subprocess, and a test asserts the refusal path (see ``test_escalation.py``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import AttemptState, Cap
from turing.research.solver import (
    CommandOutcome,
    FileEdit,
    Proposal,
    Solver,
    SolverPolicy,
)

from .conftest import (
    SOLUTION_FILE,
    FakeClock,
    ScriptedBackend,
    ScriptedChannel,
    fresh_with_cap,
)

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import Attempt, Problem
    from turing.research.solver import InMemoryCheckpointStore, WorkspaceManager


class RecordingRunner:
    """Records commands instead of running them. No subprocess, by design."""

    def __init__(self, exit_code: int = 0) -> None:
        self.seen: list[tuple[str, Path, float]] = []
        self._exit_code = exit_code

    async def run(self, command: str, *, workspace: Path, timeout_seconds: float) -> CommandOutcome:
        self.seen.append((command, workspace, timeout_seconds))
        return CommandOutcome(
            command=command,
            exit_code=self._exit_code,
            stdout_tail="52 passed",
            duration_seconds=0.07,
        )


class HangingRunner:
    async def run(self, command: str, *, workspace: Path, timeout_seconds: float) -> CommandOutcome:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


def _proposal(index: int, value: float, commands: tuple[str, ...]) -> Proposal:
    return Proposal(
        proposal_id=f"p{index}",
        rationale=f"try {value}",
        edits=(FileEdit(relative_path=SOLUTION_FILE, content=str(value)),),
        commands=commands,
        tokens=10,
    )


class TestWithARunner:
    async def test_commands_run_inside_the_workspace_after_the_edits(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        runner = RecordingRunner()
        backend = ScriptedBackend([_proposal(0, 3.0, ("pytest -q",))])
        solver = Solver(
            backend=backend,
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=3.0),
            clock=clock,
            runner=runner,
        )

        outcome = await solver.run(attempt, problem)

        assert outcome.passed
        command, workspace, timeout = runner.seen[0]
        assert command == "pytest -q"
        assert workspace == attempt.workspace_path
        assert timeout > 0  # the remaining wall-clock budget, not an ad-hoc number

    async def test_exit_codes_reach_the_next_model_call(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """The point of running anything: the model gets to see what happened."""
        backend = ScriptedBackend(
            [_proposal(0, 1.0, ("pytest -q",)), _proposal(1, 2.0, ("pytest -q",))]
        )
        solver = Solver(
            backend=backend,
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=2.0),
            clock=clock,
            runner=RecordingRunner(exit_code=1),
        )

        await solver.run(attempt, problem)

        second_context = backend.contexts[1]
        assert second_context.history[0].command_exit_codes == (1,)

    async def test_the_outcomes_are_journalled(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        solver = Solver(
            backend=ScriptedBackend([_proposal(0, 3.0, ("pytest -q", "ruff check ."))]),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=3.0),
            clock=clock,
            runner=RecordingRunner(),
        )
        await solver.run(attempt, problem)

        (record,) = await store.iterations(attempt.attempt_id)
        assert [c.command for c in record.command_outcomes] == ["pytest -q", "ruff check ."]

    async def test_a_command_that_hangs_costs_the_budget_not_the_night(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
    ) -> None:
        """Same deadline as the model call: the cap binds every phase."""
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=5, max_tokens=10_000, max_wall_clock_seconds=0.05)
        )
        channel = ScriptedChannel()
        solver = Solver(
            backend=ScriptedBackend([_proposal(0, 3.0, ("sleep 30",))]),
            store=store,
            workspaces=manager,
            escalations=channel,
            runner=HangingRunner(),
        )

        outcome = await solver.run(attempt, problem)

        assert outcome.suspended
        assert outcome.attempt.consumed.wall_clock_seconds >= 0.05


class TestVerificationDeadline:
    async def test_a_verifier_that_hangs_costs_the_budget_not_the_night(
        self,
        attempt: Attempt,
        template_dir: Path,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
    ) -> None:
        from dataclasses import dataclass

        from turing.research.contracts import (
            Problem,
            ProblemType,
            Split,
            VerificationResult,
            Verifier,
        )

        @dataclass(frozen=True)
        class HangingVerifier(Verifier):
            async def verify(self, workspace: Path) -> VerificationResult:
                await asyncio.sleep(30)
                raise AssertionError("unreachable")

        problem = Problem(
            id="speedup-1",
            problem_type=ProblemType.SPEEDUP,
            goal="goal",
            workspace_template=template_dir,
            verifier=HangingVerifier(
                verifier_id="v-speedup-1",
                problem_id="speedup-1",
                description="never returns",
                score_scale="speedup_ratio",
            ),
            split=Split.PRACTICE,
        )
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=5, max_tokens=10_000, max_wall_clock_seconds=0.05)
        )
        solver = Solver(
            backend=ScriptedBackend([_proposal(0, 3.0, ())]),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(escalate_on_cap_exhausted=False),
        )

        outcome = await solver.run(attempt, problem)

        assert outcome.state is AttemptState.FAILED_WITHIN_CAP
        assert outcome.best_result is None


@pytest.mark.parametrize("commands", [("pytest -q",), ("a", "b")])
def test_commands_are_carried_verbatim_on_the_proposal(commands: tuple[str, ...]) -> None:
    assert Proposal(proposal_id="p0", commands=commands).commands == commands
