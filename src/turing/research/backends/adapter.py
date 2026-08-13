"""The adapter the solver protocols promised and the backends never shipped.

:class:`~turing.research.solver.protocols.ProposalBackend` is one call
(``propose``) in solver vocabulary. :class:`~turing.research.backends.protocol.ModelBackend`
is one call (``generate``) in message vocabulary. Prompt shape and response
parsing are backend-specific, so the translation lives here rather than in
the solver — the solver must not grow a second reason to change when a
second engine arrives.

This module is the one sanctioned import from backends into solver types.
Everything else between the two packages stays structurally typed.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from turing.research.backends.accounting import Usage
from turing.research.backends.base import estimate_request_tokens
from turing.research.backends.errors import BackendError, BackendProtocolError
from turing.research.backends.protocol import (
    GenerationRequest,
    ModelMessage,
)
from turing.research.backends.tiering import ModelTier
from turing.research.solver.models import FileEdit, Proposal

if TYPE_CHECKING:
    from turing.research.backends.protocol import GenerationResponse, ModelBackend
    from turing.research.contracts import CapConsumption, VerificationResult
    from turing.research.solver.models import IterationSummary, ProposalContext

__all__ = ["PROPOSAL_SCHEMA_HINT", "ProposalAdapter", "encode_proposal"]

#: What the model is asked to emit. Kept as a constant so tests can pin the
#: contract without scraping a prompt, and so a second backend can reuse it.
PROPOSAL_SCHEMA_HINT = (
    "Reply with a single JSON object, no markdown, no surrounding prose. "
    "Keys: rationale (string), edits (list of {relative_path, content}), "
    "commands (list of strings), no_viable_approach (boolean). "
    "Omit commands when you are not asking to run anything. "
    "Set no_viable_approach true only when you see no way forward; then "
    "edits and commands must be empty. Do not report token counts."
)

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def encode_proposal(
    *,
    rationale: str = "",
    edits: tuple[tuple[str, str], ...] = (),
    commands: tuple[str, ...] = (),
    no_viable_approach: bool = False,
    proposal_id: str = "",
) -> str:
    """Render a proposal as the JSON a :class:`ProposalAdapter` will parse.

    For tests that script a :class:`~turing.research.backends.fake.FakeBackend`.
    A ``tokens`` field is deliberately not accepted: usage is the backend's
    to report, and a model-authored number would let the cap be under-counted.
    """
    payload: dict[str, object] = {
        "rationale": rationale,
        "edits": [{"relative_path": path, "content": content} for path, content in edits],
        "commands": list(commands),
        "no_viable_approach": no_viable_approach,
    }
    if proposal_id:
        payload["proposal_id"] = proposal_id
    return json.dumps(payload, sort_keys=True)


class ProposalAdapter:
    """:class:`~turing.research.solver.protocols.ProposalBackend` over a
    :class:`~turing.research.backends.protocol.ModelBackend`.

    Also satisfies :class:`~turing.research.solver.protocols.ResumableBackend`
    by forwarding checkpoint/restore to the wrapped engine, so an attempt's
    ``resume_token`` still carries the ledger that engine already spent.
    """

    def __init__(
        self,
        backend: ModelBackend,
        *,
        tier: ModelTier = ModelTier.ORCHESTRATOR,
    ) -> None:
        self._backend = backend
        self._tier = tier

    @property
    def backend(self) -> ModelBackend:
        """The wrapped engine, for identity and ledger reads at the round boundary."""
        return self._backend

    async def propose(self, context: ProposalContext) -> Proposal:
        """Render ``context``, call the engine, parse a :class:`Proposal`.

        Must not mutate ``context.workspace``. Token usage on the returned
        proposal is the engine's accounted total for this call, never a
        number the model wrote.
        """
        request = GenerationRequest(
            tier=self._tier,
            messages=(ModelMessage.user(_user_text(context)),),
            system=_system_text(context),
            request_id=f"{context.problem_id}:{context.iteration_index}",
        )
        try:
            response = await self._backend.generate(request)
        except BackendError as exc:
            # generate() already recorded this call, including a fallback
            # estimate when the provider reported none. Carry that spend
            # so the runner can charge the attempt; a ledger-only figure
            # disappears across operator CONTINUE. Never a model-authored
            # number.
            _carry_accounted_usage(
                exc,
                Usage(input_tokens=estimate_request_tokens(request), estimated=True),
            )
            raise
        try:
            return _parse_proposal(
                response,
                proposal_id=f"{context.problem_id}-{context.iteration_index}",
            )
        except BackendProtocolError as exc:
            # generate() already recorded this call. Carry it on the error
            # so the runner can charge the attempt; a ledger-only figure
            # disappears across operator CONTINUE. Accounted usage, never
            # a number the model wrote.
            _carry_accounted_usage(exc, response.usage)
            raise

    def checkpoint(self) -> str:
        return self._backend.checkpoint()

    def restore(self, token: str) -> None:
        self._backend.restore(token)


def _system_text(context: ProposalContext) -> str:
    remaining = _remaining_line(context.remaining)
    return (
        f"You are iterating on problem {context.problem_id!r} "
        f"({context.problem_type.value}). Goal: {context.goal}\n"
        f"Score scale: {context.score_scale}. Iteration {context.iteration_index}. "
        f"{remaining}\n"
        f"{PROPOSAL_SCHEMA_HINT}"
    )


def _user_text(context: ProposalContext) -> str:
    parts = [
        f"workspace: {context.workspace}",
        f"seed: {context.seed}",
        f"last_result: {_result_line(context.last_result)}",
        f"best_result: {_result_line(context.best_result)}",
        "history:",
        _history_block(context.history),
    ]
    return "\n".join(parts)


def _remaining_line(remaining: CapConsumption) -> str:
    return (
        f"Remaining cap: {remaining.steps} steps, {remaining.tokens} tokens, "
        f"{remaining.wall_clock_seconds:.1f}s wall-clock."
    )


def _result_line(result: VerificationResult | None) -> str:
    if result is None:
        return "none"
    return (
        f"score={result.score} passed_correctness={result.passed_correctness} "
        f"scale={result.score_scale}"
    )


def _history_block(history: tuple[IterationSummary, ...]) -> str:
    if not history:
        return "  (none)"
    lines: list[str] = []
    for item in history:
        score = "—" if item.score is None else str(item.score)
        lines.append(
            f"  [{item.iteration_index}] score={score} "
            f"correct={item.passed_correctness} {item.rationale}"
        )
    return "\n".join(lines)


def _carry_accounted_usage(exc: BackendError, usage: Usage) -> None:
    """Attach accounted spend if the error does not already carry it."""
    if exc.usage is None:
        exc.usage = usage


def _parse_proposal(response: GenerationResponse, *, proposal_id: str) -> Proposal:
    payload = _load_json(response.text)
    edits = _parse_edits(payload.get("edits"))
    commands = _parse_commands(payload.get("commands"))
    no_viable = bool(payload.get("no_viable_approach", False))
    given_id = payload.get("proposal_id")
    identity = given_id.strip() if isinstance(given_id, str) and given_id.strip() else proposal_id
    rationale = payload.get("rationale")
    return Proposal(
        proposal_id=identity,
        rationale=rationale if isinstance(rationale, str) else "",
        edits=edits,
        commands=commands,
        tokens=response.usage.total_tokens,
        no_viable_approach=no_viable,
    )


def _load_json(text: str) -> dict[str, object]:
    blob = text.strip()
    if not blob:
        raise BackendProtocolError("model reply was empty; expected a JSON proposal")
    fenced = _FENCE.search(blob)
    if fenced:
        blob = fenced.group(1)
    else:
        match = _OBJECT.search(blob)
        if match:
            blob = match.group(0)
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise BackendProtocolError(f"model reply was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BackendProtocolError("a proposal must be a JSON object")
    return payload


def _parse_edits(raw: object) -> tuple[FileEdit, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise BackendProtocolError("edits must be a JSON list")
    edits: list[FileEdit] = []
    for item in raw:
        if not isinstance(item, dict):
            raise BackendProtocolError("each edit must be a JSON object")
        path = item.get("relative_path", item.get("path"))
        if not isinstance(path, str) or not path.strip():
            raise BackendProtocolError("each edit needs a relative_path")
        content = item.get("content")
        if not isinstance(content, str):
            raise BackendProtocolError(f"edit {path!r} content must be a string")
        edits.append(FileEdit(relative_path=path, content=content))
    return tuple(edits)


def _parse_commands(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise BackendProtocolError("commands must be a JSON list")
    commands: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise BackendProtocolError("each command must be a string")
        commands.append(item)
    return tuple(commands)
