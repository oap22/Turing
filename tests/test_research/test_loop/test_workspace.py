"""Workspace materialisation and the loop's env-driven settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.loop.settings import ResearchLoopSettings
from turing.research.loop.workspace import CopyTreeWorkspaceProvider
from turing.research.problems.catalog import speedup_specs
from turing.research.problems.speedup import SpeedupAdapter

from .conftest import make_problem

if TYPE_CHECKING:
    from pathlib import Path


class TestCopyTreeWorkspaceProvider:
    async def test_it_copies_the_template(self, tmp_path: Path) -> None:
        template = tmp_path / "template"
        (template / "src").mkdir(parents=True)
        (template / "src" / "slow.py").write_text("# slow\n")
        provider = CopyTreeWorkspaceProvider(tmp_path / "workspaces")

        workspace = await provider.materialise(
            make_problem("s1", template=template), attempt_id="a1"
        )
        assert (workspace / "src" / "slow.py").read_text() == "# slow\n"

    async def test_each_attempt_starts_clean(self, tmp_path: Path) -> None:
        """Otherwise a seed is not the only thing that changed between runs."""
        template = tmp_path / "template"
        template.mkdir()
        (template / "a.txt").write_text("original")
        provider = CopyTreeWorkspaceProvider(tmp_path / "workspaces")
        problem = make_problem("s1", template=template)

        first = await provider.materialise(problem, attempt_id="a1")
        (first / "junk.txt").write_text("left over")
        second = await provider.materialise(problem, attempt_id="a1")
        assert not (second / "junk.txt").exists()
        assert (second / "a.txt").read_text() == "original"

    async def test_it_refuses_to_reuse_a_directory_when_told_not_to_clean(
        self, tmp_path: Path
    ) -> None:
        template = tmp_path / "template"
        template.mkdir()
        provider = CopyTreeWorkspaceProvider(tmp_path / "workspaces", clean_existing=False)
        problem = make_problem("s1", template=template)
        await provider.materialise(problem, attempt_id="a1")
        with pytest.raises(ContractViolationError, match="fresh workspace"):
            await provider.materialise(problem, attempt_id="a1")

    async def test_a_missing_template_still_yields_an_empty_workspace(self, tmp_path: Path) -> None:
        provider = CopyTreeWorkspaceProvider(tmp_path / "workspaces")
        workspace = await provider.materialise(
            make_problem("s1", template=tmp_path / "nope"), attempt_id="a1"
        )
        assert workspace.is_dir()
        assert list(workspace.iterdir()) == []

    async def test_it_does_not_copy_the_eval_set_into_an_attempt(self, tmp_path: Path) -> None:
        """RoundRunner.materialise uses this provider, not SpeedupAdapter.materialise_workspace.

        Confirmed 2026-08-12: the adapter excluded research/docs/test_research and
        production still copied them, because this copytree ignored the spec.
        """
        source = _source_with_eval_set(tmp_path / "source")
        spec = speedup_specs(turing_repo=source)[0]
        problem = SpeedupAdapter(
            harness_root=tmp_path / "harness",
            reference_root=tmp_path / "reference",
            specs=(spec,),
        ).load()[0]
        workspace = await CopyTreeWorkspaceProvider(tmp_path / "workspaces").materialise(
            problem, attempt_id="a1"
        )
        _assert_eval_set_absent(workspace)


def _source_with_eval_set(root: Path) -> Path:
    (root / "src" / "turing" / "research" / "problems").mkdir(parents=True)
    (root / "src" / "turing" / "research" / "problems" / "catalog.py").write_text(
        "DEFAULT_SPLITS = {'held-out': (3, 6)}\n"
    )
    (root / "src" / "turing" / "vault").mkdir(parents=True)
    (root / "src" / "turing" / "vault" / "index.py").write_text("query = 1\n")
    (root / "research" / "briefs").mkdir(parents=True)
    (root / "research" / "briefs" / "brief.md").write_text("held out: 3 and 6\n")
    (root / "tests" / "test_research").mkdir(parents=True)
    (root / "tests" / "test_research" / "test_catalog.py").write_text("floor = 52\n")
    (root / "tests" / "test_evals").mkdir(parents=True)
    (root / "tests" / "test_evals" / "test_ok.py").write_text("ok\n")
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "adr" / "0011.md").write_text("loopholes\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref\n")
    return root


def _assert_eval_set_absent(workspace: Path) -> None:
    assert (workspace / "src" / "turing" / "vault" / "index.py").is_file()
    assert (workspace / "tests" / "test_evals" / "test_ok.py").is_file()
    assert not (workspace / "src" / "turing" / "research").exists()
    assert not (workspace / "research").exists()
    assert not (workspace / "tests" / "test_research").exists()
    assert not (workspace / "docs").exists()
    assert not (workspace / ".git").exists()


class TestSettings:
    def test_defaults_do_not_require_a_dotenv(self) -> None:
        settings = ResearchLoopSettings(_env_file=None)
        assert settings.research_results_root.as_posix().endswith("research/results")
        assert settings.operator_ntfy_topic is None
        assert settings.research_escalation_repush_seconds == pytest.approx(1800.0)

    def test_env_vars_use_the_repo_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TURING_OPERATOR_NTFY_TOPIC", "turing-research")
        monkeypatch.setenv("TURING_RESEARCH_ESCALATION_POLL_SECONDS", "0.5")
        settings = ResearchLoopSettings(_env_file=None)
        assert settings.operator_ntfy_topic == "turing-research"
        assert settings.research_escalation_poll_seconds == pytest.approx(0.5)

    def test_a_non_positive_poll_interval_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TURING_RESEARCH_ESCALATION_POLL_SECONDS", "0")
        with pytest.raises(ValueError, match="greater than 0"):
            ResearchLoopSettings(_env_file=None)
