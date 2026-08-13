"""The Claude backend, driven entirely through an injected fake SDK client.

No network call is made anywhere in this file. The two properties worth the
most here are the ones the rest of the system trusts silently: mundane work
really does route to the cheap model, and every token class the provider
reports really does reach the cap.
"""

from __future__ import annotations

from typing import Any

import pytest

from turing.research.backends import (
    GenerationRequest,
    ModelMessage,
    ModelTier,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    estimate_request_tokens,
)
from turing.research.backends.claude import (
    DEFAULT_ORCHESTRATOR_MODEL,
    DEFAULT_SUBSTEP_MODEL,
    SDK_TRANSPORT_RETRIES,
    BackendSettings,
    ClaudeBackend,
    is_context_length_error,
    is_retryable_error,
)
from turing.research.backends.errors import (
    BackendCapacityError,
    BackendConfigurationError,
    BackendContextLengthError,
    BackendProtocolError,
)
from turing.research.backends.tiering import TieringPolicy

from .conftest import (
    FakeAnthropicClient,
    FakeConnectionError,
    FakeMessage,
    FakeStatusError,
    FakeTextBlock,
    FakeToolUseBlock,
    FakeUnknownBlock,
    FakeUsage,
    noop_sleep,
)


def build(client: FakeAnthropicClient, policy: TieringPolicy, **kwargs: Any) -> ClaudeBackend:
    return ClaudeBackend(
        client=client,
        policy=policy,
        sleep=noop_sleep,
        retry_base_delay_seconds=0.0,
        **kwargs,
    )


def text_response(text: str = "ok", **kwargs: Any) -> FakeMessage:
    return FakeMessage(
        content=[FakeTextBlock(text=text)],
        usage=FakeUsage(input_tokens=10, output_tokens=5),
        **kwargs,
    )


