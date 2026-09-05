"""Turing as an autonomous research agent — loop 1 (within-project autonomy).

One project is a ``(goal, verifier)`` pair. The agent works a project until the
frozen verifier passes or a cap trips; it may not quit on its own. Loop 2
(across-project scaffold self-modification) is **not** implemented here — see
``research/briefs/2026-08-12-autonomous-research-agent.md``.

Layout:

* :mod:`turing.research.contracts` — the shared types every other module below
  codes against. Re-exported here.
* :mod:`turing.research.problems` — concrete :class:`~turing.research.contracts.Verifier`
  implementations and the corpus.
* :mod:`turing.research.backends` — the "send messages + tools, get a response"
  seam, with a Claude implementation. A local implementation drops in later.
* :mod:`turing.research.solver` — one project attempt: build, score, revise,
  checkpoint, until the cap.
* :mod:`turing.research.loop` — round orchestration, escalation, round records.

Only ``contracts`` is re-exported from this package. The submodules are
imported directly so that importing a contract never drags in a backend.
"""

from __future__ import annotations

from turing.research.contracts import (
    CORE_METRICS_FIELDS,
    DIAGNOSTIC_KEY_PREFIX,
    HARNESS_FAILURE_KEY,
    RESERVED_METRICS_FIELDS,
    RESERVED_METRICS_KEYS,
    SCORE_SCALE_LEADERBOARD_PERCENTILE,
    SCORE_SCALE_SPEEDUP,
    TERMINAL_ATTEMPT_STATES,
    Attempt,
    AttemptState,
    Cap,
    CapConsumption,
    CapDimension,
    CapExtension,
    CapExtensionError,
    ContractViolationError,
    EngineIdentity,
    EscalationDecision,
    EscalationProtocolError,
    EscalationReason,
    EscalationRequest,
    EscalationVerdict,
    EvalSetMismatchError,
    FrozenVerifierError,
    Problem,
    ProblemType,
    RoundCost,
    RoundDelta,
    RoundRecord,
    Split,
    TypeScore,
    VerificationResult,
    Verifier,
    round_cells,
)

__all__ = [
    "CORE_METRICS_FIELDS",
    "DIAGNOSTIC_KEY_PREFIX",
    "HARNESS_FAILURE_KEY",
    "RESERVED_METRICS_FIELDS",
    "RESERVED_METRICS_KEYS",
    "SCORE_SCALE_LEADERBOARD_PERCENTILE",
    "SCORE_SCALE_SPEEDUP",
    "TERMINAL_ATTEMPT_STATES",
    "Attempt",
    "AttemptState",
    "Cap",
    "CapConsumption",
    "CapDimension",
    "CapExtension",
    "CapExtensionError",
    "ContractViolationError",
    "EngineIdentity",
    "EscalationDecision",
    "EscalationProtocolError",
    "EscalationReason",
    "EscalationRequest",
    "EscalationVerdict",
    "EvalSetMismatchError",
    "FrozenVerifierError",
    "Problem",
    "ProblemType",
    "RoundCost",
    "RoundDelta",
    "RoundRecord",
    "Split",
    "TypeScore",
    "VerificationResult",
    "Verifier",
    "round_cells",
]
