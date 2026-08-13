"""Tests for argv-only subprocess execution.

These are the only tests in the package that fork real processes, and every
one of them runs a sub-second Python one-liner. The corpus's real baselines
never appear here.
"""

from __future__ import annotations

import os
import sys

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.problems.process import (
    MAX_CAPTURED_OUTPUT,
    SubprocessCommandRunner,
    count_passing_tests,
    passing_test_counts,
    pythonpath_for_workspace,
    render_argv,
    run_command,
)


class TestRunCommand:
    async def test_captures_stdout_and_reports_success(self, tmp_path):
        result = await run_command(
            [sys.executable, "-c", "print('hello harness')"],
            cwd=tmp_path,
            timeout_seconds=30.0,
        )
        assert result.succeeded
        assert result.exit_code == 0
        assert "hello harness" in result.stdout
        assert result.seconds > 0

    async def test_nonzero_exit_is_a_failure_not_an_exception(self, tmp_path):
        result = await run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
            cwd=tmp_path,
            timeout_seconds=30.0,
        )
        assert not result.succeeded
        assert result.exit_code == 3
        assert "boom" in result.stderr

    async def test_timeout_is_a_failure_never_a_fast_result(self, tmp_path):
        """A killed command must not read as a command that finished quickly.

        This is the failure mode a timing harness is most exposed to: a
        benchmark that hangs and gets killed has a small wall-clock number
        attached to it, and treating that as a measurement would score an
        infinite speedup.
        """
        result = await run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout_seconds=0.5,
        )
        assert result.timed_out
        assert not result.succeeded
        assert result.seconds < 30

    async def test_runs_in_the_given_working_directory(self, tmp_path):
        (tmp_path / "marker.txt").write_text("present")
        result = await run_command(
            [sys.executable, "-c", "import pathlib; print(pathlib.Path('marker.txt').read_text())"],
            cwd=tmp_path,
            timeout_seconds=30.0,
        )
        assert "present" in result.stdout

    async def test_output_is_truncated_from_the_front(self, tmp_path):
        result = await run_command(
            [sys.executable, "-c", f"print('x' * {MAX_CAPTURED_OUTPUT * 3})"],
            cwd=tmp_path,
            timeout_seconds=60.0,
        )
        assert result.stdout.startswith("…<truncated>…")
        assert len(result.stdout) < MAX_CAPTURED_OUTPUT * 2

    async def test_rejects_an_empty_argv(self, tmp_path):
        with pytest.raises(ContractViolationError):
            await run_command([], cwd=tmp_path, timeout_seconds=1.0)

    async def test_rejects_a_non_positive_timeout(self, tmp_path):
        with pytest.raises(ContractViolationError):
            await run_command(["true"], cwd=tmp_path, timeout_seconds=0.0)

    def test_summary_names_the_command_and_how_it_ended(self):
        from turing.research.problems.process import CommandResult

        result = CommandResult(
            argv=("pytest", "-q"), exit_code=1, stdout="", stderr="", seconds=2.5
        )
        assert "pytest -q" in result.summary()
        assert "exit 1" in result.summary()


class TestSubprocessCommandRunner:
    async def test_applies_pinned_base_environment(self, tmp_path):
        runner = SubprocessCommandRunner(base_env={"TURING_RESEARCH_PROBE": "pinned"})
        result = await runner(
            [sys.executable, "-c", "import os; print(os.environ['TURING_RESEARCH_PROBE'])"],
            cwd=tmp_path,
            timeout_seconds=30.0,
        )
        assert "pinned" in result.stdout

    async def test_per_call_overrides_win_over_the_base_environment(self, tmp_path):
        runner = SubprocessCommandRunner(base_env={"TURING_RESEARCH_PROBE": "base"})
        result = await runner(
            [sys.executable, "-c", "import os; print(os.environ['TURING_RESEARCH_PROBE'])"],
            cwd=tmp_path,
            timeout_seconds=30.0,
            env_overrides={"TURING_RESEARCH_PROBE": "override"},
        )
        assert "override" in result.stdout


