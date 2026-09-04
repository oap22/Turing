"""Every invariant the RSI contracts encode."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from turing.research.contracts import ContractViolationError, FrozenVerifierError
from turing.research.rsi.contracts import (
    CheatVerdict,
    EngineResult,
    LoopEvent,
    RoundRecord,
    RoundSummary,
    RsiConfig,
    SelfEditInputs,
    VerifierLock,
    VerifierOutcome,
    VerifierSpec,
    check_verifier_lock,
    compute_verifier_lock,
    iter_forbidden,
    parse_score,
    sha256_text,
)
from turing.research.rsi.taxonomy import FailureCategory

from .conftest import bash_round

# --------------------------------------------------------------------------- #
# RsiConfig
# --------------------------------------------------------------------------- #


class TestRsiConfig:
    def test_layout_matches_bash_script(self, tmp_path: Path) -> None:
        cfg = RsiConfig(slug="my-exp-1", results_root=tmp_path / "r", workspace_root=tmp_path / "w")
        assert cfg.sandbox_dir == tmp_path / "w" / "rsi-my-exp-1"
        assert cfg.results_dir == tmp_path / "r" / "loop-rsi-my-exp-1"

    def test_defaults(self, tmp_path: Path) -> None:
        cfg = RsiConfig(slug="a", results_root=tmp_path)
        assert cfg.rounds == 10
        assert cfg.self_edit_every == 3
        assert cfg.self_edit_budget == 3
        assert cfg.noise_floor is None
        assert cfg.round_timeout_seconds == 1800.0
        assert cfg.verifier_timeout_seconds == 600.0
        assert cfg.workspace_root == Path("~/turing-workspace").expanduser()

    def test_tilde_expanded(self) -> None:
        cfg = RsiConfig(slug="a", results_root=Path("~/res"))
        assert not str(cfg.results_root).startswith("~")

    @pytest.mark.parametrize("slug", ["", "Bad", "has space", "under_score", "dots.", "é"])
    def test_bad_slug(self, slug: str, tmp_path: Path) -> None:
        with pytest.raises(ContractViolationError, match="slug"):
            RsiConfig(slug=slug, results_root=tmp_path)

    @pytest.mark.parametrize("field", ["rounds", "self_edit_every", "self_edit_budget"])
    def test_negative_ints_refused(self, field: str, tmp_path: Path) -> None:
        for bad in (-1, True):
            kwargs: dict[str, Any] = {field: bad}
            with pytest.raises(ContractViolationError, match=field):
                RsiConfig(slug="a", results_root=tmp_path, **kwargs)

    def test_zero_ints_allowed(self, tmp_path: Path) -> None:
        cfg = RsiConfig(slug="a", results_root=tmp_path, rounds=0, self_edit_every=0)
        assert cfg.rounds == 0

    def test_timeouts_positive(self, tmp_path: Path) -> None:
        with pytest.raises(ContractViolationError, match="round_timeout_seconds"):
            RsiConfig(slug="a", results_root=tmp_path, round_timeout_seconds=0)
        with pytest.raises(ContractViolationError, match="verifier_timeout_seconds"):
            RsiConfig(slug="a", results_root=tmp_path, verifier_timeout_seconds=-1)

    def test_noise_floor_non_negative(self, tmp_path: Path) -> None:
        with pytest.raises(ContractViolationError, match="noise_floor"):
            RsiConfig(slug="a", results_root=tmp_path, noise_floor=-0.1)
        assert RsiConfig(slug="a", results_root=tmp_path, noise_floor=0.0).noise_floor == 0.0

    def test_frozen(self, tmp_path: Path) -> None:
        cfg = RsiConfig(slug="a", results_root=tmp_path)
        with pytest.raises(AttributeError):
            cfg.slug = "b"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Verifier spec and lock
# --------------------------------------------------------------------------- #


class TestVerifierSpec:
    def test_empty_command_refused(self) -> None:
        with pytest.raises(ContractViolationError):
            VerifierSpec(command="   ")

    @pytest.mark.parametrize("rel", ["/abs/path", "../up", "a/../../b", ""])
    def test_unsafe_files_refused(self, rel: str) -> None:
        with pytest.raises(ContractViolationError):
            VerifierSpec(command="true", files=(rel,))

    def test_files_frozen_to_tuple(self) -> None:
        spec = VerifierSpec(command="true", files=["a.py"])  # type: ignore[arg-type]
        assert spec.files == ("a.py",)


class TestVerifierLock:
    def test_first_token_file_is_pinned(self, sandbox: Path) -> None:
        (sandbox / "verify.sh").write_text("#!/bin/sh\necho score=1\n")
        lock = compute_verifier_lock(VerifierSpec(command="./verify.sh --fast"), sandbox, now_ms=5)
        assert set(lock.file_sha256s) == {"verify.sh"}
        assert lock.command_sha256 == sha256_text("./verify.sh --fast")
        assert lock.created_at_ms == 5

    def test_non_file_first_token_not_pinned(self, sandbox: Path) -> None:
        lock = compute_verifier_lock(VerifierSpec(command="python3 -m pytest"), sandbox, now_ms=0)
        assert lock.file_sha256s == {}

    def test_explicit_files_pinned(self, sandbox: Path) -> None:
        (sandbox / "tests").mkdir()
        (sandbox / "tests" / "t.py").write_text("x = 1\n")
        lock = compute_verifier_lock(
            VerifierSpec(command="pytest tests", files=("tests/t.py",)), sandbox, now_ms=0
        )
        assert set(lock.file_sha256s) == {"tests/t.py"}

    def test_missing_explicit_file_refused(self, sandbox: Path) -> None:
        with pytest.raises(ContractViolationError, match="not a regular file"):
            compute_verifier_lock(VerifierSpec(command="true", files=("nope",)), sandbox, now_ms=0)

    def test_symlink_out_of_sandbox_refused(self, sandbox: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside.sh"
        outside.write_text("echo score=1\n")
        (sandbox / "link.sh").symlink_to(outside)
        with pytest.raises(ContractViolationError, match="outside the sandbox"):
            compute_verifier_lock(
                VerifierSpec(command="true", files=("link.sh",)), sandbox, now_ms=0
            )
        # As a first token it is silently not pinned rather than refused.
        lock = compute_verifier_lock(VerifierSpec(command="./link.sh"), sandbox, now_ms=0)
        assert lock.file_sha256s == {}

    def test_check_passes_when_intact(self, sandbox: Path) -> None:
        (sandbox / "v.sh").write_text("echo score=1\n")
        lock = compute_verifier_lock(VerifierSpec(command="./v.sh"), sandbox, now_ms=0)
        check_verifier_lock(lock, sandbox)
        check_verifier_lock(lock, sandbox, expected_command="./v.sh")

    def test_check_names_changed_file(self, sandbox: Path) -> None:
        (sandbox / "v.sh").write_text("echo score=1\n")
        lock = compute_verifier_lock(VerifierSpec(command="./v.sh"), sandbox, now_ms=0)
        (sandbox / "v.sh").write_text("echo score=999\n")
        with pytest.raises(FrozenVerifierError, match=r"'v\.sh' changed"):
            check_verifier_lock(lock, sandbox)

    def test_check_names_missing_file(self, sandbox: Path) -> None:
        (sandbox / "v.sh").write_text("echo score=1\n")
        lock = compute_verifier_lock(VerifierSpec(command="./v.sh"), sandbox, now_ms=0)
        (sandbox / "v.sh").unlink()
        with pytest.raises(FrozenVerifierError, match=r"'v\.sh' is missing"):
            check_verifier_lock(lock, sandbox)

    def test_check_refuses_different_expected_command(self, sandbox: Path) -> None:
        lock = compute_verifier_lock(VerifierSpec(command="true"), sandbox, now_ms=0)
        with pytest.raises(FrozenVerifierError, match="differs from the locked verifier"):
            check_verifier_lock(lock, sandbox, expected_command="false")

    def test_lock_refuses_inconsistent_command_hash(self) -> None:
        with pytest.raises(FrozenVerifierError, match="internally inconsistent"):
            VerifierLock(command="true", command_sha256="0" * 64, file_sha256s={}, created_at_ms=0)

    def test_edited_lock_file_command_is_caught(self, sandbox: Path) -> None:
        lock = compute_verifier_lock(VerifierSpec(command="true"), sandbox, now_ms=0)
        payload = lock.to_json()
        payload["command"] = "true || true"  # tamper with the command, keep the hash
        with pytest.raises(FrozenVerifierError):
            VerifierLock.from_json(payload)

    def test_round_trip(self, sandbox: Path) -> None:
        (sandbox / "v.sh").write_text("x")
        lock = compute_verifier_lock(VerifierSpec(command="./v.sh"), sandbox, now_ms=42)
        again = VerifierLock.from_json(json.loads(json.dumps(lock.to_json())))
        assert again == lock

    def test_malformed_lock_payload(self) -> None:
        with pytest.raises(FrozenVerifierError, match="malformed"):
            VerifierLock.from_json({"command": "true"})

    def test_iter_forbidden_covers_command_and_hash(self, sandbox: Path) -> None:
        lock = compute_verifier_lock(VerifierSpec(command="./secret --k"), sandbox, now_ms=0)
        assert set(iter_forbidden(lock)) == {"./secret --k", lock.command_sha256}


# --------------------------------------------------------------------------- #
# parse_score / VerifierOutcome
# --------------------------------------------------------------------------- #


class TestParseScore:
    def test_last_line_wins(self) -> None:
        assert parse_score("score=1\nnoise\nscore=2.5\n") == 2.5

    def test_whitespace_stripped(self) -> None:
        assert parse_score("  score=3 \n") == 3.0

    def test_spaces_around_equals_not_matched(self) -> None:
        assert parse_score("score = 1\n") is None

    def test_scientific_notation(self) -> None:
        assert parse_score("score=1.5e-3") == 1.5e-3
        assert parse_score("score=-2E+1") == -20.0

    def test_no_match(self) -> None:
        assert parse_score("") is None
        assert parse_score("done\n") is None
        assert parse_score("score=abc") is None
        assert parse_score("myscore=1") is None

    def test_non_finite_or_unparseable_skipped(self) -> None:
        assert parse_score("score=1\nscore=1e999") == 1.0
        assert parse_score("score=2\nscore=.") == 2.0
        assert parse_score("score=1e999") is None


class TestVerifierOutcome:
    def test_passed_is_exit_zero(self) -> None:
        VerifierOutcome(exit_code=0, score=None, passed=True, stdout_tail="", wall_seconds=0)
        with pytest.raises(ContractViolationError, match="contradicts"):
            VerifierOutcome(exit_code=1, score=None, passed=True, stdout_tail="", wall_seconds=0)
        with pytest.raises(ContractViolationError, match="contradicts"):
            VerifierOutcome(exit_code=0, score=1.0, passed=False, stdout_tail="", wall_seconds=0)

    def test_nan_score_refused(self) -> None:
        with pytest.raises(ContractViolationError):
            VerifierOutcome(
                exit_code=0, score=float("nan"), passed=True, stdout_tail="", wall_seconds=0
            )


class TestEngineResult:
    def test_negative_wall_refused(self) -> None:
        with pytest.raises(ContractViolationError):
            EngineResult(exit_code=0, stdout="", stderr="", wall_seconds=-1, timed_out=False)


# --------------------------------------------------------------------------- #
# RoundRecord / LoopEvent
# --------------------------------------------------------------------------- #


class TestRoundRecord:
    def test_bash_keys_first_and_categories_sorted(self) -> None:
        rec = RoundRecord(
            round=4,
            started=100,
            ended=160,
            exit=0,
            score=0.5,
            passed=True,
            categories=frozenset({FailureCategory.REGRESSED, FailureCategory.NO_PROGRESS}),
        )
        payload = rec.to_json()
        assert list(payload)[:4] == ["round", "started", "ended", "exit"]
        assert payload["categories"] == ["no_progress", "regressed"]
        assert payload["score"] == 0.5
        assert payload["agent_reported_score"] is None

    def test_round_trip(self) -> None:
        rec = RoundRecord(
            round=2,
            started=1,
            ended=2,
            exit=0,
            score=1.25,
            passed=True,
            categories=frozenset({FailureCategory.CHEAT_DETECTED}),
            scaffold_sha="abc123",
            void=True,
            agent_reported_score=9.0,
            verifier_wall_seconds=3.5,
        )
        assert RoundRecord.from_json(json.loads(rec.to_json_line())) == rec

    def test_reads_bash_script_line(self) -> None:
        rec = RoundRecord.from_json(bash_round(7, exit_code=1))
        assert (rec.round, rec.exit, rec.score, rec.passed) == (7, 1, None, False)
        assert rec.categories == frozenset()
        assert rec.void is False

    def test_event_line_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="event"):
            RoundRecord.from_json({"event": "rollback", "round": 1, "ts": 0})

    def test_malformed(self) -> None:
        with pytest.raises(ContractViolationError, match="malformed"):
            RoundRecord.from_json({"round": 1})
        with pytest.raises(ContractViolationError, match="malformed"):
            RoundRecord.from_json({**bash_round(1), "categories": ["bogus"]})

    def test_void_needs_category(self) -> None:
        with pytest.raises(ContractViolationError, match="void"):
            RoundRecord(round=1, started=0, ended=0, exit=0, void=True)

    def test_round_and_time_ordering(self) -> None:
        with pytest.raises(ContractViolationError):
            RoundRecord(round=0, started=0, ended=0, exit=0)
        with pytest.raises(ContractViolationError):
            RoundRecord(round=1, started=5, ended=4, exit=0)

    def test_improved_sentinel(self) -> None:
        assert RoundRecord(round=1, started=0, ended=0, exit=0, passed=True).improved
        assert not RoundRecord(
            round=1,
            started=0,
            ended=0,
            exit=0,
            passed=True,
            categories=frozenset({FailureCategory.NO_METRICS}),
        ).improved

    def test_measured_and_reported_scores_are_distinct(self) -> None:
        rec = RoundRecord(round=1, started=0, ended=0, exit=0, score=1.0, agent_reported_score=99.0)
        assert rec.score == 1.0
        assert rec.to_json()["score"] == 1.0


class TestLoopEvent:
    def test_to_json_flattens_details(self) -> None:
        ev = LoopEvent(event="rollback", round=6, ts=123, details={"reverted": "deadbeef"})
        assert ev.to_json() == {"event": "rollback", "round": 6, "ts": 123, "reverted": "deadbeef"}
        assert LoopEvent.from_json(json.loads(ev.to_json_line())) == ev

    def test_details_may_not_shadow_fixed_keys(self) -> None:
        with pytest.raises(ContractViolationError, match="shadow"):
            LoopEvent(event="x", round=1, ts=0, details={"round": 2})

    def test_requires_event_name(self) -> None:
        with pytest.raises(ContractViolationError):
            LoopEvent(event="", round=1, ts=0)
        with pytest.raises(ContractViolationError):
            LoopEvent.from_json({"round": 1, "ts": 0})


# --------------------------------------------------------------------------- #
# Self-edit inputs
# --------------------------------------------------------------------------- #


def _inputs(**overrides: object) -> SelfEditInputs:
    kwargs: dict[str, object] = {
        "round_index": 3,
        "best_score": 1.0,
        "rounds": (
            RoundSummary(
                round=1, score=1.0, passed=True, categories=("no_metrics",), wall_seconds=10.0
            ),
        ),
        "taxonomy_counts": {"no_metrics": 1},
        "scaffold_text": "# Scaffold\nBe careful.\n",
        "notes_tail": "round 1: tried X\n",
        "forbidden": (),
    }
    kwargs.update(overrides)
    return SelfEditInputs(**kwargs)  # type: ignore[arg-type]


class TestSelfEditInputs:
    def test_builds_and_freezes(self) -> None:
        inputs = _inputs()
        assert isinstance(inputs.rounds, tuple)
        with pytest.raises(TypeError):
            inputs.taxonomy_counts["x"] = 1  # type: ignore[index]

    def test_refuses_lock_marker_in_scaffold_or_notes(self) -> None:
        with pytest.raises(ContractViolationError, match="scaffold_text"):
            _inputs(scaffold_text="cat VERIFIER.json")
        with pytest.raises(ContractViolationError, match="notes_tail"):
            _inputs(notes_tail="see VERIFIER.json")

    def test_refuses_forbidden_substrings_without_echoing_them(self) -> None:
        with pytest.raises(ContractViolationError) as excinfo:
            _inputs(
                notes_tail="ran ./verify.sh --strict and got 3", forbidden=("./verify.sh --strict",)
            )
        assert "./verify.sh" not in str(excinfo.value)
        assert "verifier internals" in str(excinfo.value)

    def test_empty_forbidden_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="non-empty"):
            _inputs(forbidden=("",))

    def test_round_summary_from_record(self) -> None:
        rec = RoundRecord(
            round=2,
            started=10,
            ended=25,
            exit=0,
            score=0.7,
            passed=True,
            categories=frozenset({FailureCategory.REGRESSED, FailureCategory.NO_PROGRESS}),
        )
        summary = RoundSummary.from_record(rec)
        assert summary == RoundSummary(
            round=2,
            score=0.7,
            passed=True,
            categories=("no_progress", "regressed"),
            wall_seconds=15.0,
        )

    def test_rounds_must_be_summaries(self) -> None:
        with pytest.raises(ContractViolationError):
            _inputs(rounds=({"round": 1},))


# --------------------------------------------------------------------------- #
# CheatVerdict
# --------------------------------------------------------------------------- #


class TestCheatVerdict:
    def test_quiet_verdict(self) -> None:
        v = CheatVerdict(fired=False)
        assert v.categories == frozenset()

    def test_fired_needs_categories_and_reason(self) -> None:
        with pytest.raises(ContractViolationError, match="fired"):
            CheatVerdict(fired=True)
        with pytest.raises(ContractViolationError, match="fired"):
            CheatVerdict(fired=False, categories=frozenset({FailureCategory.CHEAT_DETECTED}))
        with pytest.raises(ContractViolationError, match="reason"):
            CheatVerdict(fired=True, categories=frozenset({FailureCategory.SANDBOX_ESCAPE}))

    def test_only_detector_categories(self) -> None:
        with pytest.raises(ContractViolationError, match="detector categories"):
            CheatVerdict(
                fired=True, categories=frozenset({FailureCategory.TIMEOUT}), reasons=("x",)
            )
        v = CheatVerdict(
            fired=True,
            categories=frozenset(
                {FailureCategory.VERIFIER_TAMPERED, FailureCategory.SANDBOX_ESCAPE}
            ),
            reasons=("lock", "symlink"),
        )
        assert v.fired
