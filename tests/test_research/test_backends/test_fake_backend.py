"""The test double itself.

It is worth testing because everything downstream will assert against it: if
the fake drifted from the real accounting path, every cap test built on it
would be measuring the wrong thing.
"""

from __future__ import annotations

import pytest

from turing.research.backends import (
    DeterministicClock,
    FakeBackend,
    GenerationRequest,
    ModelMessage,
    ModelTier,
    ScriptedTurn,
    StopReason,
    ToolCall,
    Usage,
)
from turing.research.backends.errors import BackendCapacityError, ScriptExhaustedError


def request(tier: ModelTier = ModelTier.ORCHESTRATOR, text: str = "go") -> GenerationRequest:
    return GenerationRequest(tier=tier, messages=(ModelMessage.user(text),))


class TestScripting:
    async def test_replays_turns_in_order(self) -> None:
        backend = FakeBackend.replying("first", "second")
        assert (await backend.generate(request())).text == "first"
        assert (await backend.generate(request())).text == "second"

    async def test_running_past_the_script_raises(self) -> None:
        """A test that makes more calls than it scripted has found a bug."""
        backend = FakeBackend.replying("only one")
        await backend.generate(request())
        with pytest.raises(ScriptExhaustedError, match="exhausted"):
            await backend.generate(request())

    async def test_an_empty_script_raises_on_first_use(self) -> None:
        with pytest.raises(ScriptExhaustedError):
            await FakeBackend([]).generate(request())

    async def test_looping_replays_the_script(self) -> None:
        backend = FakeBackend([ScriptedTurn(text="a"), ScriptedTurn(text="b")], loop=True)
        texts = [(await backend.generate(request())).text for _ in range(5)]
        assert texts == ["a", "b", "a", "b", "a"]

    async def test_scripted_errors_are_raised(self) -> None:
        backend = FakeBackend([ScriptedTurn(error=BackendCapacityError("window closed"))])
        with pytest.raises(BackendCapacityError, match="window closed"):
            await backend.generate(request())

    async def test_a_scripted_error_is_still_accounted_for(self) -> None:
        """Failure-path budget tests are written against this."""
        backend = FakeBackend([ScriptedTurn(error=BackendCapacityError("nope"))])
        with pytest.raises(BackendCapacityError):
            await backend.generate(request())
        assert backend.ledger.consumption.steps == 1
        assert backend.ledger.consumption.tokens > 0

    async def test_stop_reason_defaults_to_the_shape_of_the_turn(self) -> None:
        backend = FakeBackend(
            [
                ScriptedTurn(text="done"),
                ScriptedTurn(tool_calls=(ToolCall(call_id="c", name="bash"),)),
                ScriptedTurn(text="cut off", stop_reason=StopReason.MAX_TOKENS),
            ]
        )
        assert (await backend.generate(request())).stop_reason is StopReason.END_TURN
        assert (await backend.generate(request())).stop_reason is StopReason.TOOL_CALLS
        assert (await backend.generate(request())).stop_reason is StopReason.MAX_TOKENS


class TestDeterminism:
    async def test_identical_scripts_produce_identical_consumption(self) -> None:
        """No wall-clock flake: the clock is a counter, not a timer."""

        async def run() -> tuple[int, int, float]:
            backend = FakeBackend.replying("a", "b", "c")
            for _ in range(3):
                await backend.generate(request())
            consumed = backend.ledger.consumption
            return consumed.steps, consumed.tokens, consumed.wall_clock_seconds

        assert await run() == await run()

    async def test_the_clock_advances_a_fixed_amount_per_call(self) -> None:
        backend = FakeBackend.replying("a", "b", clock=DeterministicClock(step=0.5))
        await backend.generate(request())
        await backend.generate(request())
        assert backend.ledger.consumption.wall_clock_seconds == pytest.approx(1.0)


class TestObservability:
    async def test_records_every_request_including_its_tier(self) -> None:
        """Assert on this to prove mundane work really routed to the cheap tier."""
        backend = FakeBackend.replying("a", "b")
        await backend.generate(request(ModelTier.ORCHESTRATOR, "plan"))
        await backend.generate(request(ModelTier.SUBSTEP, "reformat"))

        assert [call.tier for call in backend.calls] == [
            ModelTier.ORCHESTRATOR,
            ModelTier.SUBSTEP,
        ]
        assert [call.messages[0].text for call in backend.calls] == ["plan", "reformat"]

    async def test_reports_the_model_matching_the_requested_tier(self) -> None:
        backend = FakeBackend.replying("a", "b")
        assert (await backend.generate(request(ModelTier.ORCHESTRATOR))).model == (
            "fake-orchestrator"
        )
        assert (await backend.generate(request(ModelTier.SUBSTEP))).model == "fake-substep"

    async def test_scripted_usage_is_used_verbatim_when_given(self) -> None:
        backend = FakeBackend(
            [ScriptedTurn(text="a", usage=Usage(input_tokens=1234, output_tokens=1))]
        )
        response = await backend.generate(request())
        assert response.usage.total_tokens == 1235
        assert response.usage.estimated is False

    async def test_omitted_usage_exercises_the_conservative_estimator(self) -> None:
        backend = FakeBackend([ScriptedTurn(text="a")])
        assert (await backend.generate(request())).usage.estimated is True

    async def test_remaining_reports_unconsumed_turns(self) -> None:
        backend = FakeBackend.replying("a", "b")
        assert backend.remaining == 2
        await backend.generate(request())
        assert backend.remaining == 1
