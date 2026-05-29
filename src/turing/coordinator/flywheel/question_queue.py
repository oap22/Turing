"""Human-gated research-question queue (ADR 0009 §2 "Night").

The coordinator holds a persisted queue of research questions. **Only
human-approved questions are eligible to be dispatched** to the workers on a
night's run — this is the load-bearing "frontier is human-gated in Phase 0"
rule. Questions enter the queue unapproved (e.g. promoted from the ``proposed``
holding queue) and a human flips ``approved`` before they can ever run.

State model per question:

    added  ──approve()──▶  approved  ──mark_dispatched()──▶  dispatched

``drain_approved()`` returns the approved-and-not-yet-dispatched questions —
the exact set the :class:`~turing.coordinator.flywheel.nightly_dispatcher.NightlyDispatcher`
fans out across the fleet. The store is in-memory, mirroring ``EpisodeStore``
and ``CapabilityRegistry``; idempotent on ``question_id`` (first write wins).
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ResearchQuestion:
    """A single research question on the coordinator's frontier.

    ``origin_question_id`` carries provenance for questions promoted from the
    ``proposed`` holding queue (the question this one grew out of); it is
    ``None`` for hand-seeded questions.
    """

    question_id: str
    prompt: str
    specialty: str
    approved: bool = False
    created_at_ms: int = 0
    dispatched_at_ms: int | None = None
    origin_question_id: str | None = None

    @property
    def is_runnable(self) -> bool:
        """Approved and not yet dispatched — eligible for a night's run."""
        return self.approved and self.dispatched_at_ms is None


class QuestionQueue:
    """In-memory, human-gated queue of research questions."""

    def __init__(self) -> None:
        self._questions: dict[str, ResearchQuestion] = {}

    def add(self, question: ResearchQuestion) -> None:
        """Add a question. Idempotent: first write for a ``question_id`` wins."""
        self._questions.setdefault(question.question_id, question)

    def get(self, question_id: str) -> ResearchQuestion:
        """Return the question, or raise ``KeyError`` if unknown."""
        return self._questions[question_id]

    def all(self) -> list[ResearchQuestion]:
        """Snapshot of every question, oldest-first by creation time."""
        return sorted(self._questions.values(), key=lambda q: (q.created_at_ms, q.question_id))

    def approve(self, question_id: str) -> None:
        """Mark a question human-approved (eligible for dispatch).

        Raises ``KeyError`` for an unknown ``question_id``.
        """
        existing = self._questions[question_id]
        if not existing.approved:
            self._questions[question_id] = replace(existing, approved=True)

    def drain_approved(self) -> list[ResearchQuestion]:
        """Approved-and-not-yet-dispatched questions, oldest-first.

        Pure read — does not mutate. The dispatcher calls
        :meth:`mark_dispatched` once a question is actually sent so a re-run
        does not double-dispatch.
        """
        return [q for q in self.all() if q.is_runnable]

    def mark_dispatched(self, question_id: str, *, at_ms: int) -> None:
        """Stamp a question as dispatched so it is not drained again.

        First stamp wins (idempotent on redelivery / retried nightly runs).
        """
        existing = self._questions[question_id]
        if existing.dispatched_at_ms is None:
            self._questions[question_id] = replace(existing, dispatched_at_ms=at_ms)

    def __len__(self) -> int:
        return len(self._questions)
