"""Webui chat pane — the secondary, ad-hoc work-direction surface (ADR 0010 §1, Slice E).

The question-queue manager (Slice C) is the hero surface for the overnight
research flywheel; this is its daytime counterpart. The operator types a
free-form prompt, the coordinator plans a DAG, and each planned subtask streams
back into the *same thread*:

    submit(prompt) ──▶ session (one thread)
        │
        ├─ plan_subtask(...)   subtask appears, status=pending
        ├─ stream(...)         worker output lands, status=streaming → completed
        └─ accept/reject/edit  per-subtask operator thumb → episode_rewards

**Reward emission reuses Slice C's emitter VERBATIM — there is no new reward
path (ADR 0010 Slice E "Reuses the reward emitter from C").** A per-subtask
thumb writes a ``MORNING_CURATION`` reward event with the *exact* magnitudes
the queue manager (and before it the retired Discord surface) used:

    accept → +1.0   reject → −1.0   edit → +0.3

and, when a subtask consumed upstream subtask outputs (a synthesis step in the
planned DAG), accept / reject fans the ±0.3 synthesis fractional credit out to
each consumed upstream — identical to ``QueueManager`` / ``write_synthesis_thumb``.
To guarantee that identity rather than re-implement it, ``ChatManager`` *delegates*
the reward write to a private :class:`QueueManager` instance over the shared
:class:`EpisodeRewardsStore`: the chat-surface thumb is, byte-for-byte, a queue
curation. Only the *input affordance* changes; the rows do not.

State is in-memory, mirroring ``QueueManager`` (a SQLite projection is layered
above out of scope for this slice). Frame fan-out rides the gateway's existing
async broadcaster (``TelemetrySink._broadcast``), the same path queue and alert
frames use — so the transition methods are ``async`` and ``await`` the
broadcast. Two frame types, mirroring the queue contract:

    chat.snapshot   full state, sent on WS connect
    chat.delta      one message, per mutation, ``action`` naming the transition

Idempotency mirrors the queue manager: a redelivered submit (same session id),
a redelivered plan (same subtask id), and a redelivered thumb on an already
curated subtask are each no-ops that never double-write a reward.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from turing.coordinator.flywheel.morning_curation import CurationDecision
from turing.gateway.queue_manager import QueueItem, QueueManager, QueueStatus

if TYPE_CHECKING:
    from turing.coordinator.episode_rewards import EpisodeRewardsStore


class ChatSubtaskStatus(StrEnum):
    """Lifecycle of one subtask streaming into a chat thread (ADR 0010 Slice E)."""

    PENDING = "pending"  # planned, not yet producing output
    STREAMING = "streaming"  # the worker is emitting tokens
    COMPLETED = "completed"  # the worker finished; awaiting a thumb
    CURATED = "curated"  # the operator thumbed it (terminal, reward written)
    ERROR = "error"  # the worker failed


@dataclass(frozen=True)
class ChatSubtask:
    """One subtask in a chat thread, projected for the webui chat pane.

    ``episode_id`` is the worker episode the operator's thumb rewards;
    ``consumed_upstreams`` lists the upstream subtask episodes a synthesis step
    read (the synthesis fractional-credit targets, empty for a plain subtask).
    The fields mirror the reward-bearing subset of :class:`QueueItem` so the
    delegated reward write is a one-to-one mapping.
    """

    id: str
    session_id: str
    index: int
    specialty: str
    prompt: str = ""
    content: str = ""
    status: ChatSubtaskStatus = ChatSubtaskStatus.PENDING
    episode_id: str | None = None
    consumed_upstreams: tuple[str, ...] = ()
    created_at_ms: int = 0
    completed_at_ms: int | None = None
    curated_at_ms: int | None = None
    decision: CurationDecision | None = None
    corrected_answer: str | None = None

    def to_frame(self) -> dict:
        """Serialize to the WS/JSON shape consumed by ``webui/src/chat``."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "index": self.index,
            "specialty": self.specialty,
            "prompt": self.prompt,
            "content": self.content,
            "status": self.status.value,
            "episode_id": self.episode_id,
            "consumed_upstreams": list(self.consumed_upstreams),
            "created_at_ms": self.created_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "curated_at_ms": self.curated_at_ms,
            "decision": self.decision.value if self.decision is not None else None,
            "corrected_answer": self.corrected_answer,
        }


