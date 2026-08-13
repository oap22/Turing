"""Seam invariants: the interface stays neutral, and the token carries state.

The seam only earns its keep if a second backend can implement it without the
solver noticing. Two properties do that work, and both are asserted here rather
than trusted: nothing vendor-specific appears in the interface, and a resume
token refuses to reinstate one engine's spend onto another.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path

import pytest

from turing.research.backends import (
    RESUME_TOKEN_VERSION,
    BackendIdentity,
    FakeBackend,
    GenerationRequest,
    LedgerSnapshot,
    LocalBackend,
    ModelBackend,
    ModelMessage,
    ModelTier,
    ResumeToken,
    Role,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from turing.research.backends.claude import ClaudeBackend
from turing.research.backends.errors import BackendProtocolError, BackendResumeError
from turing.research.backends.tiering import TieringPolicy
from turing.research.contracts import EngineIdentity

# Modules that make up the neutral seam. A vendor name appearing in any of them
# means the abstraction has leaked and a local backend can no longer implement
# the same thing.
NEUTRAL_MODULES = ("protocol.py", "tiering.py", "accounting.py", "errors.py", "base.py")

FORBIDDEN_TOKENS = (
    "anthropic",
    "claude",
    "opus",
    "haiku",
    "sonnet",
    "openai",
    "gpt-",
    "ollama",
    "mlx",
    "nemotron",
    "qwen",
)


class TestNeutrality:
    @pytest.mark.parametrize("module_name", NEUTRAL_MODULES)
    def test_no_vendor_names_in_the_seam(self, module_name: str) -> None:
        """Anything vendor-specific in the interface is a defect.

        The point of the seam is substitution. A field, name, or even an
        example that only one provider can honour turns "drop in a local
        backend" into a rewrite, so the check is mechanical rather than a
        matter of review taste.

        What this cannot see, stated so it is not mistaken for full cover: it
        greps for vendor *strings*, and the expensive leaks are vendor
        *assumptions* — that the provider always echoes a model name, always
        reports every token class, always tokenizes like English. Each of those
        passes this test cleanly and is covered by its own test elsewhere
        (``test_base``'s fallback and partial-usage cases, ``test_accounting``'s
        payload-shape ratios).
        """
        import turing.research.backends as pkg

        source = (Path(pkg.__file__).parent / module_name).read_text().lower()
        found = [token for token in FORBIDDEN_TOKENS if token in source]
        assert not found, f"{module_name} names vendors/models: {found}"

    def test_protocol_has_no_sampling_or_reasoning_knobs(self) -> None:
        """Recorded decisions, asserted so they are not quietly reversed.

        Sampling parameters are rejected outright by some current models, and
        reasoning control has no convertible cross-provider shape. Run-to-run
        variation is carried by the attempt seed and measured by the noise
        floor instead.
        """
        fields = set(GenerationRequest.__dataclass_fields__)
        assert fields.isdisjoint(
            {"temperature", "top_p", "top_k", "thinking", "reasoning", "effort", "stream"}
        )

    def test_every_backend_satisfies_the_protocol(self) -> None:
        claude = ClaudeBackend(
            client=object(),
            policy=TieringPolicy(orchestrator_model="big", substep_model="small"),
        )
        assert isinstance(FakeBackend([]), ModelBackend)
        assert isinstance(LocalBackend(), ModelBackend)
        assert isinstance(claude, ModelBackend)

    @pytest.mark.parametrize("method", ["generate", "checkpoint", "restore", "aclose"])
    def test_every_backend_matches_the_protocols_signatures(self, method: str) -> None:
        """``runtime_checkable`` only checks that a name exists.

        It says nothing about parameters, so a backend whose ``restore`` took
        different arguments would still pass an ``isinstance`` check and fail
        at the call. The substitution claim needs the shapes to match too.
        """
        expected = inspect.signature(getattr(ModelBackend, method))
        for implementation in (FakeBackend, LocalBackend, ClaudeBackend):
            actual = inspect.signature(getattr(implementation, method))
            assert list(actual.parameters) == list(expected.parameters), (
                f"{implementation.__name__}.{method} does not match the seam"
            )


class TestMessages:
    def test_only_an_assistant_turn_may_request_tools(self) -> None:
        call = ToolCall(call_id="c1", name="bash", arguments={"cmd": "ls"})
        with pytest.raises(BackendProtocolError):
            ModelMessage(role=Role.USER, text="hi", tool_calls=(call,))

    def test_only_a_user_turn_may_carry_tool_results(self) -> None:
        result = ToolResult(call_id="c1", content="ok")
        with pytest.raises(BackendProtocolError):
            ModelMessage(role=Role.ASSISTANT, text="hi", tool_results=(result,))

    def test_an_empty_message_is_rejected(self) -> None:
        with pytest.raises(BackendProtocolError):
            ModelMessage(role=Role.USER)

    def test_tool_calls_need_an_id_to_pair_results_with(self) -> None:
        with pytest.raises(BackendProtocolError):
            ToolCall(call_id="", name="bash")
        with pytest.raises(BackendProtocolError):
            ToolResult(call_id="", content="ok")

    def test_tool_arguments_are_copied_and_read_only(self) -> None:
        source = {"cmd": "ls"}
        call = ToolCall(call_id="c1", name="bash", arguments=source)
        source["cmd"] = "rm -rf /"
        assert call.arguments["cmd"] == "ls"
        with pytest.raises(TypeError):
            call.arguments["cmd"] = "mutated"  # type: ignore[index]

    def test_arguments_json_is_deterministic(self) -> None:
        first = ToolCall(call_id="c", name="t", arguments={"b": 1, "a": 2}).arguments_json()
        second = ToolCall(call_id="c", name="t", arguments={"a": 2, "b": 1}).arguments_json()
        assert first == second

    def test_there_is_no_system_role(self) -> None:
        """System text is a request field, not a turn.

        Providers disagree about whether it is a top-level parameter or a
        leading message; a field converts to either, a role does not convert
        back.
        """
        assert {role.value for role in Role} == {"user", "assistant"}


class TestGenerationRequest:
    def test_tier_has_no_default(self) -> None:
        """Routing down has to be a decision someone made.

        A default tier would let mundane work land on the expensive tier
        silently, which is exactly the spend the tier split exists to avoid.
        """
        field = GenerationRequest.__dataclass_fields__["tier"]
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING
        with pytest.raises(TypeError):
            GenerationRequest(messages=(ModelMessage.user("x"),))  # type: ignore[call-arg]

    def test_conversation_must_open_with_a_user_turn(self) -> None:
        with pytest.raises(BackendProtocolError):
            GenerationRequest(tier=ModelTier.SUBSTEP, messages=(ModelMessage.assistant("hello"),))

    def test_empty_transcript_is_rejected(self) -> None:
        with pytest.raises(BackendProtocolError):
            GenerationRequest(tier=ModelTier.SUBSTEP, messages=())

    def test_duplicate_tool_names_are_rejected(self) -> None:
        tool = ToolSpec(name="bash", description="run a command")
        with pytest.raises(BackendProtocolError):
            GenerationRequest(
                tier=ModelTier.SUBSTEP,
                messages=(ModelMessage.user("x"),),
                tools=(tool, tool),
            )

    def test_with_messages_preserves_everything_else(self) -> None:
        base = GenerationRequest(
            tier=ModelTier.SUBSTEP,
            messages=(ModelMessage.user("x"),),
            system="be terse",
            tools=(ToolSpec(name="bash", description="run"),),
            max_tokens=64,
            stop_sequences=("STOP",),
            request_id="r1",
        )
        extended = base.with_messages([*base.messages, ModelMessage.assistant("ok")])
        assert extended.messages[-1].text == "ok"
        assert extended.system == base.system
        assert extended.tools == base.tools
        assert extended.max_tokens == base.max_tokens
        assert extended.stop_sequences == base.stop_sequences
        assert extended.request_id == base.request_id


class TestTranscriptShape:
    """Transcripts no backend can serve are rejected here, not at request time.

    An API rejects them with a 400, which is survivable. A local chat template
    does not: it renders something malformed and returns plausible garbage,
    which is the worse outcome and precisely the one a neutral seam exists to
    prevent. Both futures are covered by refusing to build the transcript.
    """

    def _call(self, call_id: str = "c1") -> ToolCall:
        return ToolCall(call_id=call_id, name="bash", arguments={"cmd": "ls"})

    def test_consecutive_turns_from_the_same_speaker_are_rejected(self) -> None:
        with pytest.raises(BackendProtocolError, match="consecutive"):
            GenerationRequest(
                tier=ModelTier.ORCHESTRATOR,
                messages=(
                    ModelMessage.user("first"),
                    ModelMessage.user("second"),
                    ModelMessage.user("third"),
                ),
            )

    def test_a_tool_result_must_answer_a_call_that_was_actually_made(self) -> None:
        with pytest.raises(BackendProtocolError, match="never made"):
            GenerationRequest(
                tier=ModelTier.ORCHESTRATOR,
                messages=(
                    ModelMessage.user("go"),
                    ModelMessage.assistant("sure", tool_calls=[self._call()]),
                    ModelMessage.results([ToolResult(call_id="c-does-not-exist", content="ok")]),
                ),
            )

    def test_a_tool_result_with_no_preceding_call_at_all_is_rejected(self) -> None:
        """The transcript alternates properly; the assistant just never asked."""
        with pytest.raises(BackendProtocolError, match="never made"):
            GenerationRequest(
                tier=ModelTier.ORCHESTRATOR,
                messages=(
                    ModelMessage.user("go"),
                    ModelMessage.assistant("no tools needed"),
                    ModelMessage.results([ToolResult(call_id="c1", content="ok")]),
                ),
            )

    def test_every_requested_call_must_be_answered_before_continuing(self) -> None:
        with pytest.raises(BackendProtocolError, match="unanswered"):
            GenerationRequest(
                tier=ModelTier.ORCHESTRATOR,
                messages=(
                    ModelMessage.user("go"),
                    ModelMessage.assistant(
                        "two things", tool_calls=[self._call("c1"), self._call("c2")]
                    ),
                    ModelMessage.results([ToolResult(call_id="c1", content="ok")]),
                ),
            )

    def test_a_well_formed_tool_exchange_is_accepted(self) -> None:
        request = GenerationRequest(
            tier=ModelTier.ORCHESTRATOR,
            messages=(
                ModelMessage.user("go"),
                ModelMessage.assistant("sure", tool_calls=[self._call()]),
                ModelMessage.results([ToolResult(call_id="c1", content="ok")], text="and now?"),
            ),
        )
        assert len(request.messages) == 3

    def test_a_trailing_assistant_turn_is_allowed(self) -> None:
        """Prefill is a real capability, not a malformed transcript.

        The reviewer's third case. Constraining the opening of the model's
        reply by ending on an assistant turn is supported behaviour; rejecting
        it would remove a capability to satisfy a rule no backend imposes.
        """
        request = GenerationRequest(
            tier=ModelTier.ORCHESTRATOR,
            messages=(ModelMessage.user("write json"), ModelMessage.assistant('{"')),
        )
        assert request.messages[-1].role is Role.ASSISTANT

    def test_an_unanswered_call_in_the_final_turn_is_allowed(self) -> None:
        """The model asking for a tool is where a turn is *supposed* to end."""
        request = GenerationRequest(
            tier=ModelTier.ORCHESTRATOR,
            messages=(
                ModelMessage.user("go"),
                ModelMessage.assistant("sure", tool_calls=[self._call()]),
            ),
        )
        assert request.messages[-1].tool_calls


class TestGenerationResponse:
    def test_round_trips_into_a_transcript_turn(self) -> None:
        from turing.research.backends import GenerationResponse

        call = ToolCall(call_id="c1", name="bash", arguments={"cmd": "ls"})
        response = GenerationResponse(
            tier=ModelTier.ORCHESTRATOR,
            model="test-model",
            text="running it",
            tool_calls=(call,),
            stop_reason=StopReason.TOOL_CALLS,
            usage=Usage(input_tokens=1),
        )
        message = response.as_message()
        assert message.role is Role.ASSISTANT
        assert message.tool_calls == (call,)
        assert response.has_tool_calls


class TestBackendIdentity:
    def test_maps_onto_the_contract_level_engine_identity(self) -> None:
        """A round's delta is only attributable if the engine is recorded."""
        identity = BackendIdentity(backend="fake", orchestrator_model="big", substep_model="small")
        engine = identity.to_engine_identity("abc123")
        assert engine == EngineIdentity(
            backend="fake",
            orchestrator_model="big",
            substep_model="small",
            scaffold_git_sha="abc123",
        )

    def test_requires_a_name_and_an_orchestrator(self) -> None:
        with pytest.raises(BackendProtocolError):
            BackendIdentity(backend="", orchestrator_model="big")


