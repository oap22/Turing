"""The verifier runner measures; the lock is written once and refuses a different command."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import time
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import ContractViolationError, FrozenVerifierError
from turing.research.rsi import verifier as verifier_module
from turing.research.rsi.contracts import VERIFIER_LOCK_FILENAME, VerifierSpec, sha256_text
from turing.research.rsi.engine import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DRAIN_GRACE_SECONDS,
    OUTPUT_LIMIT_EXIT,
    CappedOutput,
)
from turing.research.rsi.verifier import (
    VERIFIER_TIMEOUT_EXIT,
    load_verifier_lock,
    run_verifier,
    sandbox_file_tokens,
    write_or_load_verifier,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestRunVerifier:
    async def test_pass_with_last_score_line(self, sandbox: Path) -> None:
        spec = VerifierSpec(command="printf 'score=1\\nnoise\\nscore=2.5\\n'")
        out = await run_verifier(spec, sandbox, 10)
        assert out.passed and out.exit_code == 0
        assert out.score == 2.5
        assert "score=2.5" in out.stdout_tail
        assert out.wall_seconds >= 0

    async def test_pass_without_score_is_pass_fail_only(self, sandbox: Path) -> None:
        out = await run_verifier(VerifierSpec(command="echo ok"), sandbox, 10)
        assert out.passed and out.score is None

    async def test_failure_exit_code_is_fail_and_score_dropped(self, sandbox: Path) -> None:
        out = await run_verifier(
            VerifierSpec(command="echo score=9; echo err >&2; exit 3"), sandbox, 10
        )
        assert not out.passed and out.exit_code == 3
        assert out.score is None
        assert "[stderr] err" in out.stdout_tail

    async def test_runs_from_sandbox(self, sandbox: Path) -> None:
        (sandbox / "SCORE").write_text("score=4\n")
        out = await run_verifier(VerifierSpec(command="cat SCORE"), sandbox, 10)
        assert out.passed and out.score == 4.0

    @pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
    async def test_timeout_is_a_failure_never_a_pass(self, sandbox: Path) -> None:
        out = await run_verifier(VerifierSpec(command="echo score=1; sleep 30"), sandbox, 0.3)
        assert not out.passed
        assert out.exit_code == VERIFIER_TIMEOUT_EXIT
        assert out.score is None
        assert out.wall_seconds < 10

    async def test_non_positive_timeout_refused(self, sandbox: Path) -> None:
        with pytest.raises(ContractViolationError):
            await run_verifier(VerifierSpec(command="true"), sandbox, 0)

    async def test_output_overflow_discards_score_even_when_process_exits_zero(
        self, sandbox: Path
    ) -> None:
        out = await run_verifier(
            VerifierSpec(
                command=(
                    "printf 'score=100\\n'; "
                    f"head -c {DEFAULT_MAX_OUTPUT_BYTES + 1} /dev/zero; "
                    "printf '\\nscore=100\\n'"
                )
            ),
            sandbox,
            30,
        )
        assert not out.passed
        assert out.exit_code == OUTPUT_LIMIT_EXIT
        assert out.score is None
        assert "output limit exceeded" in out.stdout_tail

    async def test_stderr_overflow_fails_with_valid_small_stdout_score(
        self, sandbox: Path, monkeypatch
    ) -> None:
        observed: CappedOutput | None = None
        original_run_capped = verifier_module.run_capped

        async def observe(proc, **kwargs):
            nonlocal observed
            observed = await original_run_capped(proc, **kwargs)
            return observed

        monkeypatch.setattr(verifier_module, "run_capped", observe)
        out = await run_verifier(
            VerifierSpec(
                command=f"printf 'score=7\\n'; head -c {DEFAULT_MAX_OUTPUT_BYTES + 1} /dev/zero >&2"
            ),
            sandbox,
            30,
        )
        assert not out.passed
        assert out.exit_code == OUTPUT_LIMIT_EXIT
        assert out.score is None
        assert "output limit exceeded" in out.stdout_tail
        assert observed is not None and b"score=7\n" in observed.stdout

    async def test_verifier_maps_simultaneous_timeout_and_overflow_flags(
        self, sandbox: Path, monkeypatch
    ) -> None:
        original_run_capped = verifier_module.run_capped

        async def capped(proc, **kwargs):
            cleaned = await original_run_capped(proc, **kwargs)
            return CappedOutput(
                exit_code=124,
                stdout=cleaned.stdout + b"score=100\n",
                stderr=cleaned.stderr,
                timed_out=True,
                pipe_abandoned=False,
                output_limit_exceeded=True,
            )

        monkeypatch.setattr(verifier_module, "run_capped", capped)
        out = await run_verifier(VerifierSpec(command="true"), sandbox, 5)
        assert not out.passed
        assert out.exit_code == OUTPUT_LIMIT_EXIT
        assert out.score is None


class TestWriteOrLoadVerifier:
    def test_first_run_writes_lock_with_command_and_pinned_files(self, sandbox: Path) -> None:
        (sandbox / "verify.sh").write_text("cat SCORE\n")
        spec = VerifierSpec(command="sh verify.sh", files=("verify.sh",))
        lock, resolved = write_or_load_verifier(spec, sandbox, now_ms=123)
        path = sandbox / VERIFIER_LOCK_FILENAME
        payload = json.loads(path.read_text())
        assert payload["command"] == "sh verify.sh"
        assert payload["command_sha256"] == sha256_text("sh verify.sh")
        assert set(payload["file_sha256s"]) == {"verify.sh"}
        assert payload["created_at_ms"] == 123
        assert lock.command == "sh verify.sh"
        assert resolved.files == ("verify.sh",)

    def test_first_run_pins_first_token_file_automatically(self, sandbox: Path) -> None:
        script = sandbox / "verify.py"
        script.write_text("print('score=1')\n")
        lock, _ = write_or_load_verifier(
            VerifierSpec(command="verify.py --fast"), sandbox, now_ms=1
        )
        assert "verify.py" in lock.file_sha256s

    def test_first_run_without_spec_refused(self, sandbox: Path) -> None:
        with pytest.raises(ContractViolationError, match="--verifier is required"):
            write_or_load_verifier(None, sandbox)
        assert load_verifier_lock(sandbox) is None

    def test_resume_without_flag_loads_lock(self, sandbox: Path) -> None:
        written, _ = write_or_load_verifier(VerifierSpec(command="echo score=1"), sandbox, now_ms=5)
        loaded, spec = write_or_load_verifier(None, sandbox)
        assert loaded == written
        assert spec.command == "echo score=1"

    def test_resume_with_same_flag_is_fine(self, sandbox: Path) -> None:
        write_or_load_verifier(VerifierSpec(command="echo score=1"), sandbox, now_ms=5)
        loaded, _ = write_or_load_verifier(VerifierSpec(command="echo score=1"), sandbox)
        assert loaded.command == "echo score=1"

    def test_resume_with_different_flag_is_refused_loudly(self, sandbox: Path) -> None:
        write_or_load_verifier(VerifierSpec(command="echo score=1"), sandbox, now_ms=5)
        with pytest.raises(FrozenVerifierError, match="differs from the locked verifier"):
            write_or_load_verifier(VerifierSpec(command="echo score=100"), sandbox)
        # And the lock on disk is untouched.
        lock = load_verifier_lock(sandbox)
        assert lock is not None and lock.command == "echo score=1"

    def test_resume_with_extra_pinned_file_is_refused(self, sandbox: Path) -> None:
        (sandbox / "a.txt").write_text("a")
        write_or_load_verifier(VerifierSpec(command="cat a.txt"), sandbox, now_ms=5)
        with pytest.raises(FrozenVerifierError, match="pinned files"):
            write_or_load_verifier(
                VerifierSpec(command="cat a.txt", files=("a.txt", "b.txt")), sandbox
            )

    def test_resume_with_omitted_pinned_file_is_refused(self, sandbox: Path) -> None:
        (sandbox / "a.txt").write_text("a")
        (sandbox / "b.txt").write_text("b")
        write_or_load_verifier(
            VerifierSpec(command="echo score=1", files=("a.txt", "b.txt")),
            sandbox,
            now_ms=5,
        )
        with pytest.raises(FrozenVerifierError, match="pinned files"):
            write_or_load_verifier(VerifierSpec(command="echo score=1", files=("a.txt",)), sandbox)

    def test_resume_keeps_auto_first_token_pin_when_explicit_files_are_subset(
        self, sandbox: Path
    ) -> None:
        (sandbox / "verify.sh").write_text("echo score=1\n")
        (sandbox / "a.txt").write_text("a")
        written, _ = write_or_load_verifier(
            VerifierSpec(command="./verify.sh", files=("a.txt",)), sandbox, now_ms=5
        )
        loaded, _ = write_or_load_verifier(
            VerifierSpec(command="./verify.sh", files=("a.txt",)), sandbox
        )
        assert set(written.file_sha256s) == {"a.txt", "verify.sh"}
        assert loaded == written

    def test_resume_with_changed_pinned_file_is_refused(self, sandbox: Path) -> None:
        (sandbox / "verify.sh").write_text("echo score=1\n")
        write_or_load_verifier(
            VerifierSpec(command="sh verify.sh", files=("verify.sh",)), sandbox, now_ms=5
        )
        (sandbox / "verify.sh").write_text("echo score=999\n")
        with pytest.raises(FrozenVerifierError, match="changed"):
            write_or_load_verifier(None, sandbox)

    def test_lock_is_never_rewritten(self, sandbox: Path) -> None:
        write_or_load_verifier(VerifierSpec(command="true"), sandbox, now_ms=5)
        before = (sandbox / VERIFIER_LOCK_FILENAME).read_bytes()
        write_or_load_verifier(None, sandbox, now_ms=999)
        assert (sandbox / VERIFIER_LOCK_FILENAME).read_bytes() == before

    def test_malformed_lock_is_a_frozen_verifier_error(self, sandbox: Path) -> None:
        (sandbox / VERIFIER_LOCK_FILENAME).write_text("{not json")
        with pytest.raises(FrozenVerifierError, match="unreadable"):
            load_verifier_lock(sandbox)
        (sandbox / VERIFIER_LOCK_FILENAME).write_text("[]")
        with pytest.raises(FrozenVerifierError, match="not a JSON object"):
            load_verifier_lock(sandbox)
        (sandbox / VERIFIER_LOCK_FILENAME).write_text(
            json.dumps(
                {"command": "x", "command_sha256": "0" * 64, "file_sha256s": {}, "created_at_ms": 1}
            )
        )
        with pytest.raises(FrozenVerifierError):
            load_verifier_lock(sandbox)


class TestLockPinsFirstTokenAndListedFiles:
    """Pinning is literal (I1, brief §1): the first token when it is a sandbox file, plus
    every ``--verifier-file``. Other named files are warned about, never auto-pinned —
    ``./grade.sh out.csv`` must keep working when the agent rewrites ``out.csv``."""

    def test_first_token_script_is_pinned(self, sandbox: Path) -> None:
        (sandbox / "verify.sh").write_text("cat SCORE\n")
        (sandbox / "verify.sh").chmod(0o755)
        lock, resolved = write_or_load_verifier(
            VerifierSpec(command="./verify.sh out.csv"), sandbox, now_ms=1
        )
        assert set(lock.file_sha256s) == {"verify.sh"}
        assert resolved.files == ("verify.sh",)

    def test_interpreter_command_pins_only_listed_files(self, sandbox: Path) -> None:
        (sandbox / "grade.py").write_text("print('score=1')\n")
        (sandbox / "data").mkdir()
        (sandbox / "data" / "answers.csv").write_text("1,2\n")
        lock, _ = write_or_load_verifier(
            VerifierSpec(
                command="python3 grade.py --key data/answers.csv --fast", files=("grade.py",)
            ),
            sandbox,
            now_ms=1,
        )
        assert set(lock.file_sha256s) == {"grade.py"}
        assert sandbox_file_tokens("python3 grade.py --key data/answers.csv", sandbox) == [
            "grade.py",
            "data/answers.csv",
        ]

    def test_unlisted_interpreter_script_is_not_pinned(self, sandbox: Path) -> None:
        (sandbox / "grade.py").write_text("print('score=1')\n")
        lock, _ = write_or_load_verifier(
            VerifierSpec(command="python3 grade.py"), sandbox, now_ms=1
        )
        assert lock.file_sha256s == {}

    def test_rewriting_a_pinned_script_is_refused_on_resume(self, sandbox: Path) -> None:
        (sandbox / "verify.sh").write_text("cat SCORE\n")
        write_or_load_verifier(
            VerifierSpec(command="sh verify.sh", files=("verify.sh",)), sandbox, now_ms=1
        )
        (sandbox / "verify.sh").write_text("echo score=1000\n")
        with pytest.raises(FrozenVerifierError, match=r"verify\.sh"):
            write_or_load_verifier(None, sandbox)

    def test_tokens_outside_the_sandbox_or_absolute_are_not_named(
        self, sandbox: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.sh"
        outside.write_text("true\n")
        os.symlink(outside, sandbox / "link.sh")
        assert sandbox_file_tokens(f"sh {outside} ../outside.sh link.sh", sandbox) == []


class TestMalformedLockIsAlwaysFrozenVerifierError:
    def test_empty_command_lock(self, sandbox: Path) -> None:
        (sandbox / VERIFIER_LOCK_FILENAME).write_text(
            json.dumps(
                {
                    "command": "",
                    "command_sha256": sha256_text(""),
                    "file_sha256s": {},
                    "created_at_ms": 1,
                }
            )
        )
        with pytest.raises(FrozenVerifierError, match="malformed"):
            load_verifier_lock(sandbox)

    def test_non_int_created_at_lock(self, sandbox: Path) -> None:
        (sandbox / VERIFIER_LOCK_FILENAME).write_text(
            json.dumps(
                {
                    "command": "true",
                    "command_sha256": sha256_text("true"),
                    "file_sha256s": {},
                    "created_at_ms": "soon",
                }
            )
        )
        with pytest.raises(FrozenVerifierError):
            load_verifier_lock(sandbox)


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
class TestVerifierTimeoutIsHard:
    @pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid(1)")
    async def test_stray_pipe_holder_cannot_stall_the_verifier(self, sandbox: Path) -> None:
        spec = VerifierSpec(command="setsid sh -c 'sleep 8' & echo $! > stray.pid; sleep 30")
        started = time.monotonic()
        try:
            out = await run_verifier(spec, sandbox, 0.3)
            wall = time.monotonic() - started
        finally:
            with contextlib.suppress(OSError, ValueError):
                os.kill(int((sandbox / "stray.pid").read_text().strip()), signal.SIGKILL)
        assert not out.passed and out.exit_code == VERIFIER_TIMEOUT_EXIT
        assert wall < 0.3 + 2 * DRAIN_GRACE_SECONDS + 1.0

    async def test_background_child_of_a_passing_verifier_is_killed(self, sandbox: Path) -> None:
        (sandbox / "key.txt").write_text("original\n")
        spec = VerifierSpec(
            command="( sleep 0.4; echo tampered > key.txt ) >/dev/null 2>&1 & echo score=1"
        )
        out = await run_verifier(spec, sandbox, 10)
        assert out.passed and out.score == 1.0
        await asyncio.sleep(1.0)
        assert (sandbox / "key.txt").read_text() == "original\n"
