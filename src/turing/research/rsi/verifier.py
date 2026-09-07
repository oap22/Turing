"""Run the frozen verifier and keep its lock file, ``<sandbox>/VERIFIER.json``.

The verifier is an operator-supplied shell command run from the sandbox. Its
exit code is pass/fail; the last stdout line matching ``score=<float>`` is
the score. The loop measures it — the agent's own metrics line is never the
recorded score (I2).

What this module guarantees:

* :func:`write_or_load_verifier` writes the lock on a sandbox's first run
  and, on every later run, reads the lock back and re-checks it against the
  sandbox. A ``--verifier`` that differs from the locked command is refused
  with :class:`~turing.research.contracts.FrozenVerifierError`, never
  silently ignored (I1). The lock on disk is written once and never
  rewritten by this module.
* **Pinning is literal, not inferred.** The lock pins the files listed with
  ``--verifier-file`` plus the command's *first* token when that names a
  regular file inside the sandbox (``./grade.sh``). No other token is
  pinned: ``python grade.py`` pins nothing and ``./grade.sh answers.csv``
  pins only ``grade.sh``. Tokens that name sandbox files but are not pinned
  are logged at warning level (``rsi.verifier.unpinned``), and the CLI
  refuses a first run whose lock would pin nothing while the command names
  a sandbox file; a lock that legitimately pins nothing freezes only the
  command string. See :func:`~turing.research.rsi.contracts.compute_verifier_lock`.
* :func:`run_verifier` runs the command in its own process group with hard
  wall-clock and per-stream output caps
  (:func:`~turing.research.rsi.engine.run_capped`): the group is always killed
  when the command is done, a timed-out verifier is a failure
  (``exit_code=124``), and a verifier that exceeds the output cap is a failure
  (``exit_code=125``) before any score parsing. A stray holder of the output
  pipe cannot stall the loop past the drain grace. Output below the cap is
  captured for score parsing and truncated only in ``stdout_tail``.
* :func:`load_verifier_lock` never constructs a lock it did not read: a
  malformed file — unreadable, not an object, inconsistent, or refused by
  the lock's own validation — is a :class:`FrozenVerifierError`, never a
  usage error.

What it does not do:

* It does not decide what a passing-but-unimproved run means; that is
  :func:`~turing.research.rsi.taxonomy.classify_round`.
* It does not notice tampering *between* checks. The loop must compare the
  on-disk lock and its pinned files before and after every engine run; the
  cheat detector does the rest.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from pathlib import Path

import structlog

from turing.research.contracts import ContractViolationError, FrozenVerifierError
from turing.research.rsi.contracts import (
    VERIFIER_LOCK_FILENAME,
    VerifierLock,
    VerifierOutcome,
    VerifierSpec,
    check_verifier_lock,
    compute_verifier_lock,
    parse_score,
)
from turing.research.rsi.engine import OUTPUT_LIMIT_EXIT, run_capped

logger = structlog.get_logger(__name__)

__all__ = [
    "STDOUT_TAIL_CHARS",
    "VERIFIER_TIMEOUT_EXIT",
    "canonical_verifier_files",
    "load_verifier_lock",
    "run_verifier",
    "sandbox_file_tokens",
    "verifier_lock_path",
    "write_or_load_verifier",
]

#: How much of the verifier's stdout the round record keeps.
STDOUT_TAIL_CHARS: int = 2000
#: Exit code recorded for a verifier killed at the wall-clock cap (``timeout(1)`` convention).
VERIFIER_TIMEOUT_EXIT: int = 124


def verifier_lock_path(sandbox: Path) -> Path:
    return sandbox / VERIFIER_LOCK_FILENAME


def load_verifier_lock(sandbox: Path) -> VerifierLock | None:
    """Read ``VERIFIER.json``; ``None`` when the sandbox has no lock yet.

    Raises:
        FrozenVerifierError: the file exists but cannot be read as a lock.
    """
    path = verifier_lock_path(sandbox)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FrozenVerifierError(f"verifier lock {path} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise FrozenVerifierError(f"verifier lock {path} is not a JSON object")
    try:
        return VerifierLock.from_json(payload)
    except ContractViolationError as exc:
        raise FrozenVerifierError(f"verifier lock {path} is malformed: {exc}") from exc


def sandbox_file_tokens(command: str, sandbox: Path) -> list[str]:
    """Tokens of ``command`` that name an existing regular file inside the sandbox, in order."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    root = sandbox.resolve()
    named: list[str] = []
    for token in tokens:
        candidate = Path(token)
        if not token or candidate.is_absolute() or ".." in candidate.parts:
            continue
        full = sandbox / candidate
        if full.is_symlink() or not full.is_file():
            continue
        try:
            full.resolve().relative_to(root)
        except ValueError:
            continue
        rel = candidate.as_posix()
        if rel not in named:
            named.append(rel)
    return named


