"""The model-backend seam: send messages plus tool definitions, get a response.

This is the whole interface. It is provider-neutral on purpose, and
**anything vendor-specific appearing here is a defect** — the point of the
seam is that a second backend, running locally, implements exactly this and
nothing else changes. A field that only one provider can honour would make
that substitution a rewrite instead of a swap.

What that neutrality cost, recorded so it is not silently re-litigated:

* **No sampling parameters.** No temperature, top-p, or top-k. Some current
  frontier models reject them outright, and the ones that accept them do not
  agree on semantics. Run-to-run variation in this system is carried by
  :attr:`~turing.research.contracts.Attempt.seed` and measured by the noise
  floor, not steered by a knob only some backends have.
* **No reasoning/thinking control.** Providers disagree about whether that is
  a boolean, a token budget, or an effort level, and the shapes are not
  convertible. A per-tier hint lives on
  :class:`~turing.research.backends.tiering.TieringPolicy` instead, where a
  backend translates it or ignores it. If this ever moves onto the request, it
  must arrive as a neutral enum, never as one provider's block.
* **No streaming.** The solver consumes whole turns; nothing renders tokens to
  a human. Streaming would double the surface every backend has to implement
  for no gain in loop 1.
* **System text is a field, not a role.** Some providers take it as a
  top-level parameter and others as a leading message; a field converts to
  either, a role does not convert back.

The conversation is stateless. The transcript *is* the state, which is what
makes resumption cheap: :meth:`ModelBackend.checkpoint` has only the ledger and
the engine identity to carry, and the messages come back from the attempt's own
persisted record.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from turing.research.backends.accounting import LedgerSnapshot, Usage, UsageLedger
from turing.research.backends.errors import BackendProtocolError, BackendResumeError
from turing.research.backends.tiering import ModelTier
from turing.research.contracts import EngineIdentity

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Version stamped into resume tokens, so a format change is detectable rather
#: than silently misread as a zeroed budget.
#:
#: Version 2 added the integrity digest; a version-1 token is refused rather
#: than accepted unauthenticated.
RESUME_TOKEN_VERSION = 2


class Role(str, Enum):  # noqa: UP042
    """Who authored a message.

    There is no ``SYSTEM`` member: system text is a field on the request, not a
    turn in the transcript. See the module docstring.
    """

    USER = "user"
    ASSISTANT = "assistant"


class StopReason(str, Enum):  # noqa: UP042
    """Why a turn ended, normalised across providers.

    Providers use different vocabularies for the same handful of outcomes.
    Backends map onto these members so the solver never branches on a raw
    provider string; anything unrecognised becomes :attr:`OTHER` rather than
    leaking through.

    :attr:`CONTEXT_OVERFLOW` and :attr:`PAUSE` are separate members rather than
    two more things folded into :attr:`OTHER` because each has a *different
    correct recovery*, and a caller cannot choose between them if they arrive
    as the same value. Overflow means compact the transcript and retry — the
    ordinary case for a small-context runtime, where it is common rather than
    exotic. Pause means the turn is unfinished and expects to be continued;
    treating it as a completed turn silently truncates the model's work.
    """

    END_TURN = "end_turn"
    TOOL_CALLS = "tool_calls"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    REFUSAL = "refusal"
    CONTEXT_OVERFLOW = "context_overflow"
    PAUSE = "pause"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool invocation the model asked for."""

    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.call_id:
            raise BackendProtocolError("a tool call needs an id to pair its result with")
        if not self.name:
            raise BackendProtocolError("a tool call needs a name")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    def arguments_json(self) -> str:
        """Deterministic JSON rendering, used for estimation and logging."""
        return json.dumps(dict(self.arguments), sort_keys=True, default=str)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of running a tool, fed back to the model."""

    call_id: str
    content: str
    is_error: bool = False

    def __post_init__(self) -> None:
        if not self.call_id:
            raise BackendProtocolError("a tool result must name the call it answers")


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """One turn of the conversation.

    A user turn may carry tool results; an assistant turn may carry tool calls.
    The reverse is rejected at construction — a transcript that mixes them is
    not representable by every provider, and discovering that at request time
    turns a modelling error into a vendor error.
    """

    role: Role
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()

    def __post_init__(self) -> None:
        if self.role is Role.USER and self.tool_calls:
            raise BackendProtocolError("only an assistant turn may request tool calls")
        if self.role is Role.ASSISTANT and self.tool_results:
            raise BackendProtocolError("only a user turn may carry tool results")
        if not self.text and not self.tool_calls and not self.tool_results:
            raise BackendProtocolError("a message must carry text, tool calls, or tool results")

    @classmethod
    def user(cls, text: str) -> ModelMessage:
        """A plain user turn."""
        return cls(role=Role.USER, text=text)

    @classmethod
    def assistant(cls, text: str = "", *, tool_calls: Sequence[ToolCall] = ()) -> ModelMessage:
        """An assistant turn, optionally requesting tools."""
        return cls(role=Role.ASSISTANT, text=text, tool_calls=tuple(tool_calls))

    @classmethod
    def results(cls, results: Sequence[ToolResult], *, text: str = "") -> ModelMessage:
        """A user turn answering one or more tool calls."""
        return cls(role=Role.USER, text=text, tool_results=tuple(results))


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool offered to the model.

    ``input_schema`` is JSON Schema — the one tool-description format every
    candidate backend already consumes, whatever it converts it to internally.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise BackendProtocolError("a tool needs a name")
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))

    def schema_json(self) -> str:
        """Deterministic JSON rendering, used for estimation and logging."""
        return json.dumps(dict(self.input_schema), sort_keys=True, default=str)


def _validate_transcript(messages: tuple[ModelMessage, ...]) -> None:
    """Reject transcripts that no backend can serve.

    Three structural rules, checked here rather than discovered at request
    time. They are not one provider's pedantry — they are what a conversation
    *is*, and every candidate backend needs them:

    * **Roles alternate.** Two consecutive turns from the same speaker have no
      rendering: an API rejects them outright, and a chat template silently
      renders something malformed and answers it, which is the worse of the two
      failures and the one this seam exists to prevent. Merge them instead;
      :meth:`ModelMessage.results` takes text alongside its results for exactly
      that.
    * **A tool result answers a call in the turn just before it.** A result
      naming an id that was never requested is unanswerable.
    * **Every requested call gets answered.** A tool-calling turn followed by
      anything other than its complete set of results leaves the model waiting
      on a result it will never see.

    Deliberately *not* checked: that the transcript ends on a user turn. A
    trailing assistant turn is a legitimate prefill — constraining the start of
    the model's reply — and forbidding it would remove a real capability to
    satisfy a rule no backend actually imposes.
    """
    for earlier, later in itertools.pairwise(messages):
        if earlier.role is later.role:
            raise BackendProtocolError(
                f"transcript has two consecutive {later.role.value} turns; "
                "roles must alternate, so merge them into one turn"
            )
    for index, message in enumerate(messages):
        if not message.tool_results:
            continue
        previous = messages[index - 1] if index else None
        offered = (
            {call.call_id for call in previous.tool_calls}
            if previous is not None and previous.role is Role.ASSISTANT
            else set()
        )
        answered = {result.call_id for result in message.tool_results}
        unknown = sorted(answered - offered)
        if unknown:
            raise BackendProtocolError(
                f"tool results answer calls the preceding turn never made: {unknown}"
            )
    for index, message in enumerate(messages[:-1]):
        if not message.tool_calls:
            continue
        following = messages[index + 1]
        answered = {result.call_id for result in following.tool_results}
        unanswered = sorted({call.call_id for call in message.tool_calls} - answered)
        if unanswered:
            raise BackendProtocolError(
                f"tool calls left unanswered by the next turn: {unanswered}; "
                "every requested call needs a result before the conversation continues"
            )


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """One turn's worth of work for a backend.

    ``tier`` has no default. Every call site states which tier it wants,
    because routing mundane work down is the thing that has to actually happen
    for the budget to hold, and a default would let it not happen silently.

    ``max_tokens`` of ``None`` means "use the policy's ceiling for this tier".
    """

    tier: ModelTier
    messages: tuple[ModelMessage, ...]
    system: str = ""
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int | None = None
    stop_sequences: tuple[str, ...] = ()
    request_id: str = ""

    def __post_init__(self) -> None:
        if not self.messages:
            raise BackendProtocolError("a request needs at least one message")
        if self.messages[0].role is not Role.USER:
            raise BackendProtocolError("a conversation must open with a user turn")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise BackendProtocolError("max_tokens must be positive when set")
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise BackendProtocolError("tool names must be unique within a request")
        _validate_transcript(self.messages)

    def with_messages(self, messages: Sequence[ModelMessage]) -> GenerationRequest:
        """Same request, different transcript — the solver's loop step."""
        return GenerationRequest(
            tier=self.tier,
            messages=tuple(messages),
            system=self.system,
            tools=self.tools,
            max_tokens=self.max_tokens,
            stop_sequences=self.stop_sequences,
            request_id=self.request_id,
        )