class TestTiering:
    async def test_each_tier_routes_to_its_own_model(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        """The cheap tier is the whole cost argument; it has to actually be used."""
        client = FakeAnthropicClient([text_response(), text_response()])
        backend = build(client, policy)

        await backend.generate(request_factory(tier=ModelTier.ORCHESTRATOR))
        await backend.generate(request_factory(tier=ModelTier.SUBSTEP))

        models = [call["model"] for call in client.messages.calls]
        assert models == ["test-orchestrator", "test-substep"]

    async def test_each_tier_gets_its_own_output_ceiling(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        client = FakeAnthropicClient([text_response(), text_response()])
        backend = build(client, policy)

        await backend.generate(request_factory(tier=ModelTier.ORCHESTRATOR))
        await backend.generate(request_factory(tier=ModelTier.SUBSTEP))

        assert [call["max_tokens"] for call in client.messages.calls] == [4000, 200]

    async def test_an_explicit_max_tokens_overrides_the_policy(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        client = FakeAnthropicClient([text_response()])
        backend = build(client, policy)
        await backend.generate(request_factory(max_tokens=77))
        assert client.messages.calls[0]["max_tokens"] == 77

    async def test_effort_is_sent_only_when_the_policy_sets_it(self, request_factory: Any) -> None:
        """Not every model accepts an effort hint, so silence is the default."""
        quiet = TieringPolicy(orchestrator_model="big", substep_model="small")
        client = FakeAnthropicClient([text_response()])
        await build(client, quiet).generate(request_factory())
        assert "output_config" not in client.messages.calls[0]

        loud = TieringPolicy(
            orchestrator_model="big", substep_model="small", orchestrator_effort="high"
        )
        client2 = FakeAnthropicClient([text_response()])
        await build(client2, loud).generate(request_factory())
        assert client2.messages.calls[0]["output_config"] == {"effort": "high"}

    def test_the_identity_names_both_tiers_for_the_round_record(
        self, policy: TieringPolicy
    ) -> None:
        backend = build(FakeAnthropicClient(), policy)
        engine = backend.identity.to_engine_identity("sha")
        assert engine.orchestrator_model == "test-orchestrator"
        assert engine.substep_model == "test-substep"
        assert engine.backend == "claude"


class TestRequestConversion:
    async def test_system_and_tools_are_only_sent_when_present(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        client = FakeAnthropicClient([text_response(), text_response()])
        backend = build(client, policy)

        await backend.generate(request_factory())
        assert "system" not in client.messages.calls[0]
        assert "tools" not in client.messages.calls[0]

        await backend.generate(
            request_factory(
                system="be terse",
                tools=(ToolSpec(name="bash", description="run", input_schema={"type": "object"}),),
                stop_sequences=("STOP",),
            )
        )
        sent = client.messages.calls[1]
        assert sent["system"] == "be terse"
        assert sent["tools"] == [
            {"name": "bash", "description": "run", "input_schema": {"type": "object"}}
        ]
        assert sent["stop_sequences"] == ["STOP"]

    async def test_tool_results_lead_their_turn(self, policy: TieringPolicy) -> None:
        """The provider requires tool results first in a user turn."""
        client = FakeAnthropicClient([text_response()])
        backend = build(client, policy)
        request = GenerationRequest(
            tier=ModelTier.SUBSTEP,
            messages=(
                ModelMessage.user("run it"),
                ModelMessage.assistant(
                    "sure",
                    tool_calls=[ToolCall(call_id="c1", name="bash", arguments={"cmd": "ls"})],
                ),
                ModelMessage.results(
                    [ToolResult(call_id="c1", content="a\nb", is_error=False)],
                    text="here you go",
                ),
            ),
        )
        await backend.generate(request)

        messages = client.messages.calls[0]["messages"]
        assert messages[1]["content"][0]["type"] == "text"
        assert messages[1]["content"][1] == {
            "type": "tool_use",
            "id": "c1",
            "name": "bash",
            "input": {"cmd": "ls"},
        }
        assert messages[2]["content"][0]["type"] == "tool_result"
        assert messages[2]["content"][0]["tool_use_id"] == "c1"
        assert messages[2]["content"][1]["type"] == "text"

    async def test_tool_errors_are_flagged_back_to_the_model(self, policy: TieringPolicy) -> None:
        client = FakeAnthropicClient([text_response()])
        backend = build(client, policy)
        await backend.generate(
            GenerationRequest(
                tier=ModelTier.SUBSTEP,
                messages=(
                    ModelMessage.user("go"),
                    ModelMessage.assistant(
                        "trying", tool_calls=[ToolCall(call_id="c1", name="bash")]
                    ),
                    ModelMessage.results([ToolResult(call_id="c1", content="boom", is_error=True)]),
                ),
            )
        )
        block = client.messages.calls[0]["messages"][2]["content"][0]
        assert block["is_error"] is True


class TestResponseParsing:
    async def test_text_and_tool_calls_are_both_extracted(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        client = FakeAnthropicClient(
            [
                FakeMessage(
                    content=[
                        FakeTextBlock(text="thinking about it"),
                        FakeToolUseBlock(id="c1", name="bash", input={"cmd": "ls"}),
                    ],
                    stop_reason="tool_use",
                    model="test-orchestrator",
                    usage=FakeUsage(input_tokens=10, output_tokens=5),
                )
            ]
        )
        response = await build(client, policy).generate(request_factory())
        assert response.text == "thinking about it"
        assert response.tool_calls[0].name == "bash"
        assert response.tool_calls[0].arguments == {"cmd": "ls"}
        assert response.stop_reason is StopReason.TOOL_CALLS

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("end_turn", StopReason.END_TURN),
            ("tool_use", StopReason.TOOL_CALLS),
            ("max_tokens", StopReason.MAX_TOKENS),
            ("stop_sequence", StopReason.STOP_SEQUENCE),
            ("refusal", StopReason.REFUSAL),
            ("model_context_window_exceeded", StopReason.CONTEXT_OVERFLOW),
            ("pause_turn", StopReason.PAUSE),
            ("something_new", StopReason.OTHER),
            (None, StopReason.OTHER),
        ],
    )
    async def test_stop_reasons_are_normalised(
        self, policy: TieringPolicy, request_factory: Any, raw: str | None, expected: StopReason
    ) -> None:
        """The solver never sees a raw provider vocabulary."""
        client = FakeAnthropicClient(
            [
                FakeMessage(
                    content=[FakeTextBlock(text="x")],
                    stop_reason=raw,  # type: ignore[arg-type]
                    usage=FakeUsage(input_tokens=1, output_tokens=1),
                )
            ]
        )
        response = await build(client, policy).generate(request_factory())
        assert response.stop_reason is expected

    async def test_a_contentless_response_is_a_protocol_error(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        client = FakeAnthropicClient([object()])
        with pytest.raises(BackendProtocolError):
            await build(client, policy).generate(request_factory())

    async def test_context_overflow_is_distinguishable_from_an_unknown_reason(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        """Overflow has an available recovery; "something new" does not.

        Flattened together the caller cannot tell "compact the transcript and
        retry" from "the provider said a word we do not know" — and for a
        small-context runtime overflow is the common case, not the exotic one.
        """
        client = FakeAnthropicClient(
            [
                FakeMessage(
                    content=[FakeTextBlock(text="partial")],
                    stop_reason="model_context_window_exceeded",
                    usage=FakeUsage(input_tokens=1, output_tokens=1),
                ),
                FakeMessage(
                    content=[FakeTextBlock(text="partial")],
                    stop_reason="a_reason_invented_next_year",
                    usage=FakeUsage(input_tokens=1, output_tokens=1),
                ),
            ]
        )
        backend = build(client, policy)
        assert (
            await backend.generate(request_factory())
        ).stop_reason is StopReason.CONTEXT_OVERFLOW
        assert (await backend.generate(request_factory())).stop_reason is StopReason.OTHER

    async def test_a_turn_of_only_ignorable_blocks_fails_at_the_call(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        """A response nothing can be built from must fail where it was made.

        Reasoning blocks, provider-side tool activity, and block types that do
        not exist yet are all dropped by the parser. A response made only of
        them parses into a turn with no text and no tool calls, which no
        transcript can hold — and returning it defers the failure to whatever
        line appends it, where the error names neither the call nor the cause.
        """
        client = FakeAnthropicClient(
            [
                FakeMessage(
                    content=[FakeUnknownBlock(type="thinking")],
                    stop_reason="max_tokens",
                    usage=FakeUsage(input_tokens=8000, output_tokens=600),
                )
            ]
        )
        backend = build(client, policy)
        with pytest.raises(BackendProtocolError, match="neither text nor tool calls"):
            await backend.generate(request_factory())

    async def test_an_unusable_turn_is_still_charged_what_it_actually_cost(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        """The request succeeded and the provider reported its cost. Charge that.

        Falling back to an estimate here would replace a real 8,600-token
        figure with a guess about a one-line prompt.
        """
        client = FakeAnthropicClient(
            [
                FakeMessage(
                    content=[FakeUnknownBlock(type="thinking")],
                    usage=FakeUsage(input_tokens=8000, output_tokens=600),
                )
            ]
        )
        backend = build(client, policy)
        with pytest.raises(BackendProtocolError):
            await backend.generate(request_factory())
        assert backend.ledger.consumption.tokens == 8600

    async def test_empty_text_blocks_do_not_become_stray_newlines(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        client = FakeAnthropicClient(
            [
                FakeMessage(
                    content=[FakeTextBlock(text=""), FakeTextBlock(text="the answer")],
                    usage=FakeUsage(input_tokens=1, output_tokens=1),
                )
            ]
        )
        response = await build(client, policy).generate(request_factory())
        assert response.text == "the answer"


class TestUsageExtraction:
    async def test_cache_tokens_reach_the_cap(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        """Dropping cache classes is the largest under-count available.

        A long cached agentic run is mostly cache reads; counting only
        input+output would hide most of its input from the brake.
        """
        client = FakeAnthropicClient(
            [
                FakeMessage(
                    content=[FakeTextBlock(text="x")],
                    usage=FakeUsage(
                        input_tokens=10,
                        output_tokens=20,
                        cache_creation_input_tokens=300,
                        cache_read_input_tokens=4000,
                    ),
                )
            ]
        )
        backend = build(client, policy)
        response = await backend.generate(request_factory())
        assert response.usage == Usage(
            input_tokens=10,
            output_tokens=20,
            cache_creation_tokens=300,
            cache_read_tokens=4000,
        )
        assert backend.ledger.consumption.tokens == 4330

    async def test_a_response_without_usage_is_estimated_not_zeroed(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        client = FakeAnthropicClient([FakeMessage(content=[FakeTextBlock(text="x")], usage=None)])
        backend = build(client, policy)
        response = await backend.generate(request_factory())
        assert response.usage.estimated is True
        assert backend.ledger.consumption.tokens > 0


class TestRetriesAndFailures:
    @pytest.mark.parametrize("status", [408, 429, 500, 503, 529])
    def test_transient_statuses_are_retryable(self, status: int) -> None:
        assert is_retryable_error(FakeStatusError(status))

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_request_problems_are_not_retryable(self, status: int) -> None:
        assert not is_retryable_error(FakeStatusError(status))

    def test_transport_failures_are_retryable(self) -> None:
        assert is_retryable_error(FakeConnectionError("dropped"))
        assert is_retryable_error(TimeoutError())
        assert not is_retryable_error(ValueError("bad input"))

    async def test_a_transient_failure_is_retried_and_still_charged(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        """A retried call spent its input twice; the cap must see both."""
        client = FakeAnthropicClient([FakeStatusError(429), text_response()])
        backend = build(client, policy)
        response = await backend.generate(request_factory())

        assert response.text == "ok"
        assert len(client.messages.calls) == 2
        # 15 reported by the successful call, plus an estimate for the rejected one.
        assert backend.ledger.consumption.tokens > 15
        assert backend.ledger.total_usage().estimated is True
        # Retries are one solver step, not two — but two requests, and the
        # ledger says so rather than leaving the second one invisible.
        assert backend.ledger.consumption.steps == 1
        assert backend.ledger.provider_calls == 2

    async def test_a_fatal_error_after_a_retry_charges_both_requests(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        """The failing attempt shipped its input too.

        Charging only the accumulated retry overhead drops exactly one
        request's worth — the one that actually raised — because the accounting
        layer prefers usage attached to an error over its own estimate. Two
        requests went out; two must be charged.
        """
        long_prompt = "x" * 30_000
        client = FakeAnthropicClient([FakeStatusError(429), FakeStatusError(400)])
        backend = build(client, policy)
        with pytest.raises(BackendProtocolError):
            await backend.generate(request_factory(text=long_prompt))

        assert len(client.messages.calls) == 2
        one_request = estimate_request_tokens(
            GenerationRequest(
                tier=ModelTier.ORCHESTRATOR, messages=(ModelMessage.user(long_prompt),)
            )
        )
        assert backend.ledger.consumption.tokens == 2 * one_request
        assert backend.ledger.provider_calls == 2

    async def test_an_over_long_request_is_its_own_error(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        """ "Compact and retry" is only reachable if it is distinguishable.

        Arriving as an undifferentiated protocol error, the caller retries the
        same over-long transcript or abandons a problem it could still solve.
        """
        client = FakeAnthropicClient(
            [FakeStatusError(400, "prompt is too long: 250000 tokens > 200000 maximum")]
        )
        backend = build(client, policy)
        with pytest.raises(BackendContextLengthError):
            await backend.generate(request_factory())

    async def test_an_ordinary_bad_request_is_not_read_as_context_overflow(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        client = FakeAnthropicClient([FakeStatusError(400, "unexpected tool_use_id")])
        backend = build(client, policy)
        with pytest.raises(BackendProtocolError) as caught:
            await backend.generate(request_factory())
        assert not isinstance(caught.value, BackendContextLengthError)

    def test_a_rate_limit_quoting_a_context_word_is_still_a_rate_limit(self) -> None:
        """Classification is a text match, so it must not widen past its status."""
        assert not is_context_length_error(FakeStatusError(429, "context limit backoff"))
        assert is_context_length_error(FakeStatusError(400, "prompt is too long"))

    async def test_exhausted_retries_raise_a_capacity_error(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        """Capacity is a distinct outcome: pause and checkpoint, do not fail.

        The budget window closing says nothing about whether the problem was
        solvable, so it must not be recorded as a failed attempt.
        """
        client = FakeAnthropicClient([FakeStatusError(429) for _ in range(3)])
        backend = build(client, policy, max_retries=3)
        with pytest.raises(BackendCapacityError):
            await backend.generate(request_factory())
        assert len(client.messages.calls) == 3
        assert backend.ledger.consumption.steps == 1
        assert backend.ledger.consumption.tokens > 0
        assert backend.ledger.provider_calls == 3

    async def test_a_bad_request_fails_immediately(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        client = FakeAnthropicClient([FakeStatusError(400)])
        backend = build(client, policy)
        with pytest.raises(BackendProtocolError):
            await backend.generate(request_factory())
        assert len(client.messages.calls) == 1

    async def test_bad_credentials_are_a_configuration_error(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        client = FakeAnthropicClient([FakeStatusError(401)])
        with pytest.raises(BackendConfigurationError):
            await build(client, policy).generate(request_factory())

    async def test_a_first_attempt_failure_is_estimated_rather_than_zeroed(
        self, policy: TieringPolicy, request_factory: Any
    ) -> None:
        """Nothing was spent *that we know of*, which is not the same as zero."""
        client = FakeAnthropicClient([FakeStatusError(400)])
        backend = build(client, policy)
        with pytest.raises(BackendProtocolError):
            await backend.generate(request_factory())
        assert backend.ledger.consumption.tokens > 0
        assert backend.ledger.estimated_steps == 1

    def test_max_retries_must_be_at_least_one(self, policy: TieringPolicy) -> None:
        with pytest.raises(BackendConfigurationError):
            ClaudeBackend(client=FakeAnthropicClient(), policy=policy, max_retries=0)


class TestSettings:
    def test_defaults_name_a_cheap_tier_distinct_from_the_orchestrator(self) -> None:
        settings = BackendSettings(_env_file=None)
        assert settings.orchestrator_model == DEFAULT_ORCHESTRATOR_MODEL
        assert settings.substep_model == DEFAULT_SUBSTEP_MODEL
        assert settings.orchestrator_model != settings.substep_model

    def test_reads_the_research_prefixed_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TURING_RESEARCH_ORCHESTRATOR_MODEL", "custom-big")
        monkeypatch.setenv("TURING_RESEARCH_SUBSTEP_MODEL", "custom-small")
        settings = BackendSettings(_env_file=None)
        assert settings.tiering_policy().model_for(ModelTier.ORCHESTRATOR) == "custom-big"
        assert settings.tiering_policy().model_for(ModelTier.SUBSTEP) == "custom-small"

    def test_falls_back_to_the_assistants_existing_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TURING_ANTHROPIC_API_KEY", "shared-key")
        assert BackendSettings(_env_file=None).anthropic_api_key == "shared-key"

    def test_an_empty_key_is_allowed(self) -> None:
        """The operator runs on a subscription, not necessarily on a metered key."""
        assert BackendSettings(_env_file=None).anthropic_api_key == ""


class TestTransportRetries:
    """The SDK retries beneath this backend unless told not to.

    Its default is two internal retries, so one ``create`` call can ship three
    requests without the backend seeing any of them. Layered under this
    backend's own retry loop that is up to nine requests charged as one step
    and at most three input estimates — the same under-count this module
    exists to avoid, arriving one layer lower where no test of the retry logic
    can reach it.
    """

    def test_the_production_client_disables_transport_level_retrying(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anthropic

        recorded: list[dict[str, Any]] = []

        class RecordingClient:
            def __init__(self, **kwargs: Any) -> None:
                recorded.append(kwargs)

        monkeypatch.setattr(anthropic, "AsyncAnthropic", RecordingClient)

        ClaudeBackend.from_settings(BackendSettings(_env_file=None))
        ClaudeBackend.from_settings(BackendSettings(_env_file=None, anthropic_api_key="k"))

        assert [kwargs["max_retries"] for kwargs in recorded] == [0, 0]

    def test_the_real_sdk_client_honours_that_keyword(self) -> None:
        """Guards the keyword itself: a rename would silently restore the hole.

        Constructs the real client — no request is made — because asserting
        against a recording double proves only that this test and the code
        agree on a name.
        """
        import anthropic

        assert (
            anthropic.AsyncAnthropic(api_key="k", max_retries=SDK_TRANSPORT_RETRIES).max_retries
            == 0
        )

    def test_the_default_the_sdk_would_have_used_is_not_zero(self) -> None:
        """The reason the keyword is passed at all, asserted rather than assumed."""
        import anthropic

        assert anthropic.AsyncAnthropic(api_key="k").max_retries > 0


class TestLifecycle:
    async def test_aclose_closes_the_injected_client(self, policy: TieringPolicy) -> None:
        client = FakeAnthropicClient()
        await build(client, policy).aclose()
        assert client.closed is True

    async def test_aclose_tolerates_a_client_without_a_closer(self, policy: TieringPolicy) -> None:
        await build(FakeAnthropicClient(), policy).aclose()
        backend = ClaudeBackend(client=object(), policy=policy)
        await backend.aclose()