def canonical_verifier_files(
    spec: VerifierSpec, sandbox: Path, *, locked_files: set[str] | tuple[str, ...] = ()
) -> frozenset[str]:
    """Return the effective canonical pin set for a verifier specification.

    The command's first token is auto-pinned only when it names a regular file
    inside the sandbox. On resume, ``locked_files`` preserves that fact when
    the file was deleted before the comparison; the subsequent lock check can
    then report tampering instead of misreporting a pin-set mismatch.
    """
    files = {Path(rel).as_posix() for rel in spec.files}
    first = None
    try:
        tokens = shlex.split(spec.command)
    except ValueError:
        tokens = []
    if tokens:
        candidate = Path(tokens[0])
        if not candidate.is_absolute() and ".." not in candidate.parts:
            full = sandbox / candidate
            if full.is_file():
                try:
                    full.resolve().relative_to(sandbox.resolve())
                except ValueError:
                    pass
                else:
                    first = candidate.as_posix()
            elif candidate.as_posix() in {Path(rel).as_posix() for rel in locked_files}:
                first = candidate.as_posix()
    if first is not None:
        files.add(first)
    return frozenset(files)


def write_or_load_verifier(
    spec: VerifierSpec | None, sandbox: Path, *, now_ms: int | None = None
) -> tuple[VerifierLock, VerifierSpec]:
    """Lock the verifier on first run; on resume, load the lock and refuse a different command.

    Args:
        spec: the ``--verifier`` the operator passed, or ``None`` on a resume
            without the flag.
        sandbox: the sandbox directory (must exist).
        now_ms: lock creation time; defaults to the wall clock.

    Returns:
        The lock (as written or as read back) and the spec the loop should
        run — on resume that is the *locked* command, with every pinned file
        listed.

    Raises:
        ContractViolationError: first run without a ``--verifier``.
        FrozenVerifierError: a different ``--verifier`` on resume, or the
            locked files no longer match the lock.
    """
    existing = load_verifier_lock(sandbox)
    if existing is not None:
        check_verifier_lock(
            existing, sandbox, expected_command=None if spec is None else spec.command
        )
        locked_files = {Path(rel).as_posix() for rel in existing.file_sha256s}
        if (
            spec is not None
            and canonical_verifier_files(spec, sandbox, locked_files=locked_files) != locked_files
        ):
            raise FrozenVerifierError(
                "--verifier files differ from the locked verifier's pinned files in "
                f"{VERIFIER_LOCK_FILENAME}; the lock wins and a different pin set is refused"
            )
        logger.info(
            "rsi.verifier.loaded",
            path=str(verifier_lock_path(sandbox)),
            pinned_files=sorted(existing.file_sha256s),
        )
        return existing, VerifierSpec(
            command=existing.command, files=tuple(sorted(existing.file_sha256s))
        )

    if spec is None:
        raise ContractViolationError(
            f"--verifier is required on the first run (no {VERIFIER_LOCK_FILENAME} in {sandbox})"
        )
    if not sandbox.is_dir():
        raise ContractViolationError(f"sandbox {sandbox} does not exist")
    # Pinning is literal (contracts.compute_verifier_lock): ``spec.files`` plus the
    # command's first token when it is a sandbox file. Other tokens that name
    # sandbox files are NOT pinned automatically — ``./grade.sh out.csv`` must keep
    # working when the agent rewrites ``out.csv`` — so they are warned about
    # instead; the CLI refuses outright when nothing at all would be pinned.
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    lock = compute_verifier_lock(spec, sandbox, now_ms=stamp)
    path = verifier_lock_path(sandbox)
    # 'x' so two concurrent first runs cannot both claim to have written the lock.
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(lock.to_json(), indent=2, sort_keys=True) + "\n")
    unpinned = [
        rel for rel in sandbox_file_tokens(spec.command, sandbox) if rel not in lock.file_sha256s
    ]
    if unpinned or not lock.file_sha256s:
        logger.warning(
            "rsi.verifier.unpinned",
            path=str(path),
            command_sha256=lock.command_sha256,
            pinned_files=sorted(lock.file_sha256s),
            unpinned_named_files=unpinned,
            hint="only the command string and the listed files are frozen; pass --verifier-file "
            "for any script or data the command depends on",
        )
    logger.info(
        "rsi.verifier.locked",
        path=str(path),
        command_sha256=lock.command_sha256,
        pinned_files=sorted(lock.file_sha256s),
    )
    return lock, VerifierSpec(command=spec.command, files=tuple(sorted(lock.file_sha256s)))


