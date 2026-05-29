"""Webui question-queue manager — the primary work-direction surface (ADR 0010 §1, §3).

This replaces the retired Discord task surface. The operator drives the
human-gated research frontier from the webui through a five-stage pipeline:

    proposed ──approve()──▶ approved ──dispatch()──▶ in-flight
                 │                                       │
                 │ reject()                              ▼ draft()
                 ▼                                    drafted
              curated  ◀──accept() / reject() / edit()──┘

``proposed`` items are worker-proposed follow-ups (or operator-seeded
questions) awaiting human approval; ``approved`` items are eligible for the
nightly dispatcher; ``in-flight`` items are running on the fleet; ``drafted``
items have a worker result awaiting curation; ``curated`` items are terminal
(the operator accepted / rejected / edited the draft).

**Reward emission is load-bearing and preserved VERBATIM from the retired
Discord surface (ADR 0010 §1).** Curation decisions write ``MORNING_CURATION``
reward events with the *exact* magnitudes the Discord path used:

    accept → +1.0   reject → −1.0   edit → +0.3

(matching ``flywheel.morning_curation.ACCEPT_REWARD / REJECT_REWARD /
EDIT_REWARD``). When a curated item carries synthesis provenance — its episode
consumed upstream subtask output keys — accept / reject additionally fan out
the synthesis-attributed *fractional credit* of ±0.3 to each consumed
upstream, exactly as ``episode_rewards.write_synthesis_thumb`` does
(``SYNTHESIS_FRACTIONAL_CREDIT``). Only the *input affordance* changes from
Discord reactions to webui buttons; the magnitudes do not.

Decisions are **idempotent**: a second accept / reject / edit on the same item
is a no-op and never double-writes a reward (the Discord listener's
``_already_recorded`` guard, ported to the curation source).

The store is in-memory, mirroring ``QuestionQueue`` / ``ProposedQueue`` /
``EpisodeStore``; persistence is a SQLite projection layered above (out of
scope for this slice, same as those stores). Idempotent on ``item_id``
(first ``add`` wins).

Frame fan-out rides the gateway's existing async broadcaster
(``TelemetrySink._broadcast``), the same path alert frames use — so the
transition methods are ``async`` and ``await`` the broadcast, mirroring
``AlertDispatcher._emit``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from turing.coordinator.episode_rewards import (
    SYNTHESIS_FRACTIONAL_CREDIT,
    EpisodeRewardsStore,
    RewardEvent,
    RewardSource,
    write_morning_curation,
)
from turing.coordinator.flywheel.morning_curation import (
    ACCEPT_REWARD,
    EDIT_REWARD,
    REJECT_REWARD,
    CurationDecision,
)


class QueueStatus(StrEnum):
    """The five stages of the human-gated question frontier (ADR 0010 §3)."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    IN_FLIGHT = "in-flight"
    DRAFTED = "drafted"
    CURATED = "curated"


@dataclass(frozen=True)
class QueueItem:
    """One question on the frontier, projected for the webui queue pane.

    ``episode_id`` is the subtask/episode the worker's draft belongs to; it is
    the reward-write target. ``consumed_upstreams`` lists the upstream episode
    ids whose output a synthesis draft read — the targets for synthesis
    fractional credit on accept / reject (empty for a plain subtask draft).
    """

    id: str
    prompt: str
    specialty: str
    status: QueueStatus = QueueStatus.PROPOSED
    origin_task_id: str | None = None
    origin_question_id: str | None = None
    proposed_by: str = "operator"
    episode_id: str | None = None
    consumed_upstreams: tuple[str, ...] = ()
    created_at_ms: int = 0
    approved_at_ms: int | None = None
    dispatched_at_ms: int | None = None
    drafted_at_ms: int | None = None
    curated_at_ms: int | None = None
    decision: CurationDecision | None = None
    corrected_answer: str | None = None

    def to_frame(self) -> dict:
        """Serialize to the WS/JSON shape consumed by ``webui/src/queue``."""
        return {
            "id": self.id,
            "prompt": self.prompt,
            "specialty": self.specialty,
            "status": self.status.value,
            "origin_task_id": self.origin_task_id,
            "origin_question_id": self.origin_question_id,
            "proposed_by": self.proposed_by,
            "episode_id": self.episode_id,
            "consumed_upstreams": list(self.consumed_upstreams),
            "created_at_ms": self.created_at_ms,
            "approved_at_ms": self.approved_at_ms,
            "dispatched_at_ms": self.dispatched_at_ms,
            "drafted_at_ms": self.drafted_at_ms,
            "curated_at_ms": self.curated_at_ms,
            "decision": self.decision.value if self.decision is not None else None,
            "corrected_answer": self.corrected_answer,
        }


