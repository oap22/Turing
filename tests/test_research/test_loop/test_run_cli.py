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
