"""Morning frontier review — the proposed → runnable bridge (ADR 0009 §2, #263).

Slice 2's holding queue (:class:`~turing.coordinator.flywheel.proposed_queue.ProposedQueue`)
and runnable queue (:class:`~turing.coordinator.flywheel.question_queue.QuestionQueue`)
existed but were never joined: there was no path that took a worker-proposed
follow-up and made it dispatchable. This is that path, and it is the **only**
one — keeping the "frontier is human-gated in Phase 0" invariant intact.

In the morning the operator reviews each pending proposal and either:

* **approve** — promote it into the runnable queue (carrying its
  ``origin_question_id`` provenance) so the *next* nightly run dispatches it.
  The proposal-approval *is* the human gate, so the promoted question lands
  already approved.
* **decline** — record the rejection (logged with provenance); it never
  becomes runnable and is never dispatched.

Promotion is idempotent: the runnable question's id is derived from the
proposal id, and :meth:`QuestionQueue.add` is first-write-wins, so approving the
same proposal twice yields one runnable question.

Scope: this bridges the *flywheel* queues (the Phase 0 SSH/morning-review
surface). The webui queue manager (ADR 0010 Slice C) is a separate projection;
reconciling the two surfaces is deliberately out of scope here so we don't grow
a third, divergent approval path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from turing.coordinator.flywheel.proposed_queue import ProposalStatus
from turing.coordinator.flywheel.question_queue import ResearchQuestion

if TYPE_CHECKING:
    from turing.coordinator.flywheel.proposed_queue import ProposedQuestion, ProposedQueue
    from turing.coordinator.flywheel.question_queue import QuestionQueue

logger = structlog.get_logger("turing.flywheel.frontier_review")

_PROMOTED_PREFIX = "q"


class FrontierReview:
    """Human-gated promotion of proposed follow-ups into the runnable queue."""

    def __init__(self, *, proposed: ProposedQueue, queue: QuestionQueue) -> None:
        self._proposed = proposed
        self._queue = queue

    def approve(self, proposal_id: str, *, now_ms: int) -> ResearchQuestion:
        """Promote a pending proposal into the runnable queue for the next night.

        Marks the proposal ``APPROVED`` and adds a matching, already-approved
        :class:`ResearchQuestion` (carrying ``origin_question_id`` provenance) to
        the runnable queue. Idempotent on the proposal id. Raises ``KeyError``
        for an unknown proposal.
        """
        proposal = self._proposed.get(proposal_id)
        self._proposed.set_status(proposal_id, ProposalStatus.APPROVED)
        question = ResearchQuestion(
            question_id=promoted_question_id(proposal),
            prompt=proposal.prompt,
            specialty=proposal.specialty,
            approved=True,  # the proposal approval IS the human gate
            created_at_ms=now_ms,
            origin_question_id=proposal.origin_question_id,
        )
        self._queue.add(question)
        logger.info(
            "frontier_promoted",
            proposal_id=proposal_id,
            question_id=question.question_id,
            origin_question_id=proposal.origin_question_id,
            specialty=proposal.specialty,
        )
        return question

    def decline(self, proposal_id: str) -> None:
        """Decline a proposal: record the rejection; it never becomes runnable.

        Marks the proposal ``REJECTED`` and logs the decision with provenance.
        No question is added to the runnable queue, so a declined proposal is
        never dispatched. Raises ``KeyError`` for an unknown proposal.
        """
        proposal = self._proposed.get(proposal_id)
        self._proposed.set_status(proposal_id, ProposalStatus.REJECTED)
        logger.info(
            "frontier_declined",
            proposal_id=proposal_id,
            prompt=proposal.prompt,
            origin_question_id=proposal.origin_question_id,
        )


def promoted_question_id(proposal: ProposedQuestion) -> str:
    """Deterministic runnable-question id for a promoted proposal.

    Derived from the proposal id so re-approving the same proposal maps to the
    same question id — making promotion idempotent against
    :meth:`QuestionQueue.add` (first-write-wins).
    """
    return f"{_PROMOTED_PREFIX}-{proposal.proposal_id}"