class TestRenderArgv:
    def test_substitutes_all_three_placeholders(self, tmp_path):
        rendered = render_argv(
            ("{python}", "{harness}/benchmarks/x.py", "--workspace", "{workspace}"),
            workspace=tmp_path / "ws",
            harness_root=tmp_path / "harness",
            python_executable="/usr/bin/python3",
        )
        assert rendered[0] == "/usr/bin/python3"
        assert rendered[1] == f"{tmp_path / 'harness'}/benchmarks/x.py"
        assert rendered[3] == str(tmp_path / "ws")

    def test_leaves_plain_arguments_untouched(self, tmp_path):
        rendered = render_argv(
            ("npm", "test", "--", "packages/core/src/retry.test.ts"),
            workspace=tmp_path,
            harness_root=tmp_path,
            python_executable="python",
        )
        assert rendered == ("npm", "test", "--", "packages/core/src/retry.test.ts")

    def test_rejects_an_unknown_placeholder(self, tmp_path):
        """A typo must not silently resolve to a path that does not exist."""
        with pytest.raises(ContractViolationError, match="unknown placeholder"):
            render_argv(
                ("{python}", "{harnes}/benchmarks/x.py"),
                workspace=tmp_path,
                harness_root=tmp_path,
                python_executable="python",
            )


class TestPythonpathForWorkspace:
    def test_puts_workspace_src_first(self, tmp_path):
        workspace = tmp_path / "ws"
        path = pythonpath_for_workspace(workspace, existing="/opt/other")
        assert path.split(os.pathsep)[0] == str(workspace / "src")
        assert "/opt/other" in path.split(os.pathsep)

    def test_does_not_duplicate_an_existing_workspace_src_entry(self, tmp_path):
        workspace = tmp_path / "ws"
        prefix = str(workspace / "src")
        path = pythonpath_for_workspace(workspace, existing=prefix)
        assert path.split(os.pathsep).count(prefix) == 1

    async def test_a_plain_script_imports_the_workspace_copy_not_the_editable_install(
        self, tmp_path
    ):
        """The confirmed cheat: without PYTHONPATH, `import turing` is this checkout."""
        workspace = tmp_path / "ws"
        package = workspace / "src" / "turing"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("ORIGIN = 'workspace-copy'\n")
        probe = [sys.executable, "-c", "import turing; print(turing.ORIGIN)"]
        result = await run_command(
            probe,
            cwd=workspace,
            timeout_seconds=30.0,
            env_overrides={"PYTHONPATH": pythonpath_for_workspace(workspace, existing="")},
        )
        assert result.succeeded, result.stderr
        assert result.stdout.strip() == "workspace-copy"


class TestCountPassingTests:
    def test_parses_pytest_quiet_summary(self):
        assert count_passing_tests("41 passed in 1.23s", "") == 41

    def test_prefers_the_vitest_tests_line_over_the_file_count(self):
        """vitest prints the file count first; the file count is the wrong number."""
        stdout = "Test Files  112 passed (112)\n Tests  21 passed (21)\n"
        assert count_passing_tests(stdout, "") == 21

    def test_reads_a_mixed_vitest_summary(self):
        stdout = "Test Files  1 failed | 111 passed (112)\n Tests  18 failed | 21 passed (39)\n"
        assert count_passing_tests(stdout, "") == 21

    def test_falls_back_to_stderr(self):
        assert count_passing_tests("", "52 passed in 0.07s") == 52

    def test_returns_none_when_nothing_reported_a_count(self):
        assert count_passing_tests("no tests ran", "") is None

    def test_an_extra_summary_is_unparseable_not_the_last_match(self):
        """The confirmed conftest atexit exploit: last match used to win."""
        stdout = "1 passed in 0.01s\n52 passed in 0.01s"
        assert passing_test_counts(stdout, "") == (1, 52)
        assert count_passing_tests(stdout, "") is None

    def test_two_identical_summaries_are_still_unparseable(self):
        """Agreeing twice is still two summaries; uniqueness is not the test."""
        stdout = "52 passed in 0.01s\n52 passed in 0.01s"
        assert passing_test_counts(stdout, "") == (52, 52)
        assert count_passing_tests(stdout, "") is None

    def test_an_extra_vitest_tests_line_is_unparseable(self):
        stdout = " Tests  1 passed (1)\n Tests  52 passed (52)\n"
        assert passing_test_counts(stdout, "") == (1, 52)
        assert count_passing_tests(stdout, "") is None
