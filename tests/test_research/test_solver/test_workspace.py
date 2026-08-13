"""Workspace isolation: fresh directories, and writes that stay inside them."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import Problem, ProblemType, Split
from turing.research.problems.catalog import speedup_specs
from turing.research.problems.speedup import SpeedupAdapter
from turing.research.solver import (
    FileEdit,
    Proposal,
    WorkspaceEscapeError,
    WorkspaceManager,
    WorkspaceTemplateError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import NumberVerifier


class TestPreparation:
    async def test_prepare_copies_the_template_into_a_fresh_directory(
        self, manager: WorkspaceManager, problem: Problem
    ) -> None:
        target = manager.path_for(problem.id, "a1")
        workspace = await manager.prepare(problem, target)

        assert workspace.path == target
        assert (target / "README.md").read_text(encoding="utf-8") == "baseline\n"
        assert (target / "src" / "baseline.py").exists()

    async def test_the_template_itself_is_never_touched(
        self, manager: WorkspaceManager, problem: Problem, template_dir: Path
    ) -> None:
        workspace = await manager.prepare(problem, manager.path_for(problem.id, "a1"))
        await workspace.write_text("README.md", "modified\n")

        assert (template_dir / "README.md").read_text(encoding="utf-8") == "baseline\n"

    async def test_two_attempts_get_independent_directories(
        self, manager: WorkspaceManager, problem: Problem
    ) -> None:
        first = await manager.prepare(problem, manager.path_for(problem.id, "a1"))
        second = await manager.prepare(problem, manager.path_for(problem.id, "a2"))
        await first.write_text("solution.txt", "1.0")

        assert first.path != second.path
        assert not (second.path / "solution.txt").exists()

    async def test_prepare_reattaches_instead_of_reseeding_an_existing_workspace(
        self, manager: WorkspaceManager, problem: Problem
    ) -> None:
        """The resume path. Re-copying would discard the work done so far.

        This is the difference between "an interruption costs the remainder of
        an attempt" and "an interruption costs the attempt".
        """
        target = manager.path_for(problem.id, "a1")
        workspace = await manager.prepare(problem, target)
        await workspace.write_text("solution.txt", "4.0")
        await workspace.write_text("README.md", "edited by the solver\n")

        again = await manager.prepare(problem, target)

        assert again.path == target
        assert (target / "solution.txt").read_text(encoding="utf-8") == "4.0"
        assert (target / "README.md").read_text(encoding="utf-8") == "edited by the solver\n"

    async def test_a_missing_template_is_a_harness_failure(
        self, manager: WorkspaceManager, verifier: NumberVerifier, tmp_path: Path
    ) -> None:
        problem = Problem(
            id="speedup-1",
            problem_type=ProblemType.SPEEDUP,
            goal="goal",
            workspace_template=tmp_path / "does-not-exist",
            verifier=verifier,
            split=Split.PRACTICE,
        )
        with pytest.raises(WorkspaceTemplateError):
            await manager.prepare(problem, manager.path_for(problem.id, "a1"))

    async def test_a_workspace_outside_the_root_is_refused(
        self, manager: WorkspaceManager, problem: Problem, tmp_path: Path
    ) -> None:
        """The root is what the separate ``turing`` OS user owns.

        A workspace outside it would pass every in-process check while sitting
        outside the only boundary the OS actually enforces.
        """
        with pytest.raises(WorkspaceEscapeError):
            await manager.prepare(problem, tmp_path / "somewhere-else")

    async def test_prepare_does_not_copy_the_eval_set_into_an_attempt(
        self, tmp_path: Path, workspace_root: Path
    ) -> None:
        """Solver._prepare uses WorkspaceManager, not SpeedupAdapter.materialise_workspace."""
        source = _source_with_eval_set(tmp_path / "source")
        spec = speedup_specs(turing_repo=source)[0]
        problem = SpeedupAdapter(
            harness_root=tmp_path / "harness",
            reference_root=tmp_path / "reference",
            specs=(spec,),
        ).load()[0]
        manager = WorkspaceManager(workspace_root)
        workspace = await manager.prepare(problem, manager.path_for(problem.id, "a1"))
        _assert_eval_set_absent(workspace.path)


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


class TestContainment:
    @pytest.fixture
    async def workspace(self, manager: WorkspaceManager, problem: Problem):  # type: ignore[no-untyped-def]
        return await manager.prepare(problem, manager.path_for(problem.id, "a1"))

    @pytest.mark.parametrize(
        "relative",
        [
            "../escape.txt",
            "src/../../escape.txt",
            "/etc/passwd",
            "~/escape.txt",
            "",
            "   ",
        ],
    )
    async def test_paths_that_leave_the_workspace_are_refused(
        self, workspace, relative: str
    ) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(WorkspaceEscapeError):
            workspace.resolve(relative)

    async def test_a_symlink_out_of_the_tree_is_not_a_tunnel(
        self, workspace, tmp_path: Path
    ) -> None:  # type: ignore[no-untyped-def]
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, workspace.path / "link")

        with pytest.raises(WorkspaceEscapeError):
            workspace.resolve("link/pwned.txt")
        assert not (outside / "pwned.txt").exists()

    async def test_nested_paths_inside_the_workspace_are_allowed(self, workspace) -> None:  # type: ignore[no-untyped-def]
        written = await workspace.write_text("a/b/c.txt", "hello")

        assert written.read_text(encoding="utf-8") == "hello"
        assert workspace.real_path in written.parents


class TestApply:
    @pytest.fixture
    async def workspace(self, manager: WorkspaceManager, problem: Problem):  # type: ignore[no-untyped-def]
        return await manager.prepare(problem, manager.path_for(problem.id, "a1"))

    async def test_apply_writes_every_edit_and_reports_the_paths(self, workspace) -> None:  # type: ignore[no-untyped-def]
        proposal = Proposal(
            proposal_id="p0",
            edits=(
                FileEdit(relative_path="solution.txt", content="2.5"),
                FileEdit(relative_path="src/fast.py", content="x = 1\n"),
            ),
        )
        applied = await workspace.apply(proposal)

        assert applied == ("solution.txt", "src/fast.py")
        assert (workspace.path / "solution.txt").read_text(encoding="utf-8") == "2.5"

    async def test_applying_the_same_proposal_twice_is_a_no_op(self, workspace) -> None:  # type: ignore[no-untyped-def]
        """Idempotence is what makes resume safe rather than merely careful.

        A crash between two files in one proposal is repaired by applying the
        whole thing again, so the journal never has to record partial progress
        inside a single apply.
        """
        proposal = Proposal(
            proposal_id="p0",
            edits=(FileEdit(relative_path="solution.txt", content="2.5"),),
        )
        await workspace.apply(proposal)
        first = (workspace.path / "solution.txt").read_bytes()
        await workspace.apply(proposal)

        assert (workspace.path / "solution.txt").read_bytes() == first

    async def test_an_escaping_edit_stops_the_whole_apply(self, workspace, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        proposal = Proposal(
            proposal_id="p0",
            edits=(FileEdit(relative_path="../pwned.txt", content="x"),),
        )
        with pytest.raises(WorkspaceEscapeError):
            await workspace.apply(proposal)
        assert not (workspace.path.parent / "pwned.txt").exists()

    async def test_no_temporary_files_survive_a_write(self, workspace) -> None:  # type: ignore[no-untyped-def]
        await workspace.write_text("solution.txt", "1.0")

        leftovers = [p.name for p in workspace.path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []
