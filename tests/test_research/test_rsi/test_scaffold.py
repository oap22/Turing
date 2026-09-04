"""The scaffold self-edit step: inputs are narrow, only SCAFFOLD.md may change, rollback works.

Every test that touches git uses a real repository under ``tmp_path``; the
engine is a local fake that runs a callable inside the sandbox.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import ContractViolationError, FrozenVerifierError
from turing.research.rsi.contracts import (
    EngineResult,
    RoundRecord,
    SelfEditInputs,
    VerifierSpec,
    compute_verifier_lock,
)
from turing.research.rsi.scaffold import (
    DEFAULT_SCAFFOLD_TEXT,
    SCAFFOLD_FILENAME,
    ScaffoldSelfEditStep,
    build_self_edit_inputs,
    read_or_create_scaffold,
    render_self_edit_prompt,
    rollback_scaffold,
    scaffold_head,
)
from turing.research.rsi.taxonomy import FailureCategory

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.test_research.test_rsi.conftest import RsiDirs

VERIFIER_CMD = "python verify_secret.py --strict"
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            *args,
        ],
        cwd=cwd,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


class CallableEngine:
    """Runs ``action(cwd)`` in place of ``claude`` and returns a fixed result."""

    def __init__(
        self,
        action: Callable[[Path], None] | None = None,
        *,
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> None:
        self.action = action
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.prompts: list[str] = []
        self.cwds: list[Path] = []

    async def run(self, prompt: str, *, cwd: Path, timeout_seconds: float) -> EngineResult:
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        if self.action is not None:
            self.action(cwd)
        return EngineResult(
            exit_code=self.exit_code,
            stdout="",
            stderr="",
            wall_seconds=0.1,
            timed_out=self.timed_out,
        )


@pytest.fixture
def repo(rsi_dirs: RsiDirs) -> Path:
    """The sandbox as a git repo with an initial commit holding NOTES.md and SCAFFOLD.md."""
    sandbox = rsi_dirs.sandbox
    git(sandbox, "init", "-q")
    (sandbox / "NOTES.md").write_text("\n".join(f"line {i}" for i in range(60)) + "\n")
    (sandbox / SCAFFOLD_FILENAME).write_text("# Scaffold\n\nOriginal instructions.\n")
    (sandbox / "metrics.jsonl").write_text('{"step": 1}\n')
    git(sandbox, "add", "-A")
    git(sandbox, "commit", "-q", "-m", "init")
    return sandbox


def records() -> list[RoundRecord]:
    t = 1_700_000_000
    return [
        RoundRecord(round=1, started=t, ended=t + 30, exit=0, score=0.5, passed=True),
        RoundRecord(
            round=2,
            started=t + 100,
            ended=t + 160,
            exit=0,
            score=0.4,
            passed=True,
            categories=frozenset({FailureCategory.NO_PROGRESS, FailureCategory.REGRESSED}),
        ),
        RoundRecord(
            round=3,
            started=t + 200,
            ended=t + 210,
            exit=1,
            categories=frozenset({FailureCategory.ENGINE_ERROR, FailureCategory.NO_METRICS}),
        ),
        RoundRecord(
            round=4,
            started=t + 300,
            ended=t + 310,
            exit=0,
            score=9.0,
            passed=True,
            void=True,
            categories=frozenset({FailureCategory.CHEAT_DETECTED}),
        ),
    ]


# --------------------------------------------------------------------------- #
# build_self_edit_inputs
# --------------------------------------------------------------------------- #


class TestBuildInputs:
    def test_aggregates_rounds_counts_and_best_score(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        inputs = build_self_edit_inputs(rsi_dirs.config, records(), repo, forbidden=[VERIFIER_CMD])
        assert inputs.round_index == 4
        assert [r.round for r in inputs.rounds] == [1, 2, 3, 4]
        assert inputs.rounds[1].categories == ("no_progress", "regressed")
        assert inputs.rounds[1].wall_seconds == 60.0
        # the void round's 9.0 never counts as best (I2/I5)
        assert inputs.best_score == 0.5
        assert inputs.taxonomy_counts["engine_error"] == 1
        assert inputs.taxonomy_counts["cheat_detected"] == 1
        assert inputs.taxonomy_counts["timeout"] == 0
        assert inputs.scaffold_text == "# Scaffold\n\nOriginal instructions.\n"
        assert inputs.notes_tail.splitlines()[0] == "line 20"
        assert len(inputs.notes_tail.splitlines()) == 40
        assert VERIFIER_CMD in inputs.forbidden

    def test_creates_default_scaffold_when_missing(self, rsi_dirs: RsiDirs) -> None:
        sandbox = rsi_dirs.sandbox
        assert not (sandbox / SCAFFOLD_FILENAME).exists()
        inputs = build_self_edit_inputs(rsi_dirs.config, [], sandbox, forbidden=[VERIFIER_CMD])
        assert (sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD_TEXT
        assert inputs.scaffold_text == DEFAULT_SCAFFOLD_TEXT
        assert inputs.notes_tail == ""
        assert inputs.round_index == 0
        assert inputs.best_score is None

    def test_lock_file_text_is_forbidden_by_construction(self, rsi_dirs: RsiDirs) -> None:
        sandbox = rsi_dirs.sandbox
        (sandbox / "verify_secret.py").write_text("print('score=1')\n")
        lock = compute_verifier_lock(VerifierSpec(VERIFIER_CMD), sandbox, now_ms=1)
        (sandbox / "VERIFIER.json").write_text(json.dumps(lock.to_json()))
        # caller passes nothing: the lock alone must make the command forbidden
        inputs = build_self_edit_inputs(rsi_dirs.config, [], sandbox)
        assert VERIFIER_CMD in inputs.forbidden
        assert lock.command_sha256 in inputs.forbidden

        (sandbox / "NOTES.md").write_text(f"the grader is {VERIFIER_CMD}\n")
        with pytest.raises(ContractViolationError) as excinfo:
            build_self_edit_inputs(rsi_dirs.config, [], sandbox)
        assert VERIFIER_CMD not in str(excinfo.value)

    def test_refuses_scaffold_carrying_forbidden_substring(self, rsi_dirs: RsiDirs) -> None:
        sandbox = rsi_dirs.sandbox
        (sandbox / SCAFFOLD_FILENAME).write_text(f"always run {VERIFIER_CMD} first\n")
        with pytest.raises(ContractViolationError):
            build_self_edit_inputs(rsi_dirs.config, [], sandbox, forbidden=[VERIFIER_CMD])

    def test_refuses_foreign_sandbox(self, rsi_dirs: RsiDirs, tmp_path: Path) -> None:
        other = tmp_path / "elsewhere"
        other.mkdir()
        with pytest.raises(ContractViolationError):
            build_self_edit_inputs(rsi_dirs.config, [], other, forbidden=[VERIFIER_CMD])


# --------------------------------------------------------------------------- #
# render_self_edit_prompt
# --------------------------------------------------------------------------- #


class TestPrompt:
    def test_prompt_states_rules_and_never_contains_verifier(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        inputs = build_self_edit_inputs(rsi_dirs.config, records(), repo, forbidden=[VERIFIER_CMD])
        prompt = render_self_edit_prompt(inputs)
        assert VERIFIER_CMD not in prompt
        assert "verify_secret" not in prompt
        assert "VERIFIER.json" not in prompt
        assert f"You may edit ONLY {SCAFFOLD_FILENAME}" in prompt
        assert "Do NOT commit" in prompt
        assert "Original instructions." in prompt
        assert "line 59" in prompt
        assert "| 2 | 0.4 | yes | no_progress, regressed | 60 |" in prompt
        assert "Best score: 0.5" in prompt
        assert "- engine_error: 1" in prompt

    def test_render_rechecks_forbidden(self) -> None:
        # a hand-built inputs whose forbidden list matches prompt boilerplate
        inputs = SelfEditInputs(
            round_index=1,
            best_score=None,
            rounds=(),
            taxonomy_counts={},
            scaffold_text="x",
            notes_tail="",
            forbidden=("Do NOT commit",),
        )
        with pytest.raises(ContractViolationError):
            render_self_edit_prompt(inputs)


# --------------------------------------------------------------------------- #
# ScaffoldSelfEditStep
# --------------------------------------------------------------------------- #


def _inputs(rsi_dirs: RsiDirs, repo: Path) -> SelfEditInputs:
    return build_self_edit_inputs(rsi_dirs.config, records(), repo, forbidden=[VERIFIER_CMD])


class TestSelfEditStep:
    async def test_scaffold_only_edit_is_committed(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        head_before = scaffold_head(repo)

        def edit(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).write_text("# Scaffold\n\nTry smaller batches.\n")

        engine = CallableEngine(edit)
        step = ScaffoldSelfEditStep(engine, rsi_dirs.config, repo, timeout_seconds=5)
        sha = await step.propose(_inputs(rsi_dirs, repo))

        assert sha is not None and sha != head_before
        assert sha == scaffold_head(repo)
        assert git(repo, "status", "--porcelain") == ""
        assert git(repo, "log", "-1", "--format=%s") == "rsi: self-edit after round 4"
        assert git(repo, "show", "--name-only", "--format=", "HEAD") == SCAFFOLD_FILENAME
        assert engine.cwds == [repo]
        assert VERIFIER_CMD not in engine.prompts[0]

    async def test_no_change_returns_none(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        head = scaffold_head(repo)
        step = ScaffoldSelfEditStep(CallableEngine(None), rsi_dirs.config, repo, timeout_seconds=5)
        assert await step.propose(_inputs(rsi_dirs, repo)) is None
        assert scaffold_head(repo) == head

    async def test_extra_file_rejects_edit_and_cleans_tree(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        head = scaffold_head(repo)

        def edit(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).write_text("# Scaffold\n\nsneaky\n")
            (cwd / "NOTES.md").write_text("rewritten notes\n")
            (cwd / "helper.py").write_text("print('hi')\n")
            (cwd / "pkg").mkdir()
            (cwd / "pkg" / "mod.py").write_text("x = 1\n")

        step = ScaffoldSelfEditStep(CallableEngine(edit), rsi_dirs.config, repo, timeout_seconds=5)
        assert await step.propose(_inputs(rsi_dirs, repo)) is None

        assert scaffold_head(repo) == head
        assert git(repo, "status", "--porcelain") == ""
        assert (repo / SCAFFOLD_FILENAME).read_text() == "# Scaffold\n\nOriginal instructions.\n"
        assert (repo / "NOTES.md").read_text().startswith("line 0\n")
        assert not (repo / "helper.py").exists()
        assert not (repo / "pkg").exists()

    async def test_rejection_never_discards_metrics_or_results(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        results_png = rsi_dirs.results / "plot.png"

        def edit(cwd: Path) -> None:
            (cwd / "metrics.jsonl").write_text('{"step": 1}\n{"step": 99}\n')
            (cwd / "extra.txt").write_text("x\n")
            results_png.write_bytes(b"png")

        step = ScaffoldSelfEditStep(CallableEngine(edit), rsi_dirs.config, repo, timeout_seconds=5)
        assert await step.propose(_inputs(rsi_dirs, repo)) is None
        assert not (repo / "extra.txt").exists()
        assert (repo / "metrics.jsonl").read_text().endswith('{"step": 99}\n')
        assert results_png.exists()

    async def test_pre_existing_dirt_is_not_the_steps_fault(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        # the previous round left uncommitted work; a clean scaffold edit is still kept
        (repo / "wip.py").write_text("work in progress\n")
        (repo / "NOTES.md").write_text("uncommitted notes\n")

        def edit(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).write_text("# Scaffold\n\nbetter\n")

        step = ScaffoldSelfEditStep(CallableEngine(edit), rsi_dirs.config, repo, timeout_seconds=5)
        sha = await step.propose(_inputs(rsi_dirs, repo))
        assert sha == scaffold_head(repo)
        assert (repo / "wip.py").exists()
        assert (repo / "NOTES.md").read_text() == "uncommitted notes\n"
        assert git(repo, "show", "--name-only", "--format=", "HEAD") == SCAFFOLD_FILENAME
        assert "NOTES.md" in git(repo, "status", "--porcelain")

    async def test_engine_failure_discards_partial_edit(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        head = scaffold_head(repo)

        def edit(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).write_text("half-writ")

        for engine in (
            CallableEngine(edit, exit_code=1),
            CallableEngine(edit, timed_out=True, exit_code=-9),
        ):
            step = ScaffoldSelfEditStep(engine, rsi_dirs.config, repo, timeout_seconds=5)
            assert await step.propose(_inputs(rsi_dirs, repo)) is None
            assert scaffold_head(repo) == head
            assert (
                repo / SCAFFOLD_FILENAME
            ).read_text() == "# Scaffold\n\nOriginal instructions.\n"

    async def test_deleting_scaffold_is_rejected(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        step = ScaffoldSelfEditStep(
            CallableEngine(lambda cwd: (cwd / SCAFFOLD_FILENAME).unlink()),
            rsi_dirs.config,
            repo,
            timeout_seconds=5,
        )
        assert await step.propose(_inputs(rsi_dirs, repo)) is None
        assert (repo / SCAFFOLD_FILENAME).read_text() == "# Scaffold\n\nOriginal instructions.\n"

    async def test_touching_verifier_lock_raises_and_stops(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        (repo / "VERIFIER.json").write_text('{"command": "true"}\n')
        git(repo, "add", "VERIFIER.json")
        git(repo, "commit", "-q", "-m", "lock")

        def edit(cwd: Path) -> None:
            (cwd / "VERIFIER.json").write_text('{"command": "false"}\n')
            (cwd / SCAFFOLD_FILENAME).write_text("# Scaffold\n\nlooks fine\n")

        step = ScaffoldSelfEditStep(CallableEngine(edit), rsi_dirs.config, repo, timeout_seconds=5)
        with pytest.raises(FrozenVerifierError):
            await step.propose(_inputs(rsi_dirs, repo))
        # nothing was "adjusted": the tampered file is left for the operator
        assert (repo / "VERIFIER.json").read_text() == '{"command": "false"}\n'

    def test_refuses_foreign_sandbox(self, rsi_dirs: RsiDirs, tmp_path: Path) -> None:
        with pytest.raises(ContractViolationError):
            ScaffoldSelfEditStep(CallableEngine(None), rsi_dirs.config, tmp_path)


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #


class TestRollback:
    async def test_rollback_restores_previous_scaffold(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        original = (repo / SCAFFOLD_FILENAME).read_text()

        def edit(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).write_text("# Scaffold\n\nworse idea\n")

        step = ScaffoldSelfEditStep(CallableEngine(edit), rsi_dirs.config, repo, timeout_seconds=5)
        sha = await step.propose(_inputs(rsi_dirs, repo))
        assert sha is not None
        assert (repo / SCAFFOLD_FILENAME).read_text() != original

        revert_sha = rollback_scaffold(repo, sha)
        assert revert_sha == scaffold_head(repo)
        assert revert_sha != sha
        assert (repo / SCAFFOLD_FILENAME).read_text() == original
        assert git(repo, "status", "--porcelain") == ""

    def test_rollback_refuses_non_scaffold_commit(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        (repo / "other.txt").write_text("x\n")
        git(repo, "add", "other.txt")
        git(repo, "commit", "-q", "-m", "not a scaffold commit")
        sha = git(repo, "rev-parse", "HEAD")
        with pytest.raises(ContractViolationError):
            rollback_scaffold(repo, sha)
        assert (repo / "other.txt").exists()

    def test_scaffold_head_none_without_commits(self, rsi_dirs: RsiDirs) -> None:
        git(rsi_dirs.sandbox, "init", "-q")
        assert scaffold_head(rsi_dirs.sandbox) is None


# --------------------------------------------------------------------------- #
# Regression tests from the adversarial review: an engine with bypassPermissions
# in the sandbox must not be able to keep anything but a SCAFFOLD.md edit.
# --------------------------------------------------------------------------- #

ORIGINAL = "# Scaffold\n\nOriginal instructions.\n"


def _edit_scaffold(cwd: Path, text: str = "# Scaffold\n\nlooks fine\n") -> None:
    (cwd / SCAFFOLD_FILENAME).write_text(text)


def _step(rsi_dirs: RsiDirs, repo: Path, action: Callable[[Path], None]) -> ScaffoldSelfEditStep:
    return ScaffoldSelfEditStep(CallableEngine(action), rsi_dirs.config, repo, timeout_seconds=5)


def _clean(repo: Path) -> bool:
    return git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all") == ""


class TestIgnoredPaths:
    async def test_gitignored_files_are_seen_and_removed(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        (repo / ".gitignore").write_text("data/\n*.pkl\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-q", "-m", "ignore")
        head = scaffold_head(repo)

        def edit(cwd: Path) -> None:
            (cwd / "data").mkdir()
            (cwd / "data" / "poison.csv").write_text("1,2,3\n")
            (cwd / "cache.pkl").write_bytes(b"\x80")
            _edit_scaffold(cwd)

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert scaffold_head(repo) == head
        assert not (repo / "data").exists()
        assert not (repo / "cache.pkl").exists()
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL
        assert _clean(repo)

    async def test_git_info_exclude_is_restored_and_hidden_file_removed(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        exclude = repo / ".git" / "info" / "exclude"
        before = exclude.read_text() if exclude.exists() else None

        def edit(cwd: Path) -> None:
            exclude.parent.mkdir(exist_ok=True)
            exclude.write_text("helper.py\n")
            (cwd / "helper.py").write_text("evil\n")
            _edit_scaffold(cwd)

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert not (repo / "helper.py").exists()
        assert (exclude.read_text() if exclude.exists() else None) == before
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL
        assert _clean(repo)


class TestEngineMovesHead:
    async def test_committed_verifier_tamper_raises_and_leaves_tree(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        (repo / "verify.py").write_text("print('score=0.1')\n")
        lock = compute_verifier_lock(
            VerifierSpec("python verify.py", files=("verify.py",)), repo, now_ms=1
        )
        (repo / "VERIFIER.json").write_text(json.dumps(lock.to_json()))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "lock")

        def edit(cwd: Path) -> None:
            (cwd / "verify.py").write_text("print('score=99')\n")
            new = compute_verifier_lock(
                VerifierSpec("python verify.py", files=("verify.py",)), cwd, now_ms=2
            )
            (cwd / "VERIFIER.json").write_text(json.dumps(new.to_json()))
            git(cwd, "add", "-A")
            git(cwd, "commit", "-q", "-m", "x")
            _edit_scaffold(cwd)

        with pytest.raises(FrozenVerifierError):
            await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo))
        # nothing was "adjusted": the tampering commit and the tree are left for the operator
        assert git(repo, "log", "-1", "--format=%s") == "x"
        assert (repo / "verify.py").read_text() == "print('score=99')\n"

    async def test_committed_pinned_file_change_alone_raises(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        (repo / "verify.py").write_text("print('score=0.1')\n")
        lock = compute_verifier_lock(
            VerifierSpec("python verify.py", files=("verify.py",)), repo, now_ms=1
        )
        (repo / "VERIFIER.json").write_text(json.dumps(lock.to_json()))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "lock")

        def edit(cwd: Path) -> None:
            (cwd / "verify.py").write_text("print('score=99')\n")

        with pytest.raises(FrozenVerifierError):
            await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo))
        assert (repo / "verify.py").read_text() == "print('score=99')\n"

    async def test_git_rm_of_lock_raises(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        (repo / "VERIFIER.json").write_text('{"command": "true"}\n')
        git(repo, "add", "VERIFIER.json")
        git(repo, "commit", "-q", "-m", "lock")

        def edit(cwd: Path) -> None:
            git(cwd, "rm", "-q", "VERIFIER.json")
            git(cwd, "commit", "-q", "-m", "drop lock")

        with pytest.raises(FrozenVerifierError):
            await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo))
        assert not (repo / "VERIFIER.json").exists()

    async def test_engine_commit_is_rejected_and_head_restored(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        head = scaffold_head(repo)

        def edit(cwd: Path) -> None:
            _edit_scaffold(cwd)
            (cwd / "payload.py").write_text("x\n")
            git(cwd, "add", "-A")
            git(cwd, "commit", "-q", "-m", "engine commit")

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert scaffold_head(repo) == head
        assert not (repo / "payload.py").exists()
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL
        assert _clean(repo)

    async def test_engine_commit_of_scaffold_only_is_still_rejected(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        head = scaffold_head(repo)

        def edit(cwd: Path) -> None:
            _edit_scaffold(cwd)
            git(cwd, "commit", "-q", "-am", "engine commit")

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert scaffold_head(repo) == head
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL
        assert _clean(repo)

    async def test_engine_reset_hard_is_rejected_and_head_restored(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        _edit_scaffold(repo, "# Scaffold\n\nedit A\n")
        git(repo, "commit", "-q", "-am", "A")
        head = scaffold_head(repo)

        assert (
            await _step(
                rsi_dirs, repo, lambda cwd: git(cwd, "reset", "-q", "--hard", "HEAD~1")
            ).propose(_inputs(rsi_dirs, repo))
            is None
        )
        assert scaffold_head(repo) == head
        assert (repo / SCAFFOLD_FILENAME).read_text() == "# Scaffold\n\nedit A\n"
        assert _clean(repo)

    async def test_staged_new_file_is_rejected_not_raised(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        def edit(cwd: Path) -> None:
            (cwd / "helper.py").write_text("x\n")
            git(cwd, "add", "helper.py")
            _edit_scaffold(cwd)

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert not (repo / "helper.py").exists()
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL
        assert _clean(repo)

    async def test_staged_rename_is_rejected_not_raised(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        def edit(cwd: Path) -> None:
            git(cwd, "mv", "NOTES.md", "NOTES2.md")
            _edit_scaffold(cwd)

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert (repo / "NOTES.md").exists()
        assert not (repo / "NOTES2.md").exists()
        assert _clean(repo)


class TestSymlinkedScaffold:
    async def test_symlink_to_outside_is_rejected_and_never_read(
        self, rsi_dirs: RsiDirs, repo: Path, tmp_path: Path
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET OUTSIDE SANDBOX\n")
        head = scaffold_head(repo)

        def edit(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).unlink()
            os.symlink(secret, cwd / SCAFFOLD_FILENAME)

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert scaffold_head(repo) == head
        assert not (repo / SCAFFOLD_FILENAME).is_symlink()
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL
        assert git(repo, "ls-files", "-s", SCAFFOLD_FILENAME).startswith("100644")
        assert _clean(repo)

    def test_inputs_refuse_symlinked_scaffold_or_notes(
        self, rsi_dirs: RsiDirs, repo: Path, tmp_path: Path
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET OUTSIDE SANDBOX\n")
        (repo / SCAFFOLD_FILENAME).unlink()
        os.symlink(secret, repo / SCAFFOLD_FILENAME)
        with pytest.raises(ContractViolationError) as excinfo:
            build_self_edit_inputs(rsi_dirs.config, [], repo, forbidden=[VERIFIER_CMD])
        assert "SECRET" not in str(excinfo.value)

        (repo / SCAFFOLD_FILENAME).unlink()
        (repo / SCAFFOLD_FILENAME).write_text(ORIGINAL)
        (repo / "NOTES.md").unlink()
        os.symlink(secret, repo / "NOTES.md")
        with pytest.raises(ContractViolationError):
            build_self_edit_inputs(rsi_dirs.config, [], repo, forbidden=[VERIFIER_CMD])

    def test_dangling_symlink_never_creates_file_outside(self, rsi_dirs: RsiDirs) -> None:
        sandbox = rsi_dirs.sandbox
        target = rsi_dirs.results / "STOP"
        os.symlink(target, sandbox / SCAFFOLD_FILENAME)
        with pytest.raises(ContractViolationError):
            read_or_create_scaffold(sandbox)
        assert not target.exists()

    async def test_directory_replacement_is_rejected(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        def edit(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).unlink()
            (cwd / SCAFFOLD_FILENAME).mkdir()
            (cwd / SCAFFOLD_FILENAME / "x.md").write_text("x\n")

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert (repo / SCAFFOLD_FILENAME).is_file()
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL
        assert _clean(repo)


class TestDiscardIsSafe:
    async def test_glob_filename_never_deletes_neighbours(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        (repo / "analysis.py").write_text("previous round's work\n")

        def edit(cwd: Path) -> None:
            (cwd / "a*").write_text("glob bait\n")
            (cwd / "[x]").write_text("more bait\n")
            _edit_scaffold(cwd)

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert (repo / "analysis.py").read_text() == "previous round's work\n"
        assert not (repo / "a*").exists()
        assert not (repo / "[x]").exists()

    async def test_reverting_or_deleting_previous_work_is_rejected_and_restored(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        (repo / "NOTES.md").write_text("round N notes\n")  # tracked, dirty
        (repo / "wip.py").write_text("hours of work\n")  # untracked

        def edit(cwd: Path) -> None:
            git(cwd, "checkout", "--", "NOTES.md")
            (cwd / "wip.py").unlink()
            _edit_scaffold(cwd)

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert (repo / "NOTES.md").read_text() == "round N notes\n"
        assert (repo / "wip.py").read_text() == "hours of work\n"
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL

    async def test_rejection_restores_pre_run_bytes_of_dirty_files(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        (repo / "NOTES.md").write_text("round N notes\n")
        (repo / "wip.py").write_text("hours of work\n")

        def edit(cwd: Path) -> None:
            with (cwd / "NOTES.md").open("a") as fh:
                fh.write("engine line\n")
            with (cwd / "wip.py").open("a") as fh:
                fh.write("# engine comment\n")
            _edit_scaffold(cwd)

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert (repo / "NOTES.md").read_text() == "round N notes\n"
        assert (repo / "wip.py").read_text() == "hours of work\n"

    async def test_symlink_into_results_dir_is_removed(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        (rsi_dirs.results / "keep.txt").write_text("results\n")

        def edit(cwd: Path) -> None:
            os.symlink(rsi_dirs.results, cwd / "res")
            os.symlink("/", cwd / "root")
            (cwd / "junk.txt").write_text("x\n")

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert not (repo / "res").is_symlink()
        assert not (repo / "root").is_symlink()
        assert not (repo / "junk.txt").exists()
        assert (rsi_dirs.results / "keep.txt").read_text() == "results\n"

    async def test_nested_sandbox_is_refused(self, rsi_dirs: RsiDirs, tmp_path: Path) -> None:
        git(tmp_path, "init", "-q")  # the sandbox lives inside this repo, without its own .git
        step = ScaffoldSelfEditStep(
            CallableEngine(None), rsi_dirs.config, rsi_dirs.sandbox, timeout_seconds=5
        )
        (rsi_dirs.sandbox / SCAFFOLD_FILENAME).write_text(ORIGINAL)
        with pytest.raises(ContractViolationError):
            await step.propose(_inputs(rsi_dirs, rsi_dirs.sandbox))


class TestGitInternals:
    async def test_planted_hook_is_rejected_removed_and_never_runs(
        self, rsi_dirs: RsiDirs, repo: Path, tmp_path: Path
    ) -> None:
        marker = tmp_path / "hook-ran"
        hook = repo / ".git" / "hooks" / "pre-commit"

        def edit(cwd: Path) -> None:
            hook.parent.mkdir(exist_ok=True)
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
            hook.chmod(0o755)
            _edit_scaffold(cwd)

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert not hook.exists()
        assert not marker.exists()
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL

    async def test_pre_existing_hooks_never_run_in_commit_or_rollback(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"):
            (hooks / name).write_text("#!/bin/sh\nexit 1\n")
            (hooks / name).chmod(0o755)

        sha = await _step(rsi_dirs, repo, _edit_scaffold).propose(_inputs(rsi_dirs, repo))
        assert sha == scaffold_head(repo)
        revert = rollback_scaffold(repo, sha)
        assert revert == scaffold_head(repo)
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL

    async def test_config_and_head_changes_are_rejected_and_restored(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        config = (repo / ".git" / "config").read_text()
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")

        def edit(cwd: Path) -> None:
            git(cwd, "config", "core.hooksPath", "/tmp/evil-hooks")
            git(cwd, "checkout", "-q", "-b", "engine-branch")
            _edit_scaffold(cwd)

        assert await _step(rsi_dirs, repo, edit).propose(_inputs(rsi_dirs, repo)) is None
        assert (repo / ".git" / "config").read_text() == config
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == branch
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL


class TestRollbackSafety:
    async def test_conflicting_revert_is_aborted_and_raised(
        self, rsi_dirs: RsiDirs, repo: Path
    ) -> None:
        step_a = _step(rsi_dirs, repo, lambda cwd: _edit_scaffold(cwd, "# Scaffold\n\nedit A\n"))
        sha_a = await step_a.propose(_inputs(rsi_dirs, repo))
        assert sha_a is not None
        step_b = _step(rsi_dirs, repo, lambda cwd: _edit_scaffold(cwd, "# Scaffold\n\nedit B\n"))
        sha_b = await step_b.propose(_inputs(rsi_dirs, repo))
        assert sha_b is not None

        with pytest.raises(ContractViolationError):
            rollback_scaffold(repo, sha_a)
        assert not (repo / ".git" / "REVERT_HEAD").exists()
        assert git(repo, "status", "--porcelain") == ""
        assert "<<<<<<<" not in (repo / SCAFFOLD_FILENAME).read_text()
        assert (repo / SCAFFOLD_FILENAME).read_text() == "# Scaffold\n\nedit B\n"
        assert scaffold_head(repo) == sha_b

    def test_rollback_accepts_only_commit_shas(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        for bad in ("HEAD~1", "HEAD", "", "main", "deadbeef"):
            with pytest.raises(ContractViolationError):
                rollback_scaffold(repo, bad)

    async def test_async_wrappers(self, rsi_dirs: RsiDirs, repo: Path) -> None:
        from turing.research.rsi.scaffold import rollback_scaffold_async, scaffold_head_async

        sha = await _step(rsi_dirs, repo, _edit_scaffold).propose(_inputs(rsi_dirs, repo))
        assert sha is not None
        assert await scaffold_head_async(repo) == sha
        assert await rollback_scaffold_async(repo, sha) == scaffold_head(repo)
        assert (repo / SCAFFOLD_FILENAME).read_text() == ORIGINAL
