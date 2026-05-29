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

from turing.coordinator.flywheel.generalist import (
    AI_ML_GENERALIST,
    GENERALIST_FLEET_SIZE,
    GeneralistAdapterRollout,
    GeneralistFleet,
    replicate_to_fleet,
    specialty_eval_dir,
)
from turing.coordinator.flywheel.morning_curation import (
    CurationDecision,
    CurationRecord,
    InboxDraft,
    MorningCuration,
    SFTCandidate,
    parse_inbox_draft,
)
from turing.coordinator.flywheel.nightly_dispatcher import (
    NightlyDispatcher,
    NightlyRunReport,
)
from turing.coordinator.flywheel.proposed_queue import (
    PROPOSED_FRAGMENT_KEY,
    ProposalStatus,
    ProposedQuestion,
    ProposedQueue,
    proposals_from_result,
    proposals_to_fragment,
)
from turing.coordinator.flywheel.question_queue import QuestionQueue, ResearchQuestion

__all__ = [
    "AI_ML_GENERALIST",
    "GENERALIST_FLEET_SIZE",
    "PROPOSED_FRAGMENT_KEY",
    "CurationDecision",
    "GeneralistAdapterRollout",
    "GeneralistFleet",
    "CurationRecord",
    "InboxDraft",
    "MorningCuration",
    "NightlyDispatcher",
    "NightlyRunReport",
    "ProposalStatus",
    "ProposedQuestion",
    "ProposedQueue",
    "QuestionQueue",
    "ResearchQuestion",
    "SFTCandidate",
    "parse_inbox_draft",
    "proposals_from_result",
    "proposals_to_fragment",
    "replicate_to_fleet",
    "specialty_eval_dir",
]
