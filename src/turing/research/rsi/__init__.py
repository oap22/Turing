"""The RSI workstation loop as a Python engine.

Successor to ``scripts/rsi-loop.sh``: the same on-disk layout (sandbox
``~/turing-workspace/rsi-<slug>``, results ``<root>/loop-rsi-<slug>``,
append-only ``trajectory.json`` JSONL, ``metrics.jsonl``, ``NOTES.md``,
``PROBLEM.md``, ``STOP``) plus a frozen verifier, a frozen error taxonomy, a
scaffold self-edit step with rollback, and a cheat detector.

:mod:`turing.research.rsi.contracts` and :mod:`turing.research.rsi.taxonomy`
are the types every other module codes against; see their docstrings for
what each guarantees.
"""

from turing.research.rsi.contracts import (
    CheatVerdict,
    ContractViolationError,
    Engine,
    EngineResult,
    FrozenVerifierError,
    LoopEvent,
    RoundRecord,
    RoundSummary,
    RsiConfig,
    SelfEditInputs,
    SelfEditStep,
    VerifierLock,
    VerifierOutcome,
    VerifierSpec,
    check_verifier_lock,
    compute_verifier_lock,
    parse_score,
)
from turing.research.rsi.taxonomy import (
    TAXONOMY_DIGEST,
    TAXONOMY_VERSION,
    FailureCategory,
    classify_round,
    taxonomy_counts,
    write_or_check_taxonomy,
)

__all__ = [
    "TAXONOMY_DIGEST",
    "TAXONOMY_VERSION",
    "CheatVerdict",
    "ContractViolationError",
    "Engine",
    "EngineResult",
    "FailureCategory",
    "FrozenVerifierError",
    "LoopEvent",
    "RoundRecord",
    "RoundSummary",
    "RsiConfig",
    "SelfEditInputs",
    "SelfEditStep",
    "VerifierLock",
    "VerifierOutcome",
    "VerifierSpec",
    "check_verifier_lock",
    "classify_round",
    "compute_verifier_lock",
    "parse_score",
    "taxonomy_counts",
    "write_or_check_taxonomy",
]
