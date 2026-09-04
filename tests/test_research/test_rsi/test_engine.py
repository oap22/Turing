"""Engines: the fake replays its script faithfully; the CLI engine builds argv and honours timeouts."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sys
import time
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.rsi.contracts import EngineResult
from turing.research.rsi.engine import (
    DRAIN_GRACE_SECONDS,
    ClaudeCliEngine,
    FakeEngine,
    ok_result,
    run_capped,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestFakeEngine:
    async def test_replays_results_and_records_calls(self, tmp_path: Path) -> None:
        first = ok_result("one")
        second = EngineResult(
            exit_code=2, stdout="", stderr="boom", wall_seconds=0.5, timed_out=False
        )
        engine = FakeEngine(script=[first, second])
        assert await engine.run("p1", cwd=tmp_path, timeout_seconds=5) is first
        assert await engine.run("p2", cwd=tmp_path, timeout_seconds=5) is second
        assert [c.prompt for c in engine.calls] == ["p1", "p2"]
        assert engine.calls[0].cwd == tmp_path
        assert engine.calls[0].timeout_seconds == 5
        assert engine.remaining == 0

    async def test_callables_may_mutate_cwd_sync_and_async(self, tmp_path: Path) -> None:
        def sync_step(prompt: str, cwd: Path) -> EngineResult:
            (cwd / "sync.txt").write_text(prompt)
            return ok_result()

        async def async_step(prompt: str, cwd: Path) -> EngineResult:
            (cwd / "async.txt").write_text(prompt)
            return ok_result()

        engine = FakeEngine(script=[sync_step, async_step])
        await engine.run("a", cwd=tmp_path, timeout_seconds=1)
        await engine.run("b", cwd=tmp_path, timeout_seconds=1)
        assert (tmp_path / "sync.txt").read_text() == "a"
        assert (tmp_path / "async.txt").read_text() == "b"

    async def test_exhausted_script_without_default_refuses(self, tmp_path: Path) -> None:
        engine = FakeEngine(script=[])
        with pytest.raises(ContractViolationError, match="exhausted"):
            await engine.run("p", cwd=tmp_path, timeout_seconds=1)

    async def test_exhausted_script_falls_back_to_default(self, tmp_path: Path) -> None:
        default = ok_result("default")
        engine = FakeEngine(script=[], default=default)
        assert await engine.run("p", cwd=tmp_path, timeout_seconds=1) is default

    async def test_callable_must_return_engine_result(self, tmp_path: Path) -> None:
        engine = FakeEngine(script=[lambda _p, _c: "nope"])  # type: ignore[list-item]
        with pytest.raises(ContractViolationError, match="EngineResult"):
            await engine.run("p", cwd=tmp_path, timeout_seconds=1)


class TestClaudeCliEngine:
    def test_argv_is_exactly_the_bash_invocation_plus_text_output(self) -> None:
        engine = ClaudeCliEngine("claude")
        assert engine.argv("hello world") == [
            "claude",
            "-p",
            "hello world",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "text",
        ]

    def test_empty_binary_refused(self) -> None:
        with pytest.raises(ContractViolationError):
            ClaudeCliEngine("")

    async def test_missing_binary_is_reported_not_raised(self, tmp_path: Path) -> None:
        engine = ClaudeCliEngine(str(tmp_path / "definitely-not-here"))
        result = await engine.run("p", cwd=tmp_path, timeout_seconds=5)
        assert result.exit_code == 127
        assert not result.timed_out
        assert "could not start" in result.stderr

    async def test_runs_real_subprocess_and_reports_exit_code(self, tmp_path: Path) -> None:
        # A stand-in "claude": echoes its argv and exits 7, so we see both the
        # argv the engine built and the truthful exit code.
        fake = tmp_path / "claude"
        fake.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\"\nexit 7\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        engine = ClaudeCliEngine(str(fake))
        result = await engine.run("the prompt", cwd=tmp_path, timeout_seconds=10)
        assert result.exit_code == 7
        assert not result.timed_out
        lines = result.stdout.splitlines()
        assert lines[:2] == ["-p", "the prompt"]
        assert "--output-format" in lines

    @pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
    async def test_timeout_kills_process_group(self, tmp_path: Path) -> None:
        # The stand-in spawns a grandchild that would outlive a plain kill of
        # the child; killing the group must take it down too.
        fake = tmp_path / "claude"
        fake.write_text(
            "#!/bin/sh\n(sleep 30; echo late) &\necho $! > child.pid\nsleep 30\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        engine = ClaudeCliEngine(str(fake))
        result = await engine.run("p", cwd=tmp_path, timeout_seconds=0.3)
        assert result.timed_out
        assert result.exit_code == 124
        assert result.wall_seconds < 10

        pid = int((tmp_path / "child.pid").read_text().strip())
        # After a group kill the grandchild is gone (or a zombie awaiting its
        # dead parent's reaper); a live sleep would still answer signal 0.
        alive = True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
        if alive:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                assert fh.read().split(")")[-1].split()[0] in {"Z", "X"}


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
class TestProcessGroupBoundary:
    async def test_normal_exit_kills_background_children(self, tmp_path: Path) -> None:
        # The stand-in exits 0 at once but leaves a child that would rewrite a
        # pinned file 0.4s later — between the loop's post-engine lock check
        # and the verifier's read. The group kill on normal exit takes it down.
        fake = tmp_path / "claude"
        fake.write_text(
            "#!/bin/sh\n( sleep 0.4; echo tampered > key.txt ) >/dev/null 2>&1 &\nexit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        (tmp_path / "key.txt").write_text("original\n")
        engine = ClaudeCliEngine(str(fake))
        result = await engine.run("p", cwd=tmp_path, timeout_seconds=10)
        assert result.exit_code == 0 and not result.timed_out
        await asyncio.sleep(1.0)
        assert (tmp_path / "key.txt").read_text() == "original\n"

    @pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid(1)")
    async def test_stray_pipe_holder_cannot_stall_past_the_grace(self, tmp_path: Path) -> None:
        # A descendant that left the group (setsid) inherits stdout and survives
        # the kill; the drain must still be bounded.
        fake = tmp_path / "claude"
        fake.write_text(
            "#!/bin/sh\nsetsid sh -c 'sleep 8' &\necho $! > stray.pid\nsleep 30\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        engine = ClaudeCliEngine(str(fake))
        started = time.monotonic()
        try:
            result = await engine.run("p", cwd=tmp_path, timeout_seconds=0.3)
            wall = time.monotonic() - started
        finally:
            with contextlib.suppress(OSError, ValueError):
                os.kill(int((tmp_path / "stray.pid").read_text().strip()), signal.SIGKILL)
        assert result.timed_out and result.exit_code == 124
        assert wall < 0.3 + 2 * DRAIN_GRACE_SECONDS + 1.0

    @pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid(1)")
    async def test_run_capped_returns_captured_output_when_pipe_is_abandoned(
        self, tmp_path: Path
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            "echo partial; setsid sh -c 'sleep 8' & echo $! > stray.pid; exit 3",
            cwd=str(tmp_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            capped = await run_capped(proc, timeout_seconds=5, grace_seconds=0.5)
        finally:
            with contextlib.suppress(OSError, ValueError):
                os.kill(int((tmp_path / "stray.pid").read_text().strip()), signal.SIGKILL)
        assert capped.exit_code == 3 and not capped.timed_out
        assert capped.pipe_abandoned
        assert capped.stdout == b"partial\n"