async def run_verifier(
    spec: VerifierSpec, sandbox: Path, timeout_seconds: float
) -> VerifierOutcome:
    """Run ``spec.command`` from the sandbox with a wall-clock cap and measure it.

    The command is operator-supplied, so a shell is acceptable here (it is the
    only place in the package that uses one). The process group is killed
    when the command is done — on timeout and on a normal exit — and a
    timed-out verifier is a failure with a ``None`` score: a pass can only
    come from an exit status of zero.
    """
    if timeout_seconds <= 0:
        raise ContractViolationError("verifier timeout_seconds must be positive")
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            spec.command,
            cwd=str(sandbox),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        wall = time.monotonic() - started
        logger.error("rsi.verifier.start_failed", error=str(exc))
        return VerifierOutcome(
            exit_code=127,
            score=None,
            passed=False,
            stdout_tail=f"could not start verifier: {exc}",
            wall_seconds=wall,
        )
    capped = await run_capped(proc, timeout_seconds=timeout_seconds)
    wall = time.monotonic() - started
    stdout = capped.stdout.decode("utf-8", "replace")
    stderr = capped.stderr.decode("utf-8", "replace")
    if capped.output_limit_exceeded:
        # Overflow is checked before parsing: retained bytes can contain a
        # perfectly valid score, but they are an incomplete verifier result.
        exit_code = OUTPUT_LIMIT_EXIT
        passed = False
        score = None
    else:
        exit_code = VERIFIER_TIMEOUT_EXIT if capped.timed_out else capped.exit_code
        passed = exit_code == 0
        score = parse_score(stdout) if passed else None
    tail = stdout[-STDOUT_TAIL_CHARS:]
    if not passed and stderr:
        tail = (tail + "\n[stderr] " + stderr)[-STDOUT_TAIL_CHARS:]
    if capped.output_limit_exceeded:
        tail = (tail + "\n[verifier output limit exceeded]")[-STDOUT_TAIL_CHARS:]
    logger.info(
        "rsi.verifier.finished",
        exit_code=exit_code,
        passed=passed,
        score=score,
        timed_out=capped.timed_out,
        pipe_abandoned=capped.pipe_abandoned,
        wall_seconds=round(wall, 3),
    )
    return VerifierOutcome(
        exit_code=exit_code, score=score, passed=passed, stdout_tail=tail, wall_seconds=wall
    )
