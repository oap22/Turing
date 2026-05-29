"""Phase 0 research flywheel (ADR 0009).

The Phase 0 operating model is a *nightly batch + morning curation* loop:

- **Night** — the coordinator drains a **human-gated** question queue and
  dispatches the approved questions to the Jetson workers over NATS
  (:mod:`question_queue`, :mod:`nightly_dispatcher`). Workers may attach
  **proposed follow-up questions** to their results; those land in a separate
  holding queue and are *never* auto-pursued in Phase 0
  (:mod:`proposed_queue`).
- **Morning** — the operator reviews the overnight drafts and accepts / rejects
  / edits them; those decisions are the Phase 0 reward signal
  (:mod:`morning_curation`).
- **Frontier control** — proposed follow-ups are expanded, eliminated, and
  deduped before a human approves which ones enter the next night's queue
  (:mod:`frontier`).

Every module here is deliberately small, dependency-light, and in-memory —
mirroring the rest of the coordinator's state stores (``EpisodeStore``,
``LessonStore``, ``CapabilityRegistry``).
"""

from __future__ import annotations

from turing.coordinator.flywheel.nightly_dispatcher import (
    NightlyDispatcher,
    NightlyRunReport,
)
from turing.coordinator.flywheel.question_queue import QuestionQueue, ResearchQuestion

__all__ = [
    "NightlyDispatcher",
    "NightlyRunReport",
    "QuestionQueue",
    "ResearchQuestion",
]
