"""Cap enforcement — the runaway-loop brake that replaced dollar metering.

A solver that can exceed its cap is a critical defect, so these tests assert
exact counts rather than "roughly". Each dimension is checked on its own, the
wall-clock one twice: once with a driven clock (arithmetic) and once with a
real one against a call that never returns (enforcement).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import (
    AttemptState,
    Cap,
    EscalationReason,
    Problem,
    ProblemType,
    Split,
    VerificationResult,
    Verifier,
)
from turing.research.solver import Solver, SolverError, SolverPolicy

from .conftest import (
    FakeClock,
    LadderBackend,
    ScriptedChannel,
    fresh_with_cap,
    verdict_continue,
    verdict_extend,
)

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import Attempt
    from turing.research.solver import InMemoryCheckpointStore, WorkspaceManager


def _solver(
    backend: object,
    store: object,
    manager: object,
    channel: object,
    clock: object,
    policy: SolverPolicy | None = None,
) -> Solver:
    return Solver(
        backend=backend,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        workspaces=manager,  # type: ignore[arg-type]
        escalations=channel,  # type: ignore[arg-type]
        policy=policy if policy is not None else SolverPolicy(),
        clock=clock,  # type: ignore[arg-type]
    )


class TestStepCap:
    async def test_the_solver_stops_at_exactly_max_steps(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = LadderBackend()
        channel = ScriptedChannel()
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=4, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )

        outcome = await _solver(backend, store, manager, channel, clock).run(attempt, problem)

        assert backend.calls == 4
        assert outcome.attempt.consumed.steps == 4
        assert outcome.attempt.cap_exhausted
        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.CAP_EXHAUSTED


class TestTokenCap:
    async def test_the_solver_stops_once_the_token_budget_is_spent(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = LadderBackend(tokens=100)
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=100, max_tokens=250, max_wall_clock_seconds=3600.0)
        )

        outcome = await _solver(backend, store, manager, ScriptedChannel(), clock).run(
            attempt, problem
        )

        assert backend.calls == 3  # 100, 200, 300 — the third crosses the line
        assert outcome.attempt.consumed.tokens == 300
        assert outcome.suspended


class TestWallClockCap:
    async def test_measured_wall_clock_trips_the_cap(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = LadderBackend(hook=lambda _ctx: clock.advance(2.0))
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=100, max_tokens=10_000, max_wall_clock_seconds=5.0)
        )

        outcome = await _solver(backend, store, manager, ScriptedChannel(), clock).run(
            attempt, problem
        )

        assert backend.calls == 3  # 2s, 4s, 6s
        assert outcome.attempt.consumed.wall_clock_seconds == pytest.approx(6.0)
        assert outcome.suspended

    async def test_a_timeout_charges_elapsed_time_not_the_remaining_budget(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """A short TimeoutError is not a write-off of whatever wall-clock is left.

        ``_deadline`` still cancels a hung call when remaining hits zero. The
        charge after TimeoutError is the measured elapsed time, so a one-second
        model timeout with ten seconds of budget left costs one second.
        """

        class TimingOutBackend:
            calls = 0

            async def propose(self, context: object) -> object:
                type(self).calls += 1
                clock.advance(1.0)
                raise TimeoutError("model call timed out")

        attempt = fresh_with_cap(
            attempt, Cap(max_steps=1, max_tokens=10_000, max_wall_clock_seconds=10.0)
        )

        outcome = await _solver(TimingOutBackend(), store, manager, ScriptedChannel(), clock).run(
            attempt, problem
        )

        assert TimingOutBackend.calls == 1
        assert outcome.attempt.consumed.steps == 1
        assert outcome.attempt.consumed.wall_clock_seconds == pytest.approx(1.0)
        assert outcome.suspended

    async def test_an_instant_apply_timeout_escalates_instead_of_retrying_forever(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Apply charges no step. Zero elapsed + a pending iteration livelocks.

        The cap gate skips while a journalled proposal is unfinished and
        wall-clock remains. An apply TimeoutError that measures nothing would
        return to that gate forever. Escalating is the honest stop: there is
        no durable cap progress to make.
        """

        class InstantTimeoutWorkspace:
            applies = 0

            def __init__(self, inner: object) -> None:
                self.path = inner.path  # type: ignore[attr-defined]

            async def apply(self, proposal: object) -> tuple[str, ...]:
                type(self).applies += 1
                if type(self).applies > 8:
                    raise AssertionError("apply TimeoutError livelocked")
                raise TimeoutError("apply timed out")

        class InstantTimeoutManager:
            def __init__(self, inner: WorkspaceManager) -> None:
                self._inner = inner

            async def prepare(self, problem: Problem, workspace_path: Path) -> object:
                return InstantTimeoutWorkspace(await self._inner.prepare(problem, workspace_path))

        channel = ScriptedChannel()
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=10, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )

        outcome = await asyncio.wait_for(
            _solver(
                LadderBackend(),
                store,
                InstantTimeoutManager(manager),
                channel,
                clock,
            ).run(attempt, problem),
            timeout=2.0,
        )

        assert InstantTimeoutWorkspace.applies == 1
        assert outcome.attempt.consumed.steps == 1
        assert outcome.attempt.consumed.wall_clock_seconds == pytest.approx(0.0)
        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
        assert "no elapsed time" in channel.requests[0].summary

    async def test_an_instant_verify_timeout_escalates_instead_of_retrying_forever(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Verify, like apply, charges no step. Same livelock, same stop."""

        @dataclass(frozen=True)
        class InstantTimeoutVerifier(Verifier):
            calls: list[int] = field(default_factory=list, compare=False)

            async def verify(self, workspace: Path) -> VerificationResult:
                self.calls.append(1)
                if len(self.calls) > 8:
                    raise AssertionError("verify TimeoutError livelocked")
                raise TimeoutError("verify timed out")

        verifier = InstantTimeoutVerifier(
            verifier_id=problem.verifier_id,
            problem_id=problem.id,
            description="times out instantly",
            score_scale=problem.verifier.score_scale,
        )
        timed_out = Problem(
            id=problem.id,
            problem_type=ProblemType.SPEEDUP,
            goal=problem.goal,
            workspace_template=problem.workspace_template,
            verifier=verifier,
            split=Split.PRACTICE,
        )
        channel = ScriptedChannel()
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=10, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )

        outcome = await asyncio.wait_for(
            _solver(LadderBackend(), store, manager, channel, clock).run(attempt, timed_out),
            timeout=2.0,
        )

        assert len(verifier.calls) == 1
        assert outcome.attempt.consumed.steps == 1
        assert outcome.attempt.consumed.wall_clock_seconds == pytest.approx(0.0)
        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
        assert "no elapsed time" in channel.requests[0].summary

    async def test_a_call_that_never_returns_costs_the_budget_not_the_night(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
    ) -> None:
        """The cap is enforced *during* an iteration, not only between them.

        Uses a real clock and a backend that hangs. Without the deadline the
        solver would sit on that call until the process was killed, and the
        cap would be a report rather than a brake.
        """

        class HangingBackend:
            calls = 0

            async def propose(self, context: object) -> object:
                type(self).calls += 1
                await asyncio.sleep(30)
                raise AssertionError("unreachable")

        channel = ScriptedChannel()
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=10, max_tokens=10_000, max_wall_clock_seconds=0.05)
        )

        started = time.monotonic()
        outcome = await Solver(
            backend=HangingBackend(),  # type: ignore[arg-type]
            store=store,
            workspaces=manager,
            escalations=channel,
        ).run(attempt, problem)
        elapsed = time.monotonic() - started

        assert elapsed < 5.0
        assert HangingBackend.calls == 1
        assert outcome.suspended
        assert channel.requests[0].reason is EscalationReason.CAP_EXHAUSTED
        assert outcome.attempt.consumed.wall_clock_seconds >= 0.05


class TestCapExtension:
    async def test_extend_cap_buys_exactly_the_budget_granted(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        backend = LadderBackend()
        channel = ScriptedChannel([verdict_extend(extra_steps=2)])
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=2, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )

        outcome = await _solver(backend, store, manager, channel, clock).run(attempt, problem)

        assert backend.calls == 4
        assert outcome.attempt.cap.max_steps == 4
        assert outcome.attempt.cap.extension_count == 1
        assert len(channel.requests) == 2  # the extension bought one more escalation
        assert outcome.suspended

    async def test_an_extension_too_small_to_help_lands_in_failed_within_cap(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Granting a token when steps are gone does not un-trip the brake."""
        backend = LadderBackend()
        channel = ScriptedChannel([verdict_extend(extra_tokens=1)])
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=2, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )

        outcome = await _solver(backend, store, manager, channel, clock).run(attempt, problem)

        assert backend.calls == 2
        assert outcome.state is AttemptState.FAILED_WITHIN_CAP

    async def test_continue_without_budget_resolves_to_failed_within_cap(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """No ping-pong: CONTINUE on an empty budget cannot re-escalate forever.

        "Failed within cap" is a first-class logged outcome, and it is the
        honest reading of "carry on" when there is nothing left to carry on
        with. An operator who wants more work says ``extend_cap``.
        """
        backend = LadderBackend()
        channel = ScriptedChannel([verdict_continue()])
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=2, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )

        outcome = await _solver(backend, store, manager, channel, clock).run(attempt, problem)

        assert outcome.state is AttemptState.FAILED_WITHIN_CAP
        assert outcome.terminal
        assert len(channel.requests) == 1
        assert backend.calls == 2


