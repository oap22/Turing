"""Proposed follow-up question queue (ADR 0009 §2 "Night" + "Frontier control").

While researching, a worker **may propose follow-up questions** — but in
Phase 0 it does **not** auto-pursue them. Proposals ride back **attached to the
worker's result** (a ``proposed`` payload on the ``TaskResult`` fragment, *not*
new dispatches the worker publishes itself), and the coordinator lands them in
this **holding queue**, kept strictly separate from the approved/runnable
:class:`~turing.coordinator.flywheel.question_queue.QuestionQueue`.

Nothing here is ever dispatched automatically: this queue is not wired to the
:class:`~turing.coordinator.flywheel.nightly_dispatcher.NightlyDispatcher`,
which only drains the runnable queue. A proposal becomes runnable solely by a
human approving it into the next night's queue (the "Frontier control" slice).

Each proposal carries provenance — the ``origin_task_id`` / ``origin_question_id``
it grew out of — so the morning reviewer has the context to judge it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from turing.coordinator.dispatch import TaskResult

# The key under which a worker attaches its follow-up proposals to the generic
# ``TaskResult.fragment`` dict. A list of ``{"prompt": str, "specialty"?: str}``.
PROPOSED_FRAGMENT_KEY = "proposed_questions"


class ProposalStatus(StrEnum):
    PENDING = "pending"  # awaiting the morning frontier review
    APPROVED = "approved"  # human approved → eligible to promote into the runnable queue
    REJECTED = "rejected"  # human discarded


@dataclass(frozen=True)
class ProposedQuestion:
    """A follow-up question a worker proposed, held for human review."""

    proposal_id: str
    prompt: str
    specialty: str
    origin_task_id: str
    origin_question_id: str
    proposed_by: str  # worker id
    created_at_ms: int = 0
    status: ProposalStatus = ProposalStatus.PENDING


def proposals_to_fragment(
    drafts: Iterable[tuple[str, str]],
) -> dict[str, Any]:
    """Build the ``proposed`` payload a worker attaches to its result.

    ``drafts`` is an iterable of ``(prompt, specialty)`` pairs. The result is
    merged into ``TaskResult.fragment`` under :data:`PROPOSED_FRAGMENT_KEY`.
    """
    return {
        PROPOSED_FRAGMENT_KEY: [
            {"prompt": prompt, "specialty": specialty} for prompt, specialty in drafts
        ]
    }


def proposals_from_result(result: TaskResult) -> list[dict[str, str]]:
    """Extract the raw proposed-question dicts a worker attached, if any.

    Tolerant of a missing fragment / key (a worker proposing nothing is the
    common case) and skips malformed entries (those without a non-empty prompt).
    """
    fragment = result.fragment or {}
    raw = fragment.get(PROPOSED_FRAGMENT_KEY, [])
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt", "")).strip()
        if not prompt:
            continue
        out.append({"prompt": prompt, "specialty": str(item.get("specialty", ""))})
    return out


class ProposedQueue:
    """In-memory holding queue for worker-proposed follow-up questions.

    Deliberately has **no** dispatch method — proposals cannot leave this queue
    onto the wire. They are only ever surfaced for human review and, once
    approved, promoted into the runnable queue by a separate (human-gated) path.
    """

    def __init__(self) -> None:
        self._proposals: dict[str, ProposedQuestion] = {}

    def add(self, proposal: ProposedQuestion) -> None:
        """Add a proposal. Idempotent on ``proposal_id`` (first write wins)."""
        self._proposals.setdefault(proposal.proposal_id, proposal)

    def ingest_from_result(
        self,
        result: TaskResult,
        *,
        origin_task_id: str,
        origin_question_id: str,
        now_ms: int,
        default_specialty: str = "",
    ) -> list[ProposedQuestion]:
        """Land every follow-up a worker attached to ``result`` into the queue.

        Returns the proposals created. A proposal with no specialty inherits
        ``default_specialty`` (the originating question's specialty). This is
        the *only* path proposals enter the queue, and it never dispatches.
        """
        created: list[ProposedQuestion] = []
        for raw in proposals_from_result(result):
            proposal = ProposedQuestion(
                proposal_id=f"prop-{uuid.uuid4().hex[:12]}",
                prompt=raw["prompt"],
                specialty=raw["specialty"] or default_specialty,
                origin_task_id=origin_task_id,
                origin_question_id=origin_question_id,
                proposed_by=result.worker_id,
                created_at_ms=now_ms,
                status=ProposalStatus.PENDING,
            )
            self.add(proposal)
            created.append(proposal)
        return created

    def get(self, proposal_id: str) -> ProposedQuestion:
        return self._proposals[proposal_id]

    def all(self) -> list[ProposedQuestion]:
        return sorted(
            self._proposals.values(), key=lambda p: (p.created_at_ms, p.proposal_id)
        )

    def pending(self) -> list[ProposedQuestion]:
        """Proposals awaiting the morning frontier review, oldest-first."""
        return [p for p in self.all() if p.status is ProposalStatus.PENDING]

    def set_status(self, proposal_id: str, status: ProposalStatus) -> None:
        existing = self._proposals[proposal_id]
        self._proposals[proposal_id] = replace(existing, status=status)

    def __len__(self) -> int:
        return len(self._proposals)