@dataclass
class ChatSession:
    """One ad-hoc chat thread: a prompt and the subtasks the coordinator planned.

    Subtasks are kept in insertion order so the thread reads top-to-bottom in
    plan order; ``index`` is the stable position the webui sorts on.
    """

    id: str
    prompt: str
    specialty: str = "research"
    created_at_ms: int = 0
    subtasks: list[ChatSubtask] = field(default_factory=list)

    def to_frame(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "specialty": self.specialty,
            "created_at_ms": self.created_at_ms,
            "subtasks": [s.to_frame() for s in self.subtasks],
        }


# A delta's ``action`` names the transition that produced it. ``snapshot`` is
# reserved for the full-state frame; deltas carry one of these.
ChatAction = (
    str  # "submit" | "plan" | "stream" | "complete" | "error" | "accept" | "reject" | "edit"
)

# The gateway passes ``TelemetrySink._broadcast`` here (async).
BroadcastFn = Callable[[dict], Awaitable[None]]


class ChatSessionNotFoundError(KeyError):
    """Raised when an operation targets a session id the manager never saw."""


class ChatSubtaskNotFoundError(KeyError):
    """Raised when an operation targets a subtask id absent from its session."""


class ChatSubtaskNotRewardableError(ValueError):
    """Raised when a thumb targets a subtask that is not in a rewardable state.

    Only a ``COMPLETED`` subtask (a finished worker draft awaiting curation) can
    receive a reward. A ``PENDING``/``STREAMING`` subtask has no draft yet, and an
    ``ERROR`` subtask failed — neither may be thumbed, so a worker-failed episode
    cannot be handed a +1.0 via a direct or replayed POST. ``CURATED`` is handled
    separately as an idempotent no-op (a redelivered thumb), not an error. Slice D
    will make this endpoint the sole reward source, so the guard is load-bearing.
    """


