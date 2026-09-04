"""The frozen error taxonomy for the RSI workstation loop.

This module closes OPEN-QUESTIONS Q15 for the RSI loop: failures are
categorised by a **closed, frozen** enum, and the categorisation is a pure
function of the round's measured facts. ADR 0011 §2 gates the taxonomy
*before round 0*, because aggregate stats are only comparable if failures
are categorised identically every round.

What this module guarantees:

* :class:`FailureCategory` has exactly nine members. :data:`TAXONOMY_DIGEST`
  is the SHA-256 over the sorted ``name=value`` pairs, so any edit to the
  enum changes the digest; ``test_taxonomy.py`` pins the literal hex.
* :func:`write_or_check_taxonomy` writes ``<results>/taxonomy.json`` on a
  results directory's first run and, on every later run, refuses to start
  (:class:`~turing.research.contracts.ContractViolationError`) if the stored
  digest differs from the one this code computes.
* :func:`classify_round` is pure and deterministic: same inputs, same
  frozenset, no I/O, no clock.

What it does not do:

* It does not detect cheats. The detector's verdict arrives as an input
  (:class:`~turing.research.rsi.contracts.CheatVerdict`); the classifier only
  merges its categories in.
* It does not decide whether the loop stops. That is the loop's job, driven
  by the categories this module returns.
* It does not track ``best_score`` itself. The loop supplies it from numeric
  scores only, so the "a later pass on a pass/fail-only problem is
  ``NO_PROGRESS``" rule in :func:`classify_round` is unreachable in the
  running loop unless a numeric score was measured earlier.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from enum import Enum
from typing import TYPE_CHECKING

import structlog

from turing.research.contracts import ContractViolationError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from turing.research.rsi.contracts import CheatVerdict, RoundRecord, VerifierOutcome

logger = structlog.get_logger(__name__)

__all__ = [
    "TAXONOMY_DIGEST",
    "TAXONOMY_FILENAME",
    "TAXONOMY_VERSION",
    "FailureCategory",
    "classify_round",
    "taxonomy_counts",
    "taxonomy_digest",
    "write_or_check_taxonomy",
]


class FailureCategory(str, Enum):  # noqa: UP042
    """Why a round did not count as a clean improvement.

    Closed: adding, renaming or removing a member changes
    :data:`TAXONOMY_DIGEST`, which makes every existing results directory
    refuse to resume. That is intended — a re-categorised history is not the
    same history.

    A round carries a *set* of categories (see :func:`classify_round`); a
    round that passed the verifier and improved on the best score carries the
    empty set, which is the success sentinel.
    """

    #: ``claude`` exited non-zero or could not be started.
    ENGINE_ERROR = "engine_error"
    #: The round's wall-clock cap was hit.
    TIMEOUT = "timeout"
    #: The verifier exited non-zero.
    VERIFIER_FAILED = "verifier_failed"
    #: The verifier lock did not match before or after the round.
    VERIFIER_TAMPERED = "verifier_tampered"
    #: Verifier passed (or scored) but the score is not better than the best so far.
    NO_PROGRESS = "no_progress"
    #: Score is worse than the previous round's.
    REGRESSED = "regressed"
    #: The round produced no ``metrics.jsonl`` line (alongside, never instead of, the verdict).
    NO_METRICS = "no_metrics"
    #: The cheat detector fired on score integrity.
    CHEAT_DETECTED = "cheat_detected"
    #: The cheat detector fired on the sandbox boundary.
    SANDBOX_ESCAPE = "sandbox_escape"


TAXONOMY_VERSION: str = "1"
TAXONOMY_FILENAME: str = "taxonomy.json"


def taxonomy_digest(categories: Iterable[FailureCategory] = FailureCategory) -> str:
    """SHA-256 over the sorted ``name=value`` pairs, newline-joined, UTF-8.

    Exposed so a test can recompute it; production code uses
    :data:`TAXONOMY_DIGEST`.
    """
    pairs = sorted(f"{c.name}={c.value}" for c in categories)
    return hashlib.sha256("\n".join(pairs).encode("utf-8")).hexdigest()


TAXONOMY_DIGEST: str = taxonomy_digest()


def classify_round(
    *,
    engine_exit: int,
    timed_out: bool,
    verifier: VerifierOutcome | None,
    previous_score: float | None,
    best_score: float | None,
    had_metrics_line: bool,
    cheat: CheatVerdict | None,
) -> frozenset[FailureCategory]:
    """Categorise one round from its measured facts. Pure and deterministic.

    Rules, applied independently so a round can carry several categories:

    * ``timed_out`` → :attr:`FailureCategory.TIMEOUT`. A timed-out engine is
      killed, so its exit code is an artefact; ``ENGINE_ERROR`` is *not* added
      on top.
    * otherwise ``engine_exit != 0`` → :attr:`FailureCategory.ENGINE_ERROR`.
    * ``verifier`` is ``None`` (never ran) → no verifier category.
      ``verifier.passed`` false → :attr:`FailureCategory.VERIFIER_FAILED`.
    * a passing verifier with ``score is None`` is progress only on the
      first pass (``best_score is None``); later passes are
      :attr:`FailureCategory.NO_PROGRESS`.
    * a passing verifier with a score: not strictly greater than
      ``best_score`` → ``NO_PROGRESS``; strictly less than
      ``previous_score`` → :attr:`FailureCategory.REGRESSED` (both can hold).
      Higher is better.
    * ``not had_metrics_line`` → :attr:`FailureCategory.NO_METRICS`,
      alongside whatever else applies.
    * a fired ``cheat`` verdict contributes its own categories verbatim.

    ``best_score`` is the best *before* this round; the caller updates it
    afterwards. ``previous_score`` is the immediately preceding round's
    measured score (``None`` if that round had none).
    """
    categories: set[FailureCategory] = set()
    if timed_out:
        categories.add(FailureCategory.TIMEOUT)
    elif engine_exit != 0:
        categories.add(FailureCategory.ENGINE_ERROR)

    if verifier is not None:
        if not verifier.passed:
            categories.add(FailureCategory.VERIFIER_FAILED)
        elif verifier.score is None:
            if best_score is not None:
                categories.add(FailureCategory.NO_PROGRESS)
        else:
            if best_score is not None and verifier.score <= best_score:
                categories.add(FailureCategory.NO_PROGRESS)
            if previous_score is not None and verifier.score < previous_score:
                categories.add(FailureCategory.REGRESSED)

    if not had_metrics_line:
        categories.add(FailureCategory.NO_METRICS)

    if cheat is not None and cheat.fired:
        categories.update(cheat.categories)

    return frozenset(categories)


def write_or_check_taxonomy(results_dir: Path) -> None:
    """Write ``taxonomy.json`` on first run; refuse a digest mismatch on any later run.

    The file is ``{"version", "digest", "categories": {name: value}}``. The
    comparison is on ``digest`` alone — the other keys are for humans reading
    the results directory.

    Raises:
        ContractViolationError: the stored file is unreadable, malformed, or
            carries a digest other than :data:`TAXONOMY_DIGEST`.
    """
    path = results_dir / TAXONOMY_FILENAME
    if not path.exists():
        results_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": TAXONOMY_VERSION,
            "digest": TAXONOMY_DIGEST,
            "categories": {c.name: c.value for c in FailureCategory},
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        logger.info("rsi.taxonomy.written", path=str(path), digest=TAXONOMY_DIGEST)
        return

    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractViolationError(
            f"taxonomy file {path} is unreadable ({exc}); refusing to run against a "
            "results directory whose taxonomy cannot be checked"
        ) from exc
    if not isinstance(stored, dict) or not isinstance(stored.get("digest"), str):
        raise ContractViolationError(
            f"taxonomy file {path} is malformed (no string 'digest'); refusing to run"
        )
    stored_digest: str = stored["digest"]
    if stored_digest != TAXONOMY_DIGEST:
        raise ContractViolationError(
            f"taxonomy digest mismatch in {path}: stored {stored_digest}, this code "
            f"computes {TAXONOMY_DIGEST} (version {TAXONOMY_VERSION}). Aggregate stats "
            "are only comparable if failures are categorised identically every round; "
            "start a new results directory rather than mixing taxonomies"
        )
    logger.info("rsi.taxonomy.checked", path=str(path), digest=TAXONOMY_DIGEST)


def taxonomy_counts(records: Iterable[RoundRecord]) -> dict[str, int]:
    """Count category occurrences across rounds, keyed by category value.

    Every category is present (zero-filled) so a summary always has the same
    shape regardless of which failures happened. A round contributes one to
    each category it carries.
    """
    counter: Counter[str] = Counter({c.value: 0 for c in FailureCategory})
    for record in records:
        for category in record.categories:
            counter[category.value] += 1
    return {c.value: counter[c.value] for c in FailureCategory}
