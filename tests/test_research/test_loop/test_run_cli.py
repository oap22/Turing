"""``python -m turing.research.loop.run`` — the driver's refusals.

The driver's happy path needs a real Claude subscription, the frozen baselines,
and hours of compute, so what is tested here is the part that must hold without
any of that: it refuses, at zero compute, for a reason that names the missing
piece. Every one of these is a refusal the operator will meet before their
first real round.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.loop import run as run_cli
from turing.research.loop.metrics import MIN_NOISE_FLOOR_SEEDS

if TYPE_CHECKING:
    from pathlib import Path


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--loop-slug",
        "test-loop",
        "--harness-root",
        str(tmp_path / "harness"),
        "--reference-root",
        str(tmp_path / "reference"),
        "--scaffold-repo",
        str(tmp_path / "scaffold"),
        *extra,
    ]


class TestTheDriverRefusesWhatIsNotWired:
    def test_a_round_above_zero_names_both_missing_pieces(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli.main(_args(tmp_path, "--round", "1"))
        assert code == run_cli.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "round-NN/round.json" in err
        assert "self-edit" in err
        assert "Drive --round 0." in err

    def test_missing_harness_scripts_refuse_before_any_compute(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The catalog declares its benchmark drivers; they are not written yet.

        This is the refusal a real operator hits first, and it must land
        before a workspace is copied or a model is called — which is why the
        corpus is built before the backend.
        """
        code = run_cli.main(_args(tmp_path))
        assert code == run_cli.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "declared harness script(s) do not exist" in err
        assert "at zero compute" in err

    def test_a_scaffold_repo_that_is_not_a_checkout_is_refused(self, tmp_path: Path) -> None:
        """``scaffold_git_sha`` is lineage, so it is read, never assumed."""
        with pytest.raises(ContractViolationError, match="is not a git checkout"):
            run_cli._scaffold_sha(tmp_path / "not-a-repo")


class TestTheParser:
    def test_resume_is_the_default(self, tmp_path: Path) -> None:
        assert run_cli.build_parser().parse_args(_args(tmp_path)).resume is True

    def test_no_resume_turns_it_off(self, tmp_path: Path) -> None:
        parsed = run_cli.build_parser().parse_args(_args(tmp_path, "--no-resume"))
        assert parsed.resume is False

    def test_the_default_seed_set_can_measure_a_floor(self, tmp_path: Path) -> None:
        parsed = run_cli.build_parser().parse_args(_args(tmp_path))
        assert len(parsed.seeds) >= MIN_NOISE_FLOOR_SEEDS
        assert len(set(parsed.seeds)) == len(parsed.seeds)


# --------------------------------------------------------------------------- #
# --dry-run and --yes, reached through main() with the expensive edges faked
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    specs: tuple = ()  # type: ignore[type-arg]

    def missing_harness_scripts(self) -> tuple:  # type: ignore[type-arg]
        return ()

    def eval_set_material(self) -> tuple[str, ...]:
        return ("fake",)


class _FakeBackend:
    constructed = 0
    closed = 0

    @classmethod
    def from_settings(cls, settings: object, **kwargs: object) -> _FakeBackend:
        _FakeBackend.constructed += 1
        return cls()

    async def aclose(self) -> None:
        _FakeBackend.closed += 1


@pytest.fixture
def faked_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Everything past argument parsing except the corpus, the sha and the SDK.

    Returns the results root, which must not exist before the call and — for
    a dry run or a refused run — after it either.
    """
    results_root = tmp_path / "results"
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / ".git").mkdir()
    monkeypatch.setenv("TURING_RESEARCH_RESULTS_ROOT", str(results_root))
    monkeypatch.setenv("TURING_RESEARCH_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(run_cli, "_load_corpus", lambda h, r: (_FakeAdapter(), ("fake-problem",)))
    monkeypatch.setattr(run_cli, "fingerprint_corpus", lambda corpus, extra=(): "f" * 64)
    monkeypatch.setattr(run_cli, "_scaffold_sha", lambda repo: "d" * 40)
    monkeypatch.setattr(
        "turing.research.backends.claude.ClaudeBackend.from_settings",
        _FakeBackend.from_settings,
    )
    _FakeBackend.constructed = 0
    _FakeBackend.closed = 0
    return results_root


class TestDryRunAndYes:
    def test_a_dry_run_exits_zero_and_creates_nothing_under_the_results_root(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert not faked_driver.exists()
        code = run_cli.main(_args(tmp_path, "--dry-run"))
        out = capsys.readouterr()
        assert code == run_cli.EXIT_OK, out.err
        assert not faked_driver.exists(), sorted(faked_driver.rglob("*"))
        assert "dry run" in out.out
        assert "nothing was written under the results root" in out.out
        # The plan names what the real run would use.
        assert "seeds     [1, 2, 3]" in out.out
        assert "cap       " in out.out
        # The backend client is built (a missing credential refuses here) and closed.
        assert _FakeBackend.constructed == 1
        assert _FakeBackend.closed == 1

    def test_a_dry_run_validates_the_seed_set_like_a_real_run(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--seeds 1 --dry-run`` used to exit 0 while ``--seeds 1`` refused."""
        code = run_cli.main(_args(tmp_path, "--seeds", "1", "--dry-run"))
        err = capsys.readouterr().err
        assert code == run_cli.EXIT_REFUSED
        assert f"at least {MIN_NOISE_FLOOR_SEEDS} seeds" in err
        assert not faked_driver.exists()

    def test_a_dry_run_validates_distinct_seeds_too(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli.main(_args(tmp_path, "--seeds", "1", "1", "2", "--dry-run"))
        assert code == run_cli.EXIT_REFUSED
        assert "seeds must be distinct" in capsys.readouterr().err

    def test_a_real_run_without_yes_refuses_after_preflight_and_writes_nothing(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli.main(_args(tmp_path))
        out = capsys.readouterr()
        assert code == run_cli.EXIT_REFUSED
        assert "--yes" in out.err
        assert "spends subscription compute" in out.err
        # One line, as promised.
        assert len([line for line in out.err.splitlines() if line.strip()]) == 1
        # Preflight ran (the plan printed) but the results root was not touched.
        assert "loop      " in out.out
        assert not faked_driver.exists()
        assert _FakeBackend.closed == 1

    def test_yes_is_not_needed_for_a_dry_run(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run_cli.main(_args(tmp_path, "--dry-run")) == run_cli.EXIT_OK
        assert "--yes" not in capsys.readouterr().err

    def test_the_parser_accepts_yes(self, tmp_path: Path) -> None:
        parsed = run_cli.build_parser().parse_args(_args(tmp_path, "--yes"))
        assert parsed.yes is True
        assert run_cli.build_parser().parse_args(_args(tmp_path)).yes is False

    def test_help_says_a_run_must_acknowledge_spending(self) -> None:
        text = run_cli.build_parser().format_help()
        assert "--yes" in text
        assert "subscription compute" in text