@dataclass(frozen=True, slots=True)
class GenerationResponse:
    """What a backend returned for one request.

    ``model`` is the concrete model that actually served it, not the tier —
    a round's numbers are only attributable if the engine that produced them
    is recorded rather than inferred. Providers that echo the serving model are
    taken at their word; for the ones that do not, the fallback is the model
    this engine assigns to the *requested tier*
    (:meth:`BackendIdentity.model_for_tier`), never a fixed one.
    """

    tier: ModelTier
    model: str
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason = StopReason.END_TURN
    usage: Usage = field(default_factory=Usage)

    @property
    def has_tool_calls(self) -> bool:
        """True when the model wants a tool run before it continues."""
        return bool(self.tool_calls)

    def as_message(self) -> ModelMessage:
        """This response as an assistant turn, ready to append to a transcript."""
        return ModelMessage(
            role=Role.ASSISTANT,
            text=self.text,
            tool_calls=self.tool_calls,
        )


@dataclass(frozen=True, slots=True)
class BackendIdentity:
    """Which engine a backend is, for the round record.

    Maps onto :class:`~turing.research.contracts.EngineIdentity`, which is
    logged per round and compared across rounds: a delta measured against a
    different engine is not a delta.
    """

    backend: str
    orchestrator_model: str
    substep_model: str | None = None

    def __post_init__(self) -> None:
        if not self.backend or not self.orchestrator_model:
            raise BackendProtocolError("a backend identity needs a name and an orchestrator model")

    def model_for_tier(self, tier: ModelTier) -> str:
        """The model this engine serves ``tier`` with.

        Used as the fallback when a provider does not echo which model served a
        turn — not every runtime does. Falling back to the orchestrator
        regardless of tier would record cheap-tier work as expensive-tier work,
        and the per-tier breakdown is the only evidence that routing mundane
        work down actually happened.
        """
        if tier is ModelTier.SUBSTEP and self.substep_model:
            return self.substep_model
        return self.orchestrator_model

    def to_engine_identity(self, scaffold_git_sha: str) -> EngineIdentity:
        """Pair this engine with the scaffold version that ran on it."""
        return EngineIdentity(
            backend=self.backend,
            orchestrator_model=self.orchestrator_model,
            substep_model=self.substep_model,
            scaffold_git_sha=scaffold_git_sha,
        )