# A delta's ``action`` names the transition that produced it. ``snapshot``
# is reserved for the full-state frame; deltas carry one of these.
QueueAction = str  # "add" | "approve" | "dispatch" | "draft" | "accept" | "reject" | "edit"

# The gateway passes ``TelemetrySink._broadcast`` here (async).
BroadcastFn = Callable[[dict], Awaitable[None]]


class QueueItemNotFoundError(KeyError):
    """Raised when an operation targets an ``item_id`` the manager never saw."""


class QueueManager:
    """In-memory projection of the question frontier + reward emitter.

    Construction takes the shared ``EpisodeRewardsStore`` (the same instance
    the Discord path and the nightly critic-fallback sweep write to) and an
    optional ``broadcast`` callback for WS fan-out. ``now_ms`` is injectable
    for deterministic tests, mirroring ``ReactionListener``.
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
        self._items: dict[str, QueueItem] = {}

    # ── ingestion ──────────────────────────────────────────────────────────

    async def add(self, item: QueueItem) -> QueueItem:
        """Add an item. Idempotent on ``id`` (first write wins).

        Broadcasts a ``queue.delta`` with ``action="add"`` for a genuinely new
        item; a duplicate ``add`` is a silent no-op (no second frame).
        """
        if item.id in self._items:
            return self._items[item.id]
        self._items[item.id] = item
        await self._emit_delta("add", item)
        return item

    # ── transitions (no reward) ──────────────────────────────────────────────

    async def approve(self, item_id: str) -> QueueItem:
        """proposed → approved (operator approval; not yet a reward signal)."""
        item = self._require(item_id)
        if item.status is QueueStatus.APPROVED:
            return item  # idempotent
        updated = replace(item, status=QueueStatus.APPROVED, approved_at_ms=self._now_ms())
        return await self._commit("approve", updated)

    async def dispatch(self, item_id: str) -> QueueItem:
        """approved → in-flight (the nightly dispatcher fanned this out)."""
        item = self._require(item_id)
        if item.status is QueueStatus.IN_FLIGHT:
            return item
        updated = replace(item, status=QueueStatus.IN_FLIGHT, dispatched_at_ms=self._now_ms())
        return await self._commit("dispatch", updated)

    async def draft(
        self,
        item_id: str,
        *,
        episode_id: str,
        consumed_upstreams: Sequence[str] = (),
    ) -> QueueItem:
        """in-flight → drafted (a worker returned a result for review).

        Stamps the ``episode_id`` (the reward target) and any
        ``consumed_upstreams`` (synthesis fractional-credit targets).
        """
        item = self._require(item_id)
        if item.status is QueueStatus.DRAFTED and item.episode_id == episode_id:
            return item
        updated = replace(
            item,
            status=QueueStatus.DRAFTED,
            drafted_at_ms=self._now_ms(),
            episode_id=episode_id,
            consumed_upstreams=tuple(consumed_upstreams),
        )
        return await self._commit("draft", updated)

    # ── curation decisions (reward-writing) ──────────────────────────────────

    async def accept(self, item_id: str) -> QueueItem:
        """Curate-accept: +1.0 to the episode (+0.3 to consumed upstreams)."""
        return await self._curate(item_id, CurationDecision.ACCEPT, ACCEPT_REWARD)

    async def reject(self, item_id: str) -> QueueItem:
        """Curate-reject: −1.0 to the episode (−0.3 to consumed upstreams)."""
        return await self._curate(item_id, CurationDecision.REJECT, REJECT_REWARD)

    async def edit(self, item_id: str, *, corrected_answer: str) -> QueueItem:
        """Curate-edit: +0.3 partial credit; captures the corrected answer.

        Edit is net-positive-but-imperfect, exactly the Discord/morning-curation
        semantics: the draft was salvageable but needed correction. Edit does
        **not** fan out synthesis fractional credit — the correction is the
        operator's own answer, not an endorsement of the upstreams.
        """
        return await self._curate(
            item_id,
            CurationDecision.EDIT,
            EDIT_REWARD,
            corrected_answer=corrected_answer,
            credit_upstreams=False,
        )

    # ── reads ────────────────────────────────────────────────────────────────

    def get(self, item_id: str) -> QueueItem:
        return self._require(item_id)

    def all(self) -> list[QueueItem]:
        """Every item, oldest-first by creation (stable tiebreak on id)."""
        return sorted(self._items.values(), key=lambda i: (i.created_at_ms, i.id))

    def by_status(self, status: QueueStatus) -> list[QueueItem]:
        return [i for i in self.all() if i.status is status]

    def snapshot_frame(self) -> dict:
        """The full-state ``queue.snapshot`` WS frame (sent on WS connect)."""
        return {
            "type": "queue.snapshot",
            "items": [i.to_frame() for i in self.all()],
            "timestamp_ms": self._now_ms(),
        }

    def __len__(self) -> int:
        return len(self._items)

    # ── internals ────────────────────────────────────────────────────────────

    def _require(self, item_id: str) -> QueueItem:
        try:
            return self._items[item_id]
        except KeyError as exc:
            raise QueueItemNotFoundError(item_id) from exc

    async def _curate(
        self,
        item_id: str,
        decision: CurationDecision,
        value: float,
        *,
        corrected_answer: str | None = None,
        credit_upstreams: bool = True,
    ) -> QueueItem:
        item = self._require(item_id)
        # Idempotency: a second decision on a curated item never re-writes the
        # reward. Mirrors the Discord listener's _already_recorded guard.
        if item.status is QueueStatus.CURATED:
            return item
        now = self._now_ms()
        self._write_reward(item, value=value, recorded_at_ms=now, credit_upstreams=credit_upstreams)
        updated = replace(
            item,
            status=QueueStatus.CURATED,
            curated_at_ms=now,
            decision=decision,
            corrected_answer=corrected_answer,
        )
        return await self._commit(decision.value, updated)

    def _write_reward(
        self,
        item: QueueItem,
        *,
        value: float,
        recorded_at_ms: int,
        credit_upstreams: bool,
    ) -> None:
        """Write the curation reward(s) for ``item`` into the shared store.

        Magnitudes are VERBATIM from the Discord surface (ADR 0010 §1):
        the episode gets the full ``value`` (±1.0 accept/reject, +0.3 edit);
        synthesis-consumed upstreams each get the ±0.3 fractional credit, with
        the sign of ``value`` — identical to ``write_synthesis_thumb``.
        """
        if item.episode_id is None:
            # No worker draft → nothing to reward. A guard, not an error: the
            # operator can reject a proposed item before it ever ran.
            return
        if self._already_rewarded(item.episode_id):
            return
        write_morning_curation(
            self._rewards,
            episode_id=item.episode_id,
            value=value,
            recorded_at_ms=recorded_at_ms,
        )
        if credit_upstreams:
            sign = 1.0 if value >= 0 else -1.0
            self._credit_upstreams(
                item.consumed_upstreams, sign=sign, recorded_at_ms=recorded_at_ms
            )

    def _credit_upstreams(
        self, upstreams: Iterable[str], *, sign: float, recorded_at_ms: int
    ) -> None:
        """Fan out ±0.3 synthesis fractional credit to consumed upstreams.

        Logged under ``SYNTHESIS_THUMB_FRACTIONAL`` — the same source the
        Discord synthesis thumb used — so the audit trail and downstream
        corpus builder cannot tell a webui-attributed credit from a Discord one.
        """
        for upstream_id in upstreams:
            if not upstream_id or self._already_rewarded(upstream_id):
                continue
            self._rewards.append(
                RewardEvent(
                    episode_id=upstream_id,
                    source=RewardSource.SYNTHESIS_THUMB_FRACTIONAL,
                    value=sign * SYNTHESIS_FRACTIONAL_CREDIT,
                    recorded_at_ms=recorded_at_ms,
                )
            )

    def _already_rewarded(self, episode_id: str) -> bool:
        """True if a curation/synthesis reward already exists for ``episode_id``.

        Guards against double-writing on a redelivered endpoint call. Only the
        webui-emitted sources count — a prior subtask thumb or critic fallback
        does not block a fresh curation decision (they stack, per ADR 0006).
        """
        for event in self._rewards.events_for(episode_id):
            if event.source in (
                RewardSource.MORNING_CURATION,
                RewardSource.SYNTHESIS_THUMB_FRACTIONAL,
            ):
                return True
        return False

    async def _commit(self, action: QueueAction, updated: QueueItem) -> QueueItem:
        self._items[updated.id] = updated
        await self._emit_delta(action, updated)
        return updated

    async def _emit_delta(self, action: QueueAction, item: QueueItem) -> None:
        if self._broadcast is None:
            return
        await self._broadcast(
            {
                "type": "queue.delta",
                "action": action,
                "item": item.to_frame(),
                "timestamp_ms": self._now_ms(),
            }
        )