class TestResumeToken:
    def _token(self) -> ResumeToken:
        return ResumeToken(
            backend="fake",
            orchestrator_model="big",
            substep_model="small",
            ledger=LedgerSnapshot(
                steps=3,
                wall_clock_seconds=1.5,
                per_tier={ModelTier.ORCHESTRATOR: Usage(input_tokens=100)},
            ),
        )

    def test_round_trips_through_a_string(self) -> None:
        token = self._token()
        assert ResumeToken.decode(token.encode()) == token

    def test_carries_the_spend_so_an_interruption_costs_the_remainder(self) -> None:
        decoded = ResumeToken.decode(self._token().encode())
        assert decoded.ledger.consumption.steps == 3
        assert decoded.ledger.consumption.tokens == 100

    def test_identity_match_is_exact(self) -> None:
        token = self._token()
        assert token.identity_matches(
            BackendIdentity(backend="fake", orchestrator_model="big", substep_model="small")
        )
        assert not token.identity_matches(
            BackendIdentity(backend="fake", orchestrator_model="big", substep_model="other")
        )

    def test_rejects_a_future_or_unversioned_format(self) -> None:
        payload = json.loads(self._token().encode())
        payload["version"] = RESUME_TOKEN_VERSION + 1
        with pytest.raises(BackendResumeError, match="version"):
            ResumeToken.decode(json.dumps(payload))

    def test_rejects_garbage_rather_than_reading_it_as_an_empty_budget(self) -> None:
        with pytest.raises(BackendResumeError):
            ResumeToken.decode("not json")
        with pytest.raises(BackendResumeError):
            ResumeToken.decode("[]")

    def test_an_unsealed_token_is_refused(self) -> None:
        """The pre-digest format is not readable, rather than readable and trusted.

        A token that carries a ledger and no proof of it is the whole hazard;
        accepting the old shape for compatibility would leave the hole open
        under a version number that claims it is closed.
        """
        legacy = json.dumps(
            {
                "version": RESUME_TOKEN_VERSION,
                "backend": "fake",
                "orchestrator_model": "big",
                "substep_model": "small",
                "ledger": LedgerSnapshot(steps=3, wall_clock_seconds=1.5, per_tier={}).to_dict(),
            }
        )
        with pytest.raises(BackendResumeError, match="body or digest"):
            ResumeToken.decode(legacy)

    def test_a_rewritten_ledger_does_not_survive_the_seal(self) -> None:
        """Editing the recorded spend re-grants the budget. It has to fail."""
        envelope = json.loads(self._token().encode())
        body = json.loads(envelope["body"])
        body["ledger"]["steps"] = 0
        body["ledger"]["per_tier"] = {}
        envelope["body"] = json.dumps(body, sort_keys=True)
        with pytest.raises(BackendResumeError, match="integrity"):
            ResumeToken.decode(json.dumps(envelope))

    def test_a_keyed_seal_round_trips_and_a_wrong_key_does_not(self) -> None:
        token = self._token()
        sealed = token.encode(secret="operator-key")
        assert ResumeToken.decode(sealed, secret="operator-key") == token
        with pytest.raises(BackendResumeError, match="integrity"):
            ResumeToken.decode(sealed, secret="another-key")
        with pytest.raises(BackendResumeError, match="integrity"):
            ResumeToken.decode(sealed)
