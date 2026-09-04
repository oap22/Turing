"""The taxonomy is closed, frozen, and classification is pure."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.rsi.contracts import CheatVerdict, RoundRecord, VerifierOutcome
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

if TYPE_CHECKING:
    from pathlib import Path

EXPECTED_MEMBERS = {
    "ENGINE_ERROR": "engine_error",
    "TIMEOUT": "timeout",
    "VERIFIER_FAILED": "verifier_failed",
    "VERIFIER_TAMPERED": "verifier_tampered",
    "NO_PROGRESS": "no_progress",
    "REGRESSED": "regressed",
    "NO_METRICS": "no_metrics",
    "CHEAT_DETECTED": "cheat_detected",
    "SANDBOX_ESCAPE": "sandbox_escape",
}

# Computed once from the nine members above. A future edit to the enum must
# fail this test rather than silently re-categorise every results directory.
PINNED_DIGEST = "2ee65add4eb8a03fe2466d53584904d12f7d78a374c45f74475ef646dc3dcd8d"


def _outcome(exit_code: int, score: float | None) -> VerifierOutcome:
    return VerifierOutcome(
        exit_code=exit_code, score=score, passed=exit_code == 0, stdout_tail="", wall_seconds=1.0
    )


def _classify(**overrides: object) -> frozenset[FailureCategory]:
    kwargs: dict[str, object] = {
        "engine_exit": 0,
        "timed_out": False,
        "verifier": _outcome(0, 1.0),
        "previous_score": None,
        "best_score": None,
        "had_metrics_line": True,
        "cheat": None,
    }
    kwargs.update(overrides)
    return classify_round(**kwargs)  # type: ignore[arg-type]


class TestEnum:
    def test_exactly_nine_members_with_pinned_values(self) -> None:
        assert {m.name: m.value for m in FailureCategory} == EXPECTED_MEMBERS
        assert len(FailureCategory) == 9

    def test_digest_is_pinned(self) -> None:
        assert TAXONOMY_VERSION == "1"
        assert TAXONOMY_DIGEST == PINNED_DIGEST
        assert taxonomy_digest() == PINNED_DIGEST

    def test_digest_changes_with_membership(self) -> None:
        subset = [c for c in FailureCategory if c is not FailureCategory.TIMEOUT]
        assert taxonomy_digest(subset) != PINNED_DIGEST

    def test_digest_is_order_independent(self) -> None:
        assert taxonomy_digest(reversed(list(FailureCategory))) == PINNED_DIGEST


class TestClassifyRound:
    def test_success_is_empty_set(self) -> None:
        assert _classify() == frozenset()

    def test_timeout_supersedes_engine_error(self) -> None:
        assert _classify(engine_exit=137, timed_out=True, verifier=None) == {
            FailureCategory.TIMEOUT
        }

    def test_engine_error(self) -> None:
        assert _classify(engine_exit=1, verifier=None) == {FailureCategory.ENGINE_ERROR}

    def test_verifier_failed(self) -> None:
        assert _classify(verifier=_outcome(2, None)) == {FailureCategory.VERIFIER_FAILED}

    def test_no_progress_when_not_better_than_best(self) -> None:
        assert _classify(verifier=_outcome(0, 1.0), best_score=1.0) == {FailureCategory.NO_PROGRESS}
        assert _classify(verifier=_outcome(0, 1.5), best_score=1.0) == frozenset()

    def test_regressed_and_no_progress_can_coexist(self) -> None:
        got = _classify(verifier=_outcome(0, 0.5), previous_score=0.8, best_score=0.9)
        assert got == {FailureCategory.NO_PROGRESS, FailureCategory.REGRESSED}

    def test_regressed_without_no_progress(self) -> None:
        # Worse than last round yet still a new best: REGRESSED alone.
        got = _classify(verifier=_outcome(0, 2.0), previous_score=3.0, best_score=1.0)
        assert got == {FailureCategory.REGRESSED}

    def test_pass_fail_only_first_pass_is_progress(self) -> None:
        assert _classify(verifier=_outcome(0, None), best_score=None) == frozenset()

    def test_pass_fail_only_later_pass_is_no_progress(self) -> None:
        assert _classify(verifier=_outcome(0, None), best_score=1.0) == {
            FailureCategory.NO_PROGRESS
        }

    def test_no_metrics_rides_alongside(self) -> None:
        assert _classify(had_metrics_line=False) == {FailureCategory.NO_METRICS}
        assert _classify(had_metrics_line=False, verifier=_outcome(1, None)) == {
            FailureCategory.NO_METRICS,
            FailureCategory.VERIFIER_FAILED,
        }

    def test_cheat_categories_merge_in(self) -> None:
        verdict = CheatVerdict(
            fired=True,
            categories=frozenset({FailureCategory.CHEAT_DETECTED}),
            reasons=("self-reported score 9.0 vs measured 1.0",),
        )
        assert _classify(cheat=verdict) == {FailureCategory.CHEAT_DETECTED}

    def test_unfired_cheat_adds_nothing(self) -> None:
        assert _classify(cheat=CheatVerdict(fired=False)) == frozenset()

    def test_deterministic(self) -> None:
        a = _classify(verifier=_outcome(0, 0.5), previous_score=0.8, best_score=0.9)
        b = _classify(verifier=_outcome(0, 0.5), previous_score=0.8, best_score=0.9)
        assert a == b


class TestTaxonomyFile:
    def test_first_run_writes_file(self, results: Path) -> None:
        write_or_check_taxonomy(results)
        stored = json.loads((results / TAXONOMY_FILENAME).read_text())
        assert stored == {
            "version": "1",
            "digest": PINNED_DIGEST,
            "categories": EXPECTED_MEMBERS,
        }

    def test_creates_results_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "new" / "loop-rsi-x"
        write_or_check_taxonomy(target)
        assert (target / TAXONOMY_FILENAME).is_file()

    def test_second_run_accepts_same_digest(self, results: Path) -> None:
        write_or_check_taxonomy(results)
        write_or_check_taxonomy(results)

    def test_digest_mismatch_refuses(self, results: Path) -> None:
        write_or_check_taxonomy(results)
        path = results / TAXONOMY_FILENAME
        stored = json.loads(path.read_text())
        stored["digest"] = "0" * 64
        path.write_text(json.dumps(stored))
        with pytest.raises(ContractViolationError, match="digest mismatch"):
            write_or_check_taxonomy(results)

    def test_malformed_file_refuses(self, results: Path) -> None:
        (results / TAXONOMY_FILENAME).write_text("{not json")
        with pytest.raises(ContractViolationError, match="unreadable"):
            write_or_check_taxonomy(results)
        (results / TAXONOMY_FILENAME).write_text(json.dumps({"version": "1"}))
        with pytest.raises(ContractViolationError, match="malformed"):
            write_or_check_taxonomy(results)


class TestCounts:
    def test_zero_filled_and_counted(self) -> None:
        records = [
            RoundRecord(round=1, started=0, ended=1, exit=0),
            RoundRecord(
                round=2,
                started=0,
                ended=1,
                exit=1,
                categories=frozenset({FailureCategory.ENGINE_ERROR, FailureCategory.NO_METRICS}),
            ),
            RoundRecord(
                round=3,
                started=0,
                ended=1,
                exit=0,
                categories=frozenset({FailureCategory.NO_METRICS}),
            ),
        ]
        counts = taxonomy_counts(records)
        assert set(counts) == set(EXPECTED_MEMBERS.values())
        assert counts["no_metrics"] == 2
        assert counts["engine_error"] == 1
        assert counts["timeout"] == 0

    def test_empty(self) -> None:
        assert all(v == 0 for v in taxonomy_counts([]).values())