class TestFailWithinCapPolicy:
    async def test_the_cap_can_terminate_without_asking_a_human(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """Keeps escalations-per-round a measure of autonomy, not cap sizing."""
        channel = ScriptedChannel()
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=2, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )
        solver = _solver(
            LadderBackend(),
            store,
            manager,
            channel,
            clock,
            SolverPolicy(escalate_on_cap_exhausted=False),
        )

        outcome = await solver.run(attempt, problem)

        assert outcome.state is AttemptState.FAILED_WITHIN_CAP
        assert channel.requests == []

    async def test_failed_within_cap_requires_a_tripped_cap(
        self,
        attempt: Attempt,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        """The guard behind "the solver cannot quit".

        ``FAILED_WITHIN_CAP`` is a mechanical brake, so it refuses to engage
        when there is budget left. Without this guard it would be a perfectly
        serviceable self-quit transition.
        """
        solver = _solver(LadderBackend(), store, manager, ScriptedChannel(), clock)

        with pytest.raises(SolverError, match="no self-quit transition"):
            await solver._fail_within_cap(attempt)


class TestBudgetAccounting:
    async def test_consumption_only_ever_grows(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        seen: list[tuple[int, int]] = []

        def record(_ctx: object) -> None:
            clock.advance(0.5)

        backend = LadderBackend(tokens=25, hook=record)
        attempt = fresh_with_cap(
            attempt, Cap(max_steps=5, max_tokens=10_000, max_wall_clock_seconds=3600.0)
        )
        outcome = await _solver(backend, store, manager, ScriptedChannel(), clock).run(
            attempt, problem
        )

        for record_entry in await store.iterations(attempt.attempt_id):
            seen.append((record_entry.iteration_index, record_entry.proposal.tokens))

        assert seen == [(i, 25) for i in range(5)]
        assert outcome.attempt.consumed.steps == 5
        assert outcome.attempt.consumed.tokens == 125
