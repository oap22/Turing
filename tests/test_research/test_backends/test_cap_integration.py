"""End to end: backend spend drives a real cap and survives a real checkpoint.

Every other test in this package pins one piece. These wire the pieces to the
contracts layer the solver actually uses, because the failure that matters is
not "the ledger miscounted" — it is "the loop ran past its cap anyway".
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from turing.research.backends import (
    FakeBackend,
    GenerationRequest,
    ModelMessage,
    ModelTier,
    ScriptedTurn,
    StopReason,
    ToolCall,
    ToolResult,
    Usage,
)
from turing.research.contracts import (
    Attempt,
    AttemptState,
    Cap,
    CapDimension,
    CapExtension,
    EscalationDecision,
    EscalationVerdict,
)


def new_attempt(cap: Cap) -> Attempt:
    return Attempt(
        attempt_id="a1",
        problem_id="speedup-vault-index",
        round_id="r0",
        seed=7,
        workspace_path=Path("/tmp/ws"),
        cap=cap,
    )


def turn(text: str = "thinking") -> GenerationRequest:
    return GenerationRequest(tier=ModelTier.ORCHESTRATOR, messages=(ModelMessage.user(text),))


class TestCapEnforcement:
    async def test_backend_spend_exhausts_a_real_cap(self) -> None:
        cap = Cap(max_steps=10, max_tokens=1000, max_wall_clock_seconds=3600.0)
        attempt = new_attempt(cap)
        backend = FakeBackend(
            [ScriptedTurn(text="x", usage=Usage(input_tokens=400, output_tokens=100))],
            loop=True,
        )

        now = 0
        while not attempt.cap_exhausted:
            before = backend.ledger.consumption
            await backend.generate(turn())
            now += 1000
            delta = backend.ledger.consumption
            attempt = attempt.record_consumption(_difference(before, delta), now_ms=now)

        assert attempt.cap_exhausted
        assert CapDimension.TOKENS in attempt.exceeded_cap_dimensions
        assert attempt.consumed.tokens >= 1000

    async def test_a_loop_of_failing_calls_still_trips_the_step_cap(self) -> None:
        """The brake that catches a stuck loop is the step count.

        Failed calls produce no output tokens, so if failures were free the
        loop would spin indefinitely inside a token cap.
        """
        from turing.research.backends.errors import BackendProtocolError

        cap = Cap(max_steps=3, max_tokens=10_000_000, max_wall_clock_seconds=3600.0)
        attempt = new_attempt(cap)
        backend = FakeBackend([ScriptedTurn(error=BackendProtocolError("garbage"))], loop=True)

        now = 0
        for _ in range(3):
            before = backend.ledger.consumption
            with contextlib.suppress(BackendProtocolError):
                await backend.generate(turn())
            now += 1000
            attempt = attempt.record_consumption(
                _difference(before, backend.ledger.consumption), now_ms=now
            )

        assert attempt.cap_exhausted
        assert CapDimension.STEPS in attempt.exceeded_cap_dimensions

    async def test_an_operator_extension_buys_more_calls(self) -> None:
        """The only way a cap grows is an operator decision, and it works."""
        cap = Cap(max_steps=1, max_tokens=1_000_000, max_wall_clock_seconds=3600.0)
        attempt = new_attempt(cap)
        backend = FakeBackend([ScriptedTurn(text="x", usage=Usage(input_tokens=10))], loop=True)

        await backend.generate(turn())
        attempt = attempt.record_consumption(backend.ledger.consumption, now_ms=1000)
        assert attempt.cap_exhausted

        decision = EscalationDecision(
            request_id="e1",
            verdict=EscalationVerdict.EXTEND_CAP,
            decided_at_ms=2000,
            cap_extension=CapExtension(extra_steps=2),
        )
        attempt = attempt.apply_cap_extension(decision.cap_extension, now_ms=2000)  # type: ignore[arg-type]
        assert not attempt.cap_exhausted


class TestResumeAcrossAProcessBoundary:
    async def test_an_interrupted_attempt_resumes_with_its_spend_intact(self) -> None:
        """The whole point of the checkpoint.

        The interruption costs the remainder of the attempt, not the attempt:
        a resumed backend does not get its budget back.
        """
        cap = Cap(max_steps=10, max_tokens=2000, max_wall_clock_seconds=3600.0)
        attempt = new_attempt(cap)

        first = FakeBackend(
            [ScriptedTurn(text="a", usage=Usage(input_tokens=600, output_tokens=100))],
            loop=True,
        )
        await first.generate(turn())
        await first.generate(turn())
        attempt = attempt.record_consumption(first.ledger.consumption, now_ms=1000)

        # Window closes: checkpoint and drop the process.
        attempt = attempt.pause(now_ms=2000, resume_token=first.checkpoint())
        assert attempt.state is AttemptState.PAUSED
        assert attempt.resume_token is not None

        # New process, fresh backend, same engine.
        second = FakeBackend([ScriptedTurn(text="b", usage=Usage(input_tokens=100))], loop=True)
        second.restore(attempt.resume_token)
        attempt = attempt.resume(now_ms=3000)

        assert second.ledger.consumption == first.ledger.consumption
        assert attempt.state is AttemptState.RUNNING
        assert attempt.consumed.tokens == 1400

        await second.generate(turn())
        assert second.ledger.consumption.steps == 3

    async def test_the_checkpoint_survives_a_string_round_trip(self) -> None:
        """``Attempt.resume_token`` is a plain string field on a stored record."""
        backend = FakeBackend(
            [ScriptedTurn(text="a", usage=Usage(input_tokens=42, output_tokens=1))]
        )
        await backend.generate(turn())
        persisted = str(backend.checkpoint())

        restored = FakeBackend([])
        restored.restore(persisted)
        assert restored.ledger.consumption.tokens == 43


class TestToolLoopShape:
    async def test_a_tool_using_turn_round_trips_through_the_transcript(self) -> None:
        """The solver's inner loop, in miniature: ask, run a tool, feed it back."""
        call = ToolCall(call_id="c1", name="bash", arguments={"cmd": "pytest -q"})
        backend = FakeBackend(
            [
                ScriptedTurn(tool_calls=(call,), usage=Usage(input_tokens=100, output_tokens=20)),
                ScriptedTurn(text="all green", usage=Usage(input_tokens=150, output_tokens=10)),
            ]
        )

        request = turn("make the tests pass")
        first = await backend.generate(request)
        assert first.stop_reason is StopReason.TOOL_CALLS

        request = request.with_messages(
            [
                *request.messages,
                first.as_message(),
                ModelMessage.results(
                    [ToolResult(call_id=call.call_id, content="52 passed in 0.07s")]
                ),
            ]
        )
        second = await backend.generate(request)

        assert second.text == "all green"
        assert backend.ledger.consumption.steps == 2
        assert backend.ledger.consumption.tokens == 280
        assert backend.calls[1].messages[-1].tool_results[0].call_id == "c1"

    async def test_routing_mundane_work_down_shows_up_in_the_ledger(self) -> None:
        """The cost claim is measurable, not aspirational."""
        backend = FakeBackend(
            [
                ScriptedTurn(text="plan", usage=Usage(input_tokens=5000, output_tokens=500)),
                ScriptedTurn(text="{}", usage=Usage(input_tokens=200, output_tokens=20)),
                ScriptedTurn(text="{}", usage=Usage(input_tokens=200, output_tokens=20)),
            ]
        )
        await backend.generate(turn())
        for _ in range(2):
            await backend.generate(
                GenerationRequest(
                    tier=ModelTier.SUBSTEP, messages=(ModelMessage.user("reformat this"),)
                )
            )

        assert backend.ledger.usage_for(ModelTier.ORCHESTRATOR).total_tokens == 5500
        assert backend.ledger.usage_for(ModelTier.SUBSTEP).total_tokens == 440


def _difference(before, after):  # type: ignore[no-untyped-def]
    """Consumption added between two ledger readings."""
    from turing.research.contracts import CapConsumption

    return CapConsumption(
        steps=after.steps - before.steps,
        tokens=after.tokens - before.tokens,
        wall_clock_seconds=after.wall_clock_seconds - before.wall_clock_seconds,
    )
