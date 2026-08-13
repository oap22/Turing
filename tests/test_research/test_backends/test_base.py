"""What every backend inherits: accounting on success *and* failure, resume.

These are the tests the cap depends on. They run against ``BaseBackend``
directly, because every implementation shares this code path — a backend cannot
opt out of being counted.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from turing.research.backends import (
    BackendIdentity,
    BaseBackend,
    DeterministicClock,
    GenerationRequest,
    ModelMessage,
    ModelTier,
    RawTurn,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    estimate_request_tokens,
    estimate_tokens,
    estimate_turn_output_tokens,
)
from turing.research.backends.errors import BackendCapacityError, BackendError, BackendResumeError

IDENTITY = BackendIdentity(backend="stub", orchestrator_model="big", substep_model="small")


class StubBackend(BaseBackend):
    """A backend whose provider call is whatever the test hands it."""

    def __init__(
        self,
        turn: RawTurn | BaseException,
        *,
        clock: DeterministicClock | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(identity=IDENTITY, clock=clock or DeterministicClock(step=0.25), **kwargs)  # type: ignore[arg-type]
        self._turn = turn
        self.invocations = 0

    async def _invoke(self, request: GenerationRequest) -> RawTurn:
        self.invocations += 1
        if isinstance(self._turn, BaseException):
            raise self._turn
        return self._turn


def make_request(**kwargs: object) -> GenerationRequest:
    return GenerationRequest(
        tier=ModelTier.ORCHESTRATOR,
        messages=(ModelMessage.user("solve it"),),
        **kwargs,  # type: ignore[arg-type]
    )


class TestRequestEstimation:
    def test_counts_system_messages_tools_and_results(self) -> None:
        bare = GenerationRequest(tier=ModelTier.SUBSTEP, messages=(ModelMessage.user("hello"),))
        loaded = GenerationRequest(
            tier=ModelTier.SUBSTEP,
            messages=(
                ModelMessage.user("hello"),
                ModelMessage.assistant(
                    "on it", tool_calls=[ToolCall(call_id="c1", name="bash", arguments={"c": "ls"})]
                ),
                ModelMessage.results([ToolResult(call_id="c1", content="a\nb\nc")]),
            ),
            system="you are a research agent",
            tools=(ToolSpec(name="bash", description="run a command", input_schema={"a": 1}),),
        )
        assert estimate_request_tokens(loaded) > estimate_request_tokens(bare)

    def test_includes_framing_overhead_a_naive_character_count_misses(self) -> None:
        """Message framing costs tokens without contributing characters.

        Estimating from text alone would under-count every multi-turn
        transcript, and long transcripts are the normal case here.
        """
        request = GenerationRequest(tier=ModelTier.SUBSTEP, messages=(ModelMessage.user("hi"),))
        assert estimate_request_tokens(request) > estimate_tokens("hi")

    def test_output_estimate_includes_tool_call_arguments(self) -> None:
        call = ToolCall(call_id="c1", name="bash", arguments={"cmd": "x" * 200})
        assert estimate_turn_output_tokens("", (call,)) > estimate_turn_output_tokens("", ())


class TestSuccessAccounting:
    async def test_uses_provider_figures_when_reported(self) -> None:
        backend = StubBackend(
            RawTurn(text="done", usage=Usage(input_tokens=1000, output_tokens=200))
        )
        response = await backend.generate(make_request())
        assert response.usage == Usage(input_tokens=1000, output_tokens=200)
        assert backend.ledger.consumption.tokens == 1200
        assert backend.ledger.estimated_steps == 0

    async def test_missing_usage_falls_back_to_an_estimate_not_to_zero(self) -> None:
        """A provider that reports nothing must not read as 'cost nothing'."""
        backend = StubBackend(RawTurn(text="done"))
        response = await backend.generate(make_request())
        assert response.usage.estimated is True
        assert response.usage.total_tokens > 0
        assert backend.ledger.estimated_steps == 1

    async def test_all_zero_usage_is_treated_as_absent(self) -> None:
        """No real call consumes nothing, so a zero is a reporting gap.

        Believing it would stop the cap advancing while the loop kept running —
        the exact silent failure this accounting is built to avoid.
        """
        backend = StubBackend(RawTurn(text="done", usage=Usage()))
        response = await backend.generate(make_request())
        assert response.usage.estimated is True
        assert response.usage.total_tokens > 0

    async def test_a_report_of_output_only_does_not_zero_the_prompt(self) -> None:
        """A partial report must not be read as a report about everything.

        Reporting generated tokens but not prompt tokens is the ordinary shape
        for a local runtime. If any single non-zero field counted as "the
        provider told us", one output token would stand in for an entire
        transcript — and in an agentic loop the prompt is one to two orders of
        magnitude larger than the output, so the cap would stop advancing while
        the loop kept running.
        """
        transcript = "x" * 400_000
        request = GenerationRequest(
            tier=ModelTier.ORCHESTRATOR,
            messages=(ModelMessage.user(transcript),),
        )
        backend = StubBackend(RawTurn(text="ok", usage=Usage(output_tokens=1)))
        response = await backend.generate(request)

        assert response.usage.output_tokens == 1, "the reported side is believed"
        assert response.usage.input_tokens == estimate_request_tokens(request)
        assert response.usage.estimated is True
        assert backend.ledger.consumption.tokens > 100_000

    async def test_a_report_of_prompt_only_still_charges_the_generated_side(self) -> None:
        """The mirror case: a runtime that counts what it read but not what it wrote."""
        backend = StubBackend(
            RawTurn(text="a much longer answer than the prompt", usage=Usage(input_tokens=7))
        )
        response = await backend.generate(make_request())

        assert response.usage.input_tokens == 7
        assert response.usage.output_tokens == estimate_turn_output_tokens(
            "a much longer answer than the prompt", ()
        )
        assert response.usage.estimated is True

    async def test_cache_classes_alone_count_as_a_reported_prompt(self) -> None:
        """A cached run reports its input under the cache classes, not under input.

        Estimating on top of that would double-charge the prompt of every
        cached call, which is most calls in a long run.
        """
        backend = StubBackend(
            RawTurn(text="done", usage=Usage(cache_read_tokens=9000, output_tokens=40))
        )
        response = await backend.generate(make_request())
        assert response.usage == Usage(cache_read_tokens=9000, output_tokens=40)
        assert response.usage.estimated is False

    async def test_retry_overhead_is_added_to_the_reported_figure(self) -> None:
        """Attempts that failed before the successful one still cost tokens."""
        backend = StubBackend(
            RawTurn(
                text="done",
                usage=Usage(input_tokens=100, output_tokens=10),
                extra_usage=Usage(input_tokens=250, estimated=True),
            )
        )
        response = await backend.generate(make_request())
        assert response.usage.total_tokens == 360
        assert response.usage.estimated is True

    async def test_records_a_step_and_wall_clock_per_call(self) -> None:
        backend = StubBackend(RawTurn(text="done", usage=Usage(input_tokens=10)))
        await backend.generate(make_request())
        await backend.generate(make_request())
        assert backend.ledger.consumption.steps == 2
        assert backend.ledger.consumption.wall_clock_seconds == pytest.approx(0.5)

    async def test_attributes_spend_to_the_requested_tier(self) -> None:
        backend = StubBackend(RawTurn(text="ok", usage=Usage(input_tokens=10, output_tokens=2)))
        await backend.generate(
            GenerationRequest(tier=ModelTier.SUBSTEP, messages=(ModelMessage.user("x"),))
        )
        assert backend.ledger.usage_for(ModelTier.SUBSTEP).total_tokens == 12
        assert backend.ledger.usage_for(ModelTier.ORCHESTRATOR).total_tokens == 0

    async def test_response_carries_the_serving_model_and_stop_reason(self) -> None:
        backend = StubBackend(
            RawTurn(
                text="",
                tool_calls=(ToolCall(call_id="c", name="bash"),),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(input_tokens=5),
                model="big-v2",
            )
        )
        response = await backend.generate(make_request())
        assert response.model == "big-v2"
        assert response.stop_reason is StopReason.TOOL_CALLS
        assert response.has_tool_calls

    async def test_an_unreported_model_falls_back_to_the_tier_that_was_asked_for(self) -> None:
        """Not every runtime echoes which model served a turn.

        Falling back to the orchestrator regardless of tier would record
        cheap-tier work under the expensive model, and the per-tier record is
        the only evidence that routing mundane work down actually happened —
        the whole cost argument rests on it.
        """
        backend = StubBackend(RawTurn(text="ok", usage=Usage(input_tokens=5, output_tokens=1)))
        substep = await backend.generate(
            GenerationRequest(tier=ModelTier.SUBSTEP, messages=(ModelMessage.user("x"),))
        )
        orchestrator = await backend.generate(make_request())

        assert substep.model == "small"
        assert orchestrator.model == "big"

    async def test_the_fallback_uses_the_orchestrator_when_there_is_no_cheap_tier(self) -> None:
        class SingleTier(StubBackend):
            def __init__(self) -> None:
                super().__init__(RawTurn(text="ok", usage=Usage(input_tokens=5, output_tokens=1)))
                self._identity = BackendIdentity(backend="stub", orchestrator_model="only")

        response = await SingleTier().generate(
            GenerationRequest(tier=ModelTier.SUBSTEP, messages=(ModelMessage.user("x"),))
        )
        assert response.model == "only"


class TestFailureAccounting:
    async def test_a_failed_call_still_costs_a_step(self) -> None:
        """Otherwise a loop of failing calls spins forever inside its cap."""
        backend = StubBackend(BackendError("nope"))
        with pytest.raises(BackendError):
            await backend.generate(make_request())
        assert backend.ledger.consumption.steps == 1

    async def test_a_failed_call_charges_an_estimated_input_cost(self) -> None:
        backend = StubBackend(BackendError("nope"))
        with pytest.raises(BackendError):
            await backend.generate(make_request())
        assert backend.ledger.consumption.tokens > 0
        assert backend.ledger.estimated_steps == 1

    async def test_a_failed_call_attaches_the_charged_usage_to_the_error(self) -> None:
        """Otherwise the runner treats a paid generate() as free."""
        backend = StubBackend(BackendError("nope"))
        with pytest.raises(BackendError) as caught:
            await backend.generate(make_request())
        assert caught.value.usage is not None
        assert caught.value.usage.estimated is True
        assert caught.value.usage.total_tokens == backend.ledger.consumption.tokens
        assert backend.ledger.consumption.tokens > 0

    async def test_usage_attached_to_the_error_is_preferred_over_an_estimate(self) -> None:
        backend = StubBackend(
            BackendCapacityError("rate limited", usage=Usage(input_tokens=4242, estimated=True))
        )
        with pytest.raises(BackendCapacityError):
            await backend.generate(make_request())
        assert backend.ledger.consumption.tokens == 4242

    async def test_a_non_backend_exception_is_still_counted(self) -> None:
        """A crash inside a provider SDK is not a free call."""
        backend = StubBackend(RuntimeError("sdk blew up"))
        with pytest.raises(RuntimeError) as caught:
            await backend.generate(make_request())
        assert backend.ledger.consumption.steps == 1
        assert backend.ledger.consumption.tokens > 0
        assert getattr(caught.value, "tokens", None) == backend.ledger.consumption.tokens

    async def test_a_foreign_usage_attribute_does_not_override_the_ledger_charge(self) -> None:
        """A crash whose object already has usage/tokens must still carry the ledger."""

        class SdkError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("sdk blew up")
                self.usage = Usage(input_tokens=1)
                self.tokens = 1

        request = make_request()
        backend = StubBackend(SdkError())
        with pytest.raises(SdkError) as caught:
            await backend.generate(request)
        charged = backend.ledger.consumption.tokens
        assert charged == estimate_request_tokens(request)
        assert charged > 1
        assert caught.value.usage.total_tokens == charged
        assert caught.value.tokens == charged

    async def test_a_read_only_usage_property_does_not_override_the_ledger_charge(self) -> None:
        """The ledger charge must ride out even when ``usage`` cannot be written."""

        class SdkError(RuntimeError):
            @property
            def usage(self) -> Usage:
                return Usage(input_tokens=0, output_tokens=1)

        request = make_request()
        backend = StubBackend(SdkError("sdk blew up"))
        with pytest.raises(BackendError) as caught:
            await backend.generate(request)
        charged = backend.ledger.consumption.tokens
        assert charged == estimate_request_tokens(request)
        assert charged > 1
        assert caught.value.usage is not None
        assert caught.value.usage.total_tokens == charged
        assert isinstance(caught.value.__cause__, SdkError)
        assert caught.value.__cause__.usage.total_tokens == 1

    async def test_a_rejecting_usage_setter_does_not_override_the_ledger_charge(self) -> None:
        """A setter that refuses the charge must not leak; wrap with the ledger."""

        class SdkError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("sdk blew up")
                self._usage = Usage(input_tokens=0, output_tokens=1)

            @property
            def usage(self) -> Usage:
                return self._usage

            @usage.setter
            def usage(self, value: Usage) -> None:
                if value.total_tokens != 1:
                    raise ValueError("usage is frozen at 1")
                self._usage = value

        request = make_request()
        backend = StubBackend(SdkError())
        with pytest.raises(BackendError) as caught:
            await backend.generate(request)
        charged = backend.ledger.consumption.tokens
        assert charged > 1
        assert caught.value.usage is not None
        assert caught.value.usage.total_tokens == charged
        assert isinstance(caught.value.__cause__, SdkError)
        assert caught.value.__cause__.usage.total_tokens == 1

    async def test_explicit_zero_usage_falls_back_to_an_estimate(self) -> None:
        """Usage() is unknown spend, not a free call."""
        request = make_request()
        backend = StubBackend(BackendError("nope", usage=Usage()))
        with pytest.raises(BackendError) as caught:
            await backend.generate(request)
        charged = backend.ledger.consumption.tokens
        assert charged == estimate_request_tokens(request)
        assert charged > 0
        assert caught.value.usage is not None
        assert caught.value.usage.estimated is True
        assert caught.value.usage.total_tokens == charged

    async def test_cancellation_mid_call_is_still_counted(self) -> None:
        """The window closing mid-attempt still spent the input it sent."""
        import asyncio

        backend = StubBackend(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await backend.generate(make_request())
        assert backend.ledger.consumption.steps == 1
        assert backend.ledger.consumption.tokens > 0

    async def test_failures_accumulate_rather_than_replacing(self) -> None:
        backend = StubBackend(BackendError("nope"))
        for _ in range(3):
            with pytest.raises(BackendError):
                await backend.generate(make_request())
        assert backend.ledger.consumption.steps == 3


class TestCheckpointAndResume:
    async def test_a_resumed_backend_continues_from_what_it_spent(self) -> None:
        """An interruption costs the remainder of an attempt, not the attempt."""
        first = StubBackend(RawTurn(text="a", usage=Usage(input_tokens=500, output_tokens=100)))
        await first.generate(make_request())
        token = first.checkpoint()

        second = StubBackend(RawTurn(text="b", usage=Usage(input_tokens=200, output_tokens=10)))
        second.restore(token)
        assert second.ledger.consumption == first.ledger.consumption

        await second.generate(make_request())
        assert second.ledger.consumption.tokens == 810
        assert second.ledger.consumption.steps == 2

    async def test_resuming_onto_a_different_engine_is_refused(self) -> None:
        """A round measured across two engine configurations is not a round.

        The engine identity rides in the token so drift fails loudly instead of
        contaminating the round's delta.
        """
        first = StubBackend(RawTurn(text="a", usage=Usage(input_tokens=10)))
        await first.generate(make_request())
        token = first.checkpoint()

        class OtherEngine(StubBackend):
            def __init__(self) -> None:
                super().__init__(RawTurn(text="b"))
                self._identity = BackendIdentity(
                    backend="stub", orchestrator_model="different", substep_model="small"
                )

        with pytest.raises(BackendResumeError, match="different engine"):
            OtherEngine().restore(token)

    async def test_restore_refuses_after_the_backend_has_already_spent(self) -> None:
        first = StubBackend(RawTurn(text="a", usage=Usage(input_tokens=10)))
        await first.generate(make_request())
        token = first.checkpoint()

        second = StubBackend(RawTurn(text="b", usage=Usage(input_tokens=10)))
        await second.generate(make_request())
        with pytest.raises(BackendError, match="already recorded"):
            second.restore(token)

    async def test_the_token_is_a_plain_string_contracts_never_inspects(self) -> None:
        backend = StubBackend(RawTurn(text="a", usage=Usage(input_tokens=10)))
        await backend.generate(make_request())
        assert isinstance(backend.checkpoint(), str)

    async def test_aclose_is_safe_by_default(self) -> None:
        await StubBackend(RawTurn(text="a")).aclose()


class TestResumeTokenTampering:
    """A rewritable token is a resettable cap.

    The token is the one sanctioned way spend crosses a process boundary. If
    its ledger can be edited and still accepted, an attempt resumes with its
    whole budget handed back — an unlimited cap reachable by editing a file,
    which is the failure the cap exists to prevent rather than a lesser one.
    """

    async def _spent_backend(self, secret: str | None = None) -> StubBackend:
        backend = StubBackend(
            RawTurn(text="a", usage=Usage(input_tokens=900, output_tokens=90)),
            resume_secret=secret,
        )
        await backend.generate(make_request())
        return backend

    @staticmethod
    def _zero_the_ledger(token: str) -> str:
        envelope = json.loads(token)
        body = json.loads(envelope["body"])
        body["ledger"] = {
            "steps": 0,
            "estimated_steps": 0,
            "provider_calls": 0,
            "wall_clock_seconds": 0.0,
            "per_tier": {},
        }
        envelope["body"] = json.dumps(body, sort_keys=True)
        return json.dumps(envelope, sort_keys=True)

    async def test_an_edited_ledger_is_refused_rather_than_resumed(self) -> None:
        spent = await self._spent_backend()
        forged = self._zero_the_ledger(spent.checkpoint())

        fresh = StubBackend(RawTurn(text="b"))
        with pytest.raises(BackendResumeError, match="integrity"):
            fresh.restore(forged)
        assert fresh.ledger.consumption.tokens == 0, "nothing was reinstated"

    async def test_a_resealed_forgery_fails_when_a_secret_is_configured(self) -> None:
        """Without a key, anyone who can rewrite the body can recompute the seal.

        With one, they cannot. This is the seam a self-editing agent's writable
        workspace attaches to: pass a secret its own process cannot read.
        """
        spent = await self._spent_backend(secret="operator-key")
        forged = self._zero_the_ledger(spent.checkpoint())
        envelope = json.loads(forged)
        # The attacker reseals with the only key they have: none.
        envelope["digest"] = hmac.new(
            b"", envelope["body"].encode("utf-8"), hashlib.sha256
        ).hexdigest()

        fresh = StubBackend(RawTurn(text="b"), resume_secret="operator-key")
        with pytest.raises(BackendResumeError, match="integrity"):
            fresh.restore(json.dumps(envelope, sort_keys=True))

    async def test_a_sealed_token_still_round_trips_on_the_same_secret(self) -> None:
        spent = await self._spent_backend(secret="operator-key")
        fresh = StubBackend(RawTurn(text="b"), resume_secret="operator-key")
        fresh.restore(spent.checkpoint())
        assert fresh.ledger.consumption == spent.ledger.consumption

    async def test_a_token_sealed_with_another_secret_is_refused(self) -> None:
        spent = await self._spent_backend(secret="operator-key")
        fresh = StubBackend(RawTurn(text="b"), resume_secret="a-different-key")
        with pytest.raises(BackendResumeError, match="integrity"):
            fresh.restore(spent.checkpoint())

    async def test_a_truncated_checkpoint_is_refused_rather_than_read_as_empty(self) -> None:
        """The accidental case: a checkpoint half-written when the process died."""
        spent = await self._spent_backend()
        token = spent.checkpoint()
        fresh = StubBackend(RawTurn(text="b"))
        with pytest.raises(BackendResumeError):
            fresh.restore(token[: len(token) // 2])


class TestProviderCallAccounting:
    """Requests shipped is a separate number from steps taken.

    The step count is the cap's brake and counts turns. A turn that shipped
    three requests is still one step — but if that is the only number recorded,
    a retry storm is invisible, and "one step" can quietly mean any number of
    requests.
    """

    async def test_a_plain_call_is_one_step_and_one_request(self) -> None:
        backend = StubBackend(RawTurn(text="a", usage=Usage(input_tokens=10, output_tokens=2)))
        await backend.generate(make_request())
        assert backend.ledger.steps == 1
        assert backend.ledger.provider_calls == 1

    async def test_a_retried_call_is_one_step_and_several_requests(self) -> None:
        backend = StubBackend(
            RawTurn(text="a", usage=Usage(input_tokens=10, output_tokens=2), provider_calls=3)
        )
        await backend.generate(make_request())
        assert backend.ledger.steps == 1
        assert backend.ledger.provider_calls == 3

    async def test_a_failure_reports_the_requests_it_burned(self) -> None:
        backend = StubBackend(BackendCapacityError("rate limited", provider_calls=3))
        with pytest.raises(BackendCapacityError):
            await backend.generate(make_request())
        assert backend.ledger.steps == 1
        assert backend.ledger.provider_calls == 3

    async def test_the_request_count_survives_a_checkpoint(self) -> None:
        first = StubBackend(
            RawTurn(text="a", usage=Usage(input_tokens=10, output_tokens=2), provider_calls=2)
        )
        await first.generate(make_request())
        second = StubBackend(RawTurn(text="b"))
        second.restore(first.checkpoint())
        assert second.ledger.provider_calls == 2


class TestWallClockSemantics:
    """The ledger's wall clock is model-call time, and only that.

    Recorded as a test because the property's name does not say so and the
    integration hazard is silent: for this agent nearly all elapsed time goes
    into training scripts and test suites, so substituting this figure for an
    attempt's elapsed time disables the wall-clock dimension of the cap without
    any visible failure.
    """

    async def test_it_counts_time_inside_calls_not_time_between_them(self) -> None:
        clock = DeterministicClock(step=0.25)
        backend = StubBackend(
            RawTurn(text="a", usage=Usage(input_tokens=10, output_tokens=2)), clock=clock
        )

        await backend.generate(make_request())
        # The caller goes off and runs a test suite for a simulated hour.
        for _ in range(2):
            clock()
        await backend.generate(make_request())

        assert backend.ledger.wall_clock_seconds == pytest.approx(0.5)
        assert backend.ledger.consumption.wall_clock_seconds == pytest.approx(0.5)
        assert clock() > backend.ledger.wall_clock_seconds, (
            "real elapsed time exceeds in-call time; the attempt's wall-clock "
            "dimension has to come from the caller's own timer"
        )
