"""The four things the solver depends on, as structural protocols.

The solver owns the refinement loop and nothing else. It gets its model calls,
its operator channel, its durable storage and its clock from outside, which is
what lets the whole loop be exercised end to end with no network, no API key
and no wall-clock waiting.

**On the model backend.** :mod:`turing.research.backends` owns the real
"send messages + tools, get a response" interface and its Claude
implementation. :class:`ProposalBackend` here is deliberately *narrower* than
that: it is the single call the solver makes, phrased in solver vocabulary
(:class:`~turing.research.solver.models.ProposalContext` in,
:class:`~turing.research.solver.models.Proposal` out). The adapter between the
two is :class:`~turing.research.backends.adapter.ProposalAdapter`: it renders
the context into messages, calls the backend, and parses the reply. Prompt
shape and response parsing are backend-specific, so the adapter lives with
the backend and the solver must not grow a second reason to change.
Structural typing means no import crosses between the two modules *except*
through that adapter.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import (
        Attempt,
        EscalationDecision,
        EscalationRequest,
    )
    from turing.research.solver.models import (
        CommandOutcome,
        IterationRecord,
        Proposal,
        ProposalContext,
    )

__all__ = [
    "CheckpointStore",
    "Clock",
    "CommandRunner",
    "EscalationChannel",
    "ProposalBackend",
    "ResumableBackend",
    "SystemClock",
]


@runtime_checkable
class Clock(Protocol):
    """Wall-clock and monotonic time, injected so tests need not wait.

    Two readings rather than one: ``now_ms`` stamps checkpoints and must be
    real calendar time for an operator reading an escalation at 2am, while
    ``monotonic`` measures the wall-clock a cap dimension is denominated in
    and must not jump when the system clock is adjusted.
    """

    def now_ms(self) -> int:
        """Milliseconds since the epoch, for checkpoint timestamps."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, for measuring durations."""
        ...


class SystemClock:
    """The real clock. The default for everything outside tests."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def monotonic(self) -> float:
        return time.monotonic()


@runtime_checkable
class ProposalBackend(Protocol):
    """Turns the state of an attempt into the next change to try.

    One method, because the solver's whole model interaction is one call per
    iteration. Implementations live behind :mod:`turing.research.backends`;
    the fake used in tests is a scripted list of proposals.
    """

    async def propose(self, context: ProposalContext) -> Proposal:
        """Propose the next change. Must not mutate ``context.workspace``."""
        ...


@runtime_checkable
class ResumableBackend(Protocol):
    """A backend that can hand its in-progress state to a later process.

    Optional. The solver checks for it with ``isinstance`` and carries whatever
    :meth:`checkpoint` returns in
    :attr:`turing.research.contracts.Attempt.resume_token`, which contracts
    define as opaque and backend-owned. Without it a resumed attempt still
    keeps its workspace, journal and budget — it just starts a fresh
    conversation, so the model loses whatever context it had accumulated.

    The two methods are deliberately synchronous: this is serialising state
    the backend already holds, not talking to anyone.
    """

    def checkpoint(self) -> str:
        """Serialise resumable state into a token."""
        ...

    def restore(self, token: str) -> None:
        """Reinstate state from a token, refusing one from a different engine."""
        ...


@runtime_checkable
class CommandRunner(Protocol):
    """Runs a command inside an attempt's workspace.

    **Seam, not an implementation.** Executing model-authored commands is the
    job of the existing safety layer — shell gate, deny-list, capability
    tokens — and of the separate ``turing`` macOS user that owns the
    workspace. The solver refuses to run commands unless a runner is supplied,
    so wiring one in is a deliberate act by whoever owns that safety surface,
    not a default.
    """

    async def run(self, command: str, *, workspace: Path, timeout_seconds: float) -> CommandOutcome:
        """Run ``command`` with ``workspace`` as its working directory."""
        ...


@runtime_checkable
class EscalationChannel(Protocol):
    """The operator gate: a rich question out, a three-valued answer back.

    ``await_decision`` returning ``None`` is the unattended case and the
    normal one — ntfy fires, nobody is awake, and the solver checkpoints at
    ``ESCALATED`` and returns. A later call to
    :meth:`turing.research.solver.Solver.run` re-polls the same request id.
    An implementation that blocks until an operator answers is also valid; the
    solver treats both identically.

    Note the return type: :class:`~turing.research.contracts.EscalationDecision`
    carries a verdict and, for ``EXTEND_CAP``, numbers. There is no channel
    here for free-form advice, and adding one would make the operator the
    improvement mechanism and confound the next round's delta.
    """

    async def raise_escalation(self, request: EscalationRequest) -> None:
        """Deliver a request to the operator. Must not block on an answer."""
        ...

    async def await_decision(self, request_id: str) -> EscalationDecision | None:
        """Return the operator's decision, or ``None`` if there is not one yet."""
        ...


@runtime_checkable
class CheckpointStore(Protocol):
    """Durable storage for attempts, iteration journals and escalations.

    :meth:`commit_iteration` writing both arguments **atomically** is the load-
    bearing requirement. Consumed budget lives on the attempt and the phase
    that spent it lives on the record; if those two can disagree after a
    crash, the solver either re-spends tokens it already paid for or loses
    work it already did.
    """

    async def save_attempt(self, attempt: Attempt) -> None:
        """Persist an attempt checkpoint. Must reject a stale ``checkpoint_seq``."""
        ...

    async def load_attempt(self, attempt_id: str) -> Attempt | None:
        """Load the newest checkpoint for an attempt, or ``None``."""
        ...

    async def commit_iteration(self, record: IterationRecord, attempt: Attempt) -> None:
        """Persist a journal entry and the attempt it charged, in one transaction."""
        ...

    async def latest_iteration(self, attempt_id: str) -> IterationRecord | None:
        """The highest-indexed journal entry for an attempt, or ``None``."""
        ...

    async def iterations(self, attempt_id: str) -> tuple[IterationRecord, ...]:
        """Every journal entry for an attempt, in index order."""
        ...

    async def save_escalation(self, request: EscalationRequest) -> None:
        """Persist an escalation so a resumed run can report what it asked."""
        ...

    async def load_escalation(self, request_id: str) -> EscalationRequest | None:
        """Load a previously raised escalation, or ``None``."""
        ...