class ChatManager:
    """In-memory projection of ad-hoc chat threads + reward delegation.

    Construction takes the shared :class:`EpisodeRewardsStore` (the same
    instance the queue manager, and before it the Discord path, writes to) and
    an optional ``broadcast`` callback for WS fan-out. ``now_ms`` is injectable
    for deterministic tests, mirroring :class:`QueueManager`.

    The reward path is *delegated* to a private :class:`QueueManager` over the
    same store (see module docstring): a per-subtask thumb is a queue curation,
    so the rows are byte-identical and there is no second emitter to drift.
    """

    def __init__(
        self,
        *,
        rewards: EpisodeRewardsStore,
        broadcast: BroadcastFn | None = None,
        now_ms: Callable[[], int],
    ) -> None:
        self._rewards = rewards
        self._broadcast = broadcast
        self._now_ms = now_ms
        self._sessions: dict[str, ChatSession] = {}
        # The reused Slice C emitter. broadcast=None: chat frames ride the chat
        # contract, not the queue one, so the delegate stays silent on the WS;
        # we only borrow its reward-writing semantics over the shared store.
        self._rewarder = QueueManager(rewards=rewards, broadcast=None, now_ms=now_ms)

    # ── ingestion ────────────────────────────────────────────────────────────

    async def submit(
        self, *, session_id: str, prompt: str, specialty: str = "research"
    ) -> ChatSession:
        """Open a chat thread for ``prompt``. Idempotent on ``session_id``.

        Broadcasts a ``chat.delta`` with ``action="submit"`` for a genuinely
        new session; a duplicate submit is a silent no-op (no second frame).
        """
        if session_id in self._sessions:
            return self._sessions[session_id]
        session = ChatSession(
            id=session_id,
            prompt=prompt,
            specialty=specialty,
            created_at_ms=self._now_ms(),
        )
        self._sessions[session_id] = session
        await self._emit_session_delta("submit", session)
        return session

    async def plan_subtask(
        self,
        session_id: str,
        *,
        subtask_id: str,
        specialty: str,
        prompt: str = "",
        episode_id: str | None = None,
        consumed_upstreams: Sequence[str] = (),
    ) -> ChatSubtask:
        """Add a planned subtask to the thread (coordinator DAG output).

        Idempotent on ``subtask_id`` within a session. Emits ``action="plan"``.
        """
        session = self._require_session(session_id)
        existing = self._find(session, subtask_id)
        if existing is not None:
            return existing
        subtask = ChatSubtask(
            id=subtask_id,
            session_id=session_id,
            index=len(session.subtasks),
            specialty=specialty,
            prompt=prompt,
            episode_id=episode_id,
            consumed_upstreams=tuple(consumed_upstreams),
            created_at_ms=self._now_ms(),
        )
        session.subtasks.append(subtask)
        await self._emit_subtask_delta("plan", subtask)
        return subtask

    # ── streaming (no reward) ──────────────────────────────────────────────────

    async def stream(
        self, session_id: str, subtask_id: str, *, chunk: str, done: bool = False
    ) -> ChatSubtask:
        """Append a streamed ``chunk`` to a subtask's content.

        Sets status to ``streaming`` while output flows and ``completed`` once
        ``done`` is true (the operator can thumb a completed subtask). Emits
        ``action="stream"`` while streaming, ``action="complete"`` on the final
        chunk. No reward — that is the thumb's job.
        """
        subtask = self._require_subtask(session_id, subtask_id)
        if subtask.status is ChatSubtaskStatus.CURATED:
            return subtask  # terminal; late chunks are ignored
        completed_at = self._now_ms() if done else subtask.completed_at_ms
        updated = replace(
            subtask,
            content=subtask.content + chunk,
            status=ChatSubtaskStatus.COMPLETED if done else ChatSubtaskStatus.STREAMING,
            completed_at_ms=completed_at,
        )
        action = "complete" if done else "stream"
        return await self._commit(action, updated)

    async def fail(self, session_id: str, subtask_id: str, *, error: str = "") -> ChatSubtask:
        """Mark a subtask errored (the worker failed). Emits ``action="error"``."""
        subtask = self._require_subtask(session_id, subtask_id)
        if subtask.status is ChatSubtaskStatus.CURATED:
            return subtask
        updated = replace(
            subtask,
            status=ChatSubtaskStatus.ERROR,
            content=error or subtask.content,
            completed_at_ms=self._now_ms(),
        )
        return await self._commit("error", updated)

    # ── per-subtask thumbs (reward-writing, delegated to Slice C) ──────────────

    async def accept(self, session_id: str, subtask_id: str) -> ChatSubtask:
        """Thumb-up a subtask: +1.0 to the episode (+0.3 to consumed upstreams)."""
        return await self._thumb(session_id, subtask_id, CurationDecision.ACCEPT)

    async def reject(self, session_id: str, subtask_id: str) -> ChatSubtask:
        """Thumb-down a subtask: −1.0 to the episode (−0.3 to consumed upstreams)."""
        return await self._thumb(session_id, subtask_id, CurationDecision.REJECT)

    async def edit(self, session_id: str, subtask_id: str, *, corrected_answer: str) -> ChatSubtask:
        """Edit a subtask: +0.3 partial credit; captures the corrected answer.

        Edit does **not** fan synthesis fractional credit — the correction is
        the operator's own answer, not an endorsement of the upstreams. Same
        semantics as ``QueueManager.edit``.
        """
        return await self._thumb(
            session_id, subtask_id, CurationDecision.EDIT, corrected_answer=corrected_answer
        )

    # ── reads ────────────────────────────────────────────────────────────────

    def get_session(self, session_id: str) -> ChatSession:
        return self._require_session(session_id)

    def sessions(self) -> list[ChatSession]:
        """Every session, oldest-first by creation (stable tiebreak on id)."""
        return sorted(self._sessions.values(), key=lambda s: (s.created_at_ms, s.id))

    def snapshot_frame(self) -> dict:
        """The full-state ``chat.snapshot`` WS frame (sent on WS connect)."""
        return {
            "type": "chat.snapshot",
            "sessions": [s.to_frame() for s in self.sessions()],
            "timestamp_ms": self._now_ms(),
        }

    def __len__(self) -> int:
        return len(self._sessions)

    # ── internals ────────────────────────────────────────────────────────────

    def _require_session(self, session_id: str) -> ChatSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise ChatSessionNotFoundError(session_id) from exc

    @staticmethod
    def _find(session: ChatSession, subtask_id: str) -> ChatSubtask | None:
        for s in session.subtasks:
            if s.id == subtask_id:
                return s
        return None

    def _require_subtask(self, session_id: str, subtask_id: str) -> ChatSubtask:
        session = self._require_session(session_id)
        subtask = self._find(session, subtask_id)
        if subtask is None:
            raise ChatSubtaskNotFoundError(subtask_id)
        return subtask

    async def _thumb(
        self,
        session_id: str,
        subtask_id: str,
        decision: CurationDecision,
        *,
        corrected_answer: str | None = None,
    ) -> ChatSubtask:
        subtask = self._require_subtask(session_id, subtask_id)
        # Idempotency: a second thumb on a curated subtask never re-writes the
        # reward. Mirrors the queue manager's CURATED guard.
        if subtask.status is ChatSubtaskStatus.CURATED:
            return subtask
        # Only a COMPLETED draft is rewardable. Reject a thumb on a PENDING /
        # STREAMING (no draft yet) or ERROR (worker failed) subtask so a failed
        # episode cannot be handed a reward via a direct or replayed POST — the
        # queue surface only curates from DRAFTED for the same reason, and Slice D
        # makes this the sole reward source.
        if subtask.status is not ChatSubtaskStatus.COMPLETED:
            raise ChatSubtaskNotRewardableError(subtask_id)
        await self._delegate_reward(subtask, decision, corrected_answer=corrected_answer)
        updated = replace(
            subtask,
            status=ChatSubtaskStatus.CURATED,
            curated_at_ms=self._now_ms(),
            decision=decision,
            corrected_answer=corrected_answer,
        )
        return await self._commit(decision.value, updated)

    async def _delegate_reward(
        self,
        subtask: ChatSubtask,
        decision: CurationDecision,
        *,
        corrected_answer: str | None,
    ) -> None:
        """Write the reward by *replaying the thumb as a queue curation*.

        This is the heart of the "reuse C's emitter" requirement: rather than
        re-implement the magnitudes and the synthesis fan-out, we hand the
        subtask to the Slice C :class:`QueueManager` as a drafted item and let
        it curate. The rows it lands in the shared store are byte-identical to
        a queue-pane decision over the same episode topology — proven in
        ``test_chat_reward_equivalence`` against the queue surface.

        The delegate's ``broadcast`` is ``None``, so its curation only writes to
        the shared store and emits no queue frame — the chat frame is fanned out
        separately by ``_commit``. A subtask with no ``episode_id`` (never ran)
        writes no reward, matching the queue manager's guard.

        Cross-surface idempotency (by design, not a bug): the chat delegate and
        the live :class:`QueueManager` share one :class:`EpisodeRewardsStore`, and
        ``_already_rewarded`` keys solely on ``episode_id``. So a chat thumb and a
        queue curation on the *same* ``episode_id`` resolve to the same guard key —
        whichever lands first writes the reward; the second silently no-ops rather
        than double-writing. This is safe because ``episode_id`` is unique per run
        (one episode is surfaced on one surface), so the collision is theoretical;
        the guard simply makes the reward write idempotent across both surfaces.
        """
        item_id = f"chat:{subtask.session_id}:{subtask.id}"
        item = QueueItem(
            id=item_id,
            prompt=subtask.prompt or subtask.content,
            specialty=subtask.specialty,
            status=QueueStatus.DRAFTED,
            episode_id=subtask.episode_id,
            consumed_upstreams=subtask.consumed_upstreams,
            created_at_ms=subtask.created_at_ms,
        )
        # Seed the delegate's state directly (its broadcast is None, so this is
        # a pure in-memory insert), then curate through its public reward path.
        self._rewarder._items[item_id] = item
        if decision is CurationDecision.ACCEPT:
            await self._rewarder.accept(item_id)
        elif decision is CurationDecision.REJECT:
            await self._rewarder.reject(item_id)
        else:  # EDIT
            await self._rewarder.edit(item_id, corrected_answer=corrected_answer or "")

    async def _commit(self, action: ChatAction, updated: ChatSubtask) -> ChatSubtask:
        session = self._require_session(updated.session_id)
        for i, existing in enumerate(session.subtasks):
            if existing.id == updated.id:
                session.subtasks[i] = updated
                break
        await self._emit_subtask_delta(action, updated)
        return updated

    async def _emit_session_delta(self, action: ChatAction, session: ChatSession) -> None:
        if self._broadcast is None:
            return
        await self._broadcast(
            {
                "type": "chat.delta",
                "action": action,
                "session": session.to_frame(),
                "timestamp_ms": self._now_ms(),
            }
        )

    async def _emit_subtask_delta(self, action: ChatAction, subtask: ChatSubtask) -> None:
        if self._broadcast is None:
            return
        await self._broadcast(
            {
                "type": "chat.delta",
                "action": action,
                "session_id": subtask.session_id,
                "subtask": subtask.to_frame(),
                "timestamp_ms": self._now_ms(),
            }
        )