def _token_digest(body: str, secret: str | None) -> str:
    """Keyed digest over a token body.

    With a secret this is an HMAC and a rewritten body cannot be re-sealed
    without the key. Without one it degrades to an unkeyed digest, which
    detects corruption and casual editing but is **not** authentication — see
    :class:`ResumeToken` on which of the two a given deployment needs.
    """
    return hmac.new(
        (secret or "").encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ResumeToken:
    """Opaque-to-contracts state carried through an attempt's checkpoint.

    :attr:`~turing.research.contracts.Attempt.resume_token` is a string the
    contracts layer never interprets. This is what goes in it. It carries two
    things:

    * the ledger, so consumption already burned is not forgotten and re-granted
      — the difference between an interruption costing the remainder of an
      attempt and costing the attempt;
    * the engine identity, so resuming onto a different engine is refused
      rather than silently producing a round whose numbers came from two
      configurations.

    **Integrity.** This is the one sanctioned way spend crosses a process
    boundary, so a token that can be rewritten is a cap that can be reset: edit
    the ledger to zeros, resume, and the whole budget is granted again. Every
    token therefore carries a digest over its body, and a body that does not
    match its digest is refused.

    The digest is keyed when a secret is configured and unkeyed otherwise. That
    distinction is the whole security story and is worth stating plainly:

    * **Unkeyed** — catches truncated, corrupted, or hand-edited checkpoints.
      Sufficient while nothing with write access to the checkpoint has an
      interest in a larger budget, which is the state of loop 1.
    * **Keyed** — catches deliberate forgery, because re-sealing a rewritten
      body needs the key. This is the mode to run in once a self-editing agent
      holds a writable workspace, which is the seam loop 2 attaches to: pass a
      secret the agent's own process cannot read and nothing else changes.
    """

    backend: str
    orchestrator_model: str
    substep_model: str | None
    ledger: LedgerSnapshot
    version: int = RESUME_TOKEN_VERSION

    def _body(self) -> str:
        return json.dumps(
            {
                "backend": self.backend,
                "orchestrator_model": self.orchestrator_model,
                "substep_model": self.substep_model,
                "ledger": self.ledger.to_dict(),
            },
            sort_keys=True,
        )

    def encode(self, *, secret: str | None = None) -> str:
        """Render to the string an attempt checkpoint stores, sealed."""
        body = self._body()
        return json.dumps(
            {
                "version": self.version,
                "body": body,
                "digest": _token_digest(body, secret),
            },
            sort_keys=True,
        )

    @classmethod
    def decode(cls, raw: str, *, secret: str | None = None) -> ResumeToken:
        """Parse a token produced by :meth:`encode`, verifying its seal.

        A token whose body does not match its digest is refused rather than
        read: the failure mode being defended against is a budget silently
        re-granted, and a resume that quietly starts from zero is exactly that.
        """
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise BackendResumeError(f"resume token is not valid JSON: {exc}") from exc
        if not isinstance(envelope, dict):
            raise BackendResumeError("resume token must decode to an object")
        version = envelope.get("version")
        if version != RESUME_TOKEN_VERSION:
            raise BackendResumeError(
                f"resume token version {version!r} is not readable by this build "
                f"(expected {RESUME_TOKEN_VERSION})"
            )
        body = envelope.get("body")
        digest = envelope.get("digest")
        if not isinstance(body, str) or not isinstance(digest, str):
            raise BackendResumeError("resume token is missing its body or digest")
        if not hmac.compare_digest(digest, _token_digest(body, secret)):
            raise BackendResumeError(
                "resume token failed its integrity check: the recorded spend has been "
                "altered, or it was sealed with a different secret. Refusing rather than "
                "resuming, because accepting it would re-grant budget already burned"
            )
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:  # pragma: no cover - digest covers this
            raise BackendResumeError(f"resume token body is not valid JSON: {exc}") from exc
        try:
            return cls(
                backend=str(payload["backend"]),
                orchestrator_model=str(payload["orchestrator_model"]),
                substep_model=payload["substep_model"],
                ledger=LedgerSnapshot.from_dict(payload["ledger"]),
                version=version,
            )
        except (KeyError, TypeError) as exc:
            raise BackendResumeError(f"resume token missing field: {exc}") from exc

    def identity_matches(self, identity: BackendIdentity) -> bool:
        """True when this token was minted by the same engine configuration."""
        return (
            self.backend == identity.backend
            and self.orchestrator_model == identity.orchestrator_model
            and self.substep_model == identity.substep_model
        )


@runtime_checkable
class ModelBackend(Protocol):
    """Send messages and tool definitions, get a response back.

    Six members, all provider-neutral. A local implementation satisfies this
    without any change to the solver, which is the entire point of the seam.
    """

    @property
    def identity(self) -> BackendIdentity:
        """Which engine this is, for the round record."""
        ...

    @property
    def ledger(self) -> UsageLedger:
        """What this backend has spent. The cap reads it."""
        ...

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Run one turn, accounting for it whether or not it succeeds."""
        ...

    def checkpoint(self) -> str:
        """Serialise resumable state into an attempt's ``resume_token``."""
        ...

    def restore(self, token: str) -> None:
        """Reinstate state from a token, refusing a mismatched engine."""
        ...

    async def aclose(self) -> None:
        """Release any transport held by this backend."""
        ...
