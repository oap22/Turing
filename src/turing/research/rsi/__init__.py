"""The RSI workstation loop as a Python engine.

Successor to ``scripts/rsi-loop.sh``: the same on-disk layout (sandbox
``~/turing-workspace/rsi-<slug>``, results ``<root>/loop-rsi-<slug>``,
append-only ``trajectory.json`` JSONL, ``metrics.jsonl``, ``NOTES.md``,
``PROBLEM.md``, ``STOP``) plus a frozen verifier, a frozen error taxonomy, a
scaffold self-edit step with rollback, and a cheat detector.

Module map (each states what it guarantees and what it does not):

- :mod:`~turing.research.rsi.contracts` / :mod:`~turing.research.rsi.taxonomy`
  — the frozen types and the closed failure taxonomy every other module codes
  against.
- :mod:`~turing.research.rsi.engine` — the :class:`Engine` protocol's
  implementations (``claude -p``, ``codex exec``, and a scripted fake).
- :mod:`~turing.research.rsi.verifier` — the verifier lock on disk and the
  loop-side measurement of the score.
- :mod:`~turing.research.rsi.cheat` — the per-round cheat detector and the
  neutral ``git`` helper.
- :mod:`~turing.research.rsi.scaffold` — the SCAFFOLD.md self-edit step.
- :mod:`~turing.research.rsi.loop` — the round loop that ties them together.
- :mod:`~turing.research.rsi.cli` — ``python -m turing.research.rsi``.

This package does NOT run anything at import time and does not touch the
filesystem until :meth:`RsiLoop.prepare` or :func:`cli.main` is called.
"""

from turing.research.rsi.cheat import CheatDetector, CheatSnapshot, GitResult, git_env, run_git
from turing.research.rsi.cli import EXIT_INTERRUPTED, EXIT_STOPPED, EXIT_USAGE, Plan, resolve_plan
from turing.research.rsi.cli import main as cli_main
from turing.research.rsi.contracts import (
    BASH_TRAJECTORY_KEYS,
    SLUG_PATTERN,
    VERIFIER_LOCK_FILENAME,
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
    iter_forbidden,
    parse_score,
    sha256_file,
    sha256_text,
)
from turing.research.rsi.engine import (
    ClaudeCliEngine,
    CodexCliEngine,
    FakeCall,
    FakeEngine,
    ok_result,
)
from turing.research.rsi.loop import (
    EXIT_OK,
    LoopOutcome,
    RsiLoop,
    StopReason,
    TrajectoryState,
    build_round_prompt,
    describe_plan,
    read_trajectory,
)
from turing.research.rsi.scaffold import (
    DEFAULT_SCAFFOLD_TEXT,
    SCAFFOLD_FILENAME,
    ScaffoldSelfEditStep,
    build_self_edit_inputs,
    render_self_edit_prompt,
    rollback_scaffold,
    rollback_scaffold_async,
    scaffold_head,
    scaffold_head_async,
)
from turing.research.rsi.taxonomy import (
    TAXONOMY_DIGEST,
    TAXONOMY_FILENAME,
    TAXONOMY_VERSION,
    FailureCategory,
    classify_round,
    taxonomy_counts,
    taxonomy_digest,
    write_or_check_taxonomy,
)
from turing.research.rsi.verifier import (
    load_verifier_lock,
    run_verifier,
    sandbox_file_tokens,
    write_or_load_verifier,
)

__all__ = [
    "BASH_TRAJECTORY_KEYS",
    "DEFAULT_SCAFFOLD_TEXT",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_STOPPED",
    "EXIT_USAGE",
    "SCAFFOLD_FILENAME",
    "SLUG_PATTERN",
    "TAXONOMY_DIGEST",
    "TAXONOMY_FILENAME",
    "TAXONOMY_VERSION",
    "VERIFIER_LOCK_FILENAME",
    "CheatDetector",
    "CheatSnapshot",
    "CheatVerdict",
    "ClaudeCliEngine",
    "CodexCliEngine",
    "ContractViolationError",
    "Engine",
    "EngineResult",
    "FailureCategory",
    "FakeCall",
    "FakeEngine",
    "FrozenVerifierError",
    "GitResult",
    "LoopEvent",
    "LoopOutcome",
    "Plan",
    "RoundRecord",
    "RoundSummary",
    "RsiConfig",
    "RsiLoop",
    "ScaffoldSelfEditStep",
    "SelfEditInputs",
    "SelfEditStep",
    "StopReason",
    "TrajectoryState",
    "VerifierLock",
    "VerifierOutcome",
    "VerifierSpec",
    "build_round_prompt",
    "build_self_edit_inputs",
    "check_verifier_lock",
    "classify_round",
    "cli_main",
    "compute_verifier_lock",
    "describe_plan",
    "git_env",
    "iter_forbidden",
    "load_verifier_lock",
    "ok_result",
    "parse_score",
    "read_trajectory",
    "render_self_edit_prompt",
    "resolve_plan",
    "rollback_scaffold",
    "rollback_scaffold_async",
    "run_git",
    "run_verifier",
    "sandbox_file_tokens",
    "scaffold_head",
    "scaffold_head_async",
    "sha256_file",
    "sha256_text",
    "taxonomy_counts",
    "taxonomy_digest",
    "write_or_check_taxonomy",
    "write_or_load_verifier",
]
