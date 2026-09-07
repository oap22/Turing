"""The RSI workstation round loop: rounds, verifier, cheat detector, self-edit, rollback.

Successor to the ``while true`` in ``scripts/rsi-loop.sh``, keeping its
on-disk layout so the desktop panes keep working: the sandbox
(``PROBLEM.md``, ``NOTES.md``, ``STOP``, plus the new ``SCAFFOLD.md`` and
``VERIFIER.json``) and the results directory (append-only
``trajectory.json`` JSONL, ``metrics.jsonl`` the agent appends to,
``taxonomy.json``).

One round, in order:

1. honour ``STOP``; put ``SCAFFOLD.md`` back to the loop-owned version if a
   previous round moved it (event ``scaffold_drift``); re-check the verifier
   lock — pinned files *and* the lock file's own bytes (a mismatch here
   records a void ``verifier_tampered`` round and stops);
2. take the cheat detector's snapshot, build the prompt
   (``SCAFFOLD.md`` verbatim, then the bash script's round prompt, then the
   verifier contract), run the engine with the round wall-clock cap;
3. re-check the lock (same consequence), reconcile ``SCAFFOLD.md`` again,
   run the verifier and measure the score, read the agent's newest
   ``metrics.jsonl`` line for this round;
4. ask the cheat detector; classify; append the trajectory line; a fired
   detector voids the round and stops the loop;
5. every ``self_edit_every`` rounds: first judge the previous self-edit (roll
   it back if the best score since dropped by more than the noise floor;
   ``self_edit_kept`` / ``rollback`` events), then, budget permitting, ask
   the :class:`~turing.research.rsi.contracts.SelfEditStep` for a new one.

The self-edit contract this loop relies on: ``await step.propose(inputs)``
returns the SHA of a commit whose only change is ``SCAFFOLD.md``, or
``None``. The loop verifies that itself: before the step it snapshots HEAD,
the index tree and the *content digest* of every dirty or untracked path;
afterwards the returned SHA must be HEAD, ``git diff --name-only`` between
the pre-edit HEAD and it must be exactly ``SCAFFOLD.md`` (as a regular
file), and no path may have changed content — a file that was already dirty
before the step counts as changed when its bytes differ. Otherwise the edit
is discarded *surgically* (HEAD and index back to the snapshot, only the
paths the step touched restored or removed, the agent's pre-existing
uncommitted work left alone) and ``self_edit_rejected`` is logged (I3). The
lock is re-checked after every self-edit too (I1).

What this module guarantees:

* I1: the lock is checked before and after the engine and after each
  self-edit, comparing the pinned files, the on-disk ``VERIFIER.json`` parsed
  against the lock in memory *and* its bytes against the digest recorded on
  the first run; a mismatch stops the loop with exit status 3. The first run
  appends a ``verifier_locked`` event (command hash, pinned hashes, lock
  file digest) to ``trajectory.json``; a resume whose lock differs from that
  event is refused with :class:`~turing.research.contracts.FrozenVerifierError`
  — a rewritten-and-committed lock cannot hijack the next invocation.
* I2: :attr:`RoundRecord.score` is only ever the verifier's measurement;
  the agent's number goes to ``agent_reported_score`` and to the detector.
  A metrics line whose ``score`` is not a finite number cannot crash the
  round: it is recorded as ``None`` and flagged ``cheat_detected``.
* I3: see above. ``SCAFFOLD.md`` is loop-owned during rounds as well: a
  round that edits or commits it does not change the scaffold version — the
  loop restores the expected blob (committing a restore when the agent
  committed) and appends ``scaffold_drift``; the self-edit budget and the
  rollback rule therefore govern every scaffold change.
* I3 also covers what ``git status`` cannot see: git-ignored paths are
  compared by ``(size, mtime)``, and the results dir is snapshotted before
  the step and compared afterwards — a self-edit that created or modified
  anything under results (``metrics.jsonl`` included) is rejected, its
  scaffold change discarded and ``self_edit_rejected`` logged; the results
  files themselves are left alone.
* I4: the self-edit summary is built from round records, taxonomy counts,
  ``SCAFFOLD.md`` and the tail of ``NOTES.md`` only — read as regular files
  inside the sandbox, never through a symlink — with the verifier command,
  its hash, the lock file's text, every pinned file's hash and (up to 1 MiB)
  body, and the lock-file name redacted before the record is constructed
  (the record refuses them if redaction missed).
* I5: a fired detector or a tamper stops the loop; nothing here retries,
  adjusts, or resumes past it.
* I7: ``trajectory.json`` is only ever appended to, one JSON object per
  line, ``round/started/ended/exit`` first; a round that ran always gets its
  line, whatever the agent wrote to metrics/NOTES/SCAFFOLD.
* Rollback is judged across invocations: a pending self-edit is rebuilt
  from the trajectory (``self_edit`` without a later ``rollback`` /
  ``self_edit_kept`` for its SHA) on resume. The rollback itself does not
  depend on a clean index (it restores the parent's ``SCAFFOLD.md`` and
  commits only that path); if it fails, a persisted ``rollback_failed`` is a
  terminal refusal rather than permission to continue under the edit.

What it does not do:

* It does not implement the self-edit proposal (``scaffold.py`` does) or
  parse flags (``__main__`` does); it raises
  :class:`~turing.research.contracts.ContractViolationError` for usage
  errors and leaves exit-code mapping to the CLI.
* On a pass/fail-only problem (the verifier never prints ``score=``), the
  first valid verifier pass is progress and every later valid pass is
  ``no_progress``. This fact is reconstructed from ``passed`` and ``void``
  on historical rows, independently of numeric ``best_score``.
* It cannot see a scaffold rewrite that a crash left between the engine's
  exit and the post-engine reconcile of an *earlier* invocation, unless a
  ``scaffold_blob`` event exists (every loop-made scaffold change writes
  one); a sandbox from before those events adopts ``HEAD:SCAFFOLD.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import shlex
import stat
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

import structlog

from turing.research.contracts import ContractViolationError, FrozenVerifierError
from turing.research.rsi.cheat import CheatDetector, lock_file_mismatch, run_git
from turing.research.rsi.contracts import (
    VERIFIER_LOCK_FILENAME,
    VERIFIER_LOCK_MARKER,
    LoopEvent,
    RoundRecord,
    RoundSummary,
    RsiConfig,
    SelfEditInputs,
    VerifierLock,
    VerifierOutcome,
    VerifierSpec,
    check_verifier_lock,
    iter_forbidden,
    sha256_file,
)
from turing.research.rsi.scaffold import (
    DEFAULT_SCAFFOLD_TEXT,
    NOTES_FILENAME,
    NOTES_TAIL_LINES,
    SCAFFOLD_FILENAME,
)
from turing.research.rsi.taxonomy import (
    TAXONOMY_DIGEST,
    FailureCategory,
    classify_round,
    taxonomy_counts,
    write_or_check_taxonomy,
)
from turing.research.rsi.verifier import (
    load_verifier_lock,
    run_verifier,
    verifier_lock_path,
    write_or_load_verifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path

    from turing.research.rsi.contracts import Engine, SelfEditStep

logger = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_SCAFFOLD",
    "EXIT_CHEAT",
    "EXIT_ENGINE_FAILURE",
    "EXIT_OK",
    "MAX_CONSECUTIVE_ENGINE_FAILURES",
    "METRICS_FILENAME",
    "NOTES_TAIL_LINES",
    "PROBLEM_FILENAME",
    "SCAFFOLD_FILENAME",
    "STOP_FILENAME",
    "TRAJECTORY_FILENAME",
    "LoopOutcome",
    "RsiLoop",
    "StopReason",
    "TrajectoryState",
    "append_jsonl",
    "build_round_prompt",
    "describe_plan",
    "read_trajectory",
    "redact",
]

PROBLEM_FILENAME: str = "PROBLEM.md"
STOP_FILENAME: str = "STOP"
TRAJECTORY_FILENAME: str = "trajectory.json"
METRICS_FILENAME: str = "metrics.jsonl"
MAX_CONSECUTIVE_ENGINE_FAILURES: int = 3
EXIT_OK: int = 0
EXIT_ENGINE_FAILURE: int = 1
EXIT_CHEAT: int = 3
_REDACTED: str = "<redacted>"
#: Largest pre-existing dirty file whose bytes are kept for restoring after a rejected self-edit.
_RESTORE_MAX_BYTES: int = 64 * 1024 * 1024
_REGULAR_FILE_MODE: str = "100644"
#: Largest pinned verifier file whose body becomes a redaction needle for the self-edit summary.
_NEEDLE_MAX_BYTES: int = 1024 * 1024
#: Shortest pinned-file body worth redacting; anything shorter would mangle ordinary text.
_NEEDLE_MIN_CHARS: int = 8
#: Minimum evidence window used when an older self-edit event has no persisted
#: schedule and the current invocation has proposals disabled.
_LEGACY_PENDING_WINDOW: int = 1
#: Events that carry the loop-owned scaffold blob id.
_SCAFFOLD_EVENTS: frozenset[str] = frozenset(
    {"scaffold_seeded", "self_edit", "rollback", "scaffold_drift"}
)

#: Alias of :data:`turing.research.rsi.scaffold.DEFAULT_SCAFFOLD_TEXT`.
DEFAULT_SCAFFOLD: str = DEFAULT_SCAFFOLD_TEXT


class StopReason(str, Enum):  # noqa: UP042
    ROUNDS_EXHAUSTED = "rounds_exhausted"
    STOP_FILE = "stop_file"
    VERIFIER_TAMPERED = "verifier_tampered"
    CHEAT_DETECTED = "cheat_detected"
    ENGINE_FAILURES = "engine_failures"
    ROLLBACK_FAILED = "rollback_failed"


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """How one invocation of :meth:`RsiLoop.run` ended."""

    rounds_run: int
    stop_reason: StopReason
    best_score: float | None
    self_edits: int
    rollbacks: int
    #: Number of rounds whose engine timed out or exited non-zero in this invocation.
    #: Historical trajectory rows are intentionally excluded from this count.
    engine_failures: int = 0

    @property
    def exit_code(self) -> int:
        if self.stop_reason in (StopReason.VERIFIER_TAMPERED, StopReason.CHEAT_DETECTED):
            return EXIT_CHEAT
        if self.stop_reason is StopReason.ROLLBACK_FAILED:
            # Continuing under an edit the loop failed to undo is a terminal
            # invocation failure, even when the round's engine succeeded.
            return EXIT_ENGINE_FAILURE
        if self.rounds_run > 0 and self.engine_failures >= self.rounds_run:
            return EXIT_ENGINE_FAILURE
        return EXIT_OK


@dataclass(frozen=True, slots=True)
class TrajectoryState:
    """What an existing ``trajectory.json`` says about where to resume."""

    records: tuple[RoundRecord, ...]
    events: tuple[LoopEvent, ...]
    next_round: int

    @property
    def best_score(self) -> float | None:
        scores = [r.score for r in self.records if not r.void and r.passed and r.score is not None]
        return max(scores) if scores else None

    @property
    def previous_score(self) -> float | None:
        counted = [r for r in self.records if not r.void]
        if not counted:
            return None
        last = counted[-1]
        return last.score if last.passed else None

    @property
    def prior_pass(self) -> bool:
        """Whether any historical row records a valid verifier pass.

        A pass remains evidence even when its row also carries
        ``engine_error`` or ``no_metrics``. Void rows are excluded because a
        fired detector invalidates the verifier result.
        """
        return any(record.passed and not record.void for record in self.records)


# --------------------------------------------------------------------------- #
# Trajectory helpers
# --------------------------------------------------------------------------- #


def append_jsonl(path: Path, line: str) -> None:
    """Append one line; never truncates (I7)."""
    if "\n" in line.rstrip("\n"):
        raise ContractViolationError("a JSONL line may not contain a newline")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def read_trajectory(path: Path) -> TrajectoryState:
    """Parse the JSONL; event lines are kept but do not count toward the next round."""
    if not path.exists():
        return TrajectoryState(records=(), events=(), next_round=1)
    records: list[RoundRecord] = []
    events: list[LoopEvent] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError as exc:
                raise ContractViolationError(
                    f"{path}:{lineno} is not JSON; refusing to resume on a corrupt trajectory"
                ) from exc
            if not isinstance(payload, dict):
                raise ContractViolationError(f"{path}:{lineno} is not a JSON object")
            if "event" in payload:
                events.append(LoopEvent.from_json(payload))
            else:
                records.append(RoundRecord.from_json(payload))
    return TrajectoryState(
        records=tuple(records), events=tuple(events), next_round=len(records) + 1
    )


def redact(text: str, needles: Sequence[str]) -> str:
    """Replace every non-empty needle (longest first) so the self-edit summary carries none."""
    for needle in sorted({n for n in needles if n}, key=len, reverse=True):
        text = text.replace(needle, _REDACTED)
    return text


def build_round_prompt(
    *, round_no: int, results_dir: Path, scaffold_text: str, verifier_command: str
) -> str:
    """``SCAFFOLD.md`` verbatim, then the bash script's prompt, then the verifier contract."""
    body = (
        f"You are round {round_no} of a continuous research loop. Your sandbox is this\n"
        f"working directory; you may write only here and in {results_dir}. Read\n"
        "PROBLEM.md (the goal) and NOTES.md (state from earlier rounds). Do ONE\n"
        "focused iteration of research, building, or experimentation toward the\n"
        "problem. Before you finish: (1) append exactly one JSON line to\n"
        f'{results_dir}/metrics.jsonl of the form {{"step": {round_no}, "ts": <epoch seconds>, ...}}\n'
        "including every numeric measurement you produced this round; (2) save any\n"
        f"plots or figures as .png or .svg files in {results_dir}; (3) update NOTES.md with\n"
        "what you found and what the next round should try; (4) commit your work with\n"
        "git. If the problem is solved, or you are convinced no further progress is\n"
        "possible, create an empty file named STOP in the working directory and\n"
        "record why in NOTES.md.\n"
    )
    verifier = (
        "\nGrading: after this round the loop runs the frozen verifier command\n"
        f"    {verifier_command}\n"
        "from the sandbox. Its exit status is pass/fail and the last stdout line of the\n"
        "form score=<number> is the score (higher is better). You may run it yourself to\n"
        "check your work, but the LOOP measures the score, not you: a score you write to\n"
        "metrics.jsonl is informational only, and one that disagrees with the measured\n"
        f"score voids the round. Do not modify the verifier, its inputs, or {VERIFIER_LOCK_FILENAME};\n"
        "any change to them is detected and stops the loop.\n"
        f"{SCAFFOLD_FILENAME} is owned by the loop: do not edit or commit it. The loop\n"
        "revises it between rounds; a changed copy is restored and the change logged.\n"
    )
    scaffold = scaffold_text.rstrip("\n")
    head = f"{scaffold}\n\n" if scaffold else ""
    return head + body + verifier


# --------------------------------------------------------------------------- #
# Working-tree snapshots (for I3)
# --------------------------------------------------------------------------- #


def _digest_or_none(path: Path, *, cheap: bool = False) -> str | None:
    """Content digest for a regular file, ``link:<target>``/``dir``/``special`` otherwise, ``None`` if absent.

    With ``cheap`` a regular file is signed by ``(size, mtime_ns)`` instead of
    its content — used for git-ignored files, which can be large build
    artefacts but are still paths the self-edit step may not touch (I3).
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return f"link:{os.readlink(path)}"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if not stat.S_ISREG(st.st_mode):
        return f"special:{st.st_mode:o}"
    if cheap:
        return f"stat:{st.st_size}:{st.st_mtime_ns}"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_status_z(stdout: str) -> dict[str, str]:
    """``{path: XY}`` from ``git status --porcelain -z``; renames list both paths."""
    entries = stdout.split("\0")
    out: dict[str, str] = {}
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        out[path] = status
        if status[0] in "RC" and i < len(entries):
            out[entries[i]] = status
            i += 1
    return out


@dataclass(frozen=True, slots=True)
class _TreeState:
    """HEAD, index tree and the digest (plus kept bytes) of every dirty path before a self-edit."""

    head: str
    index_tree: str | None
    dirty: dict[str, str | None]
    kept: dict[str, bytes]


@dataclass(slots=True)
class _PendingEdit:
    sha: str
    committed_after_round: int
    judgment_window: int
    judgment_window_source: str
    best_before: float | None
    passed_before: bool
    prior_scores: tuple[float, ...]
    rounds_after: list[RoundRecord] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


class RsiLoop:
    """Drives rounds for one ``(slug, sandbox, results)`` triple. Construct, then ``await run()``."""

    def __init__(
        self,
        config: RsiConfig,
        *,
        engine: Engine,
        verifier: VerifierSpec | None,
        self_edit: SelfEditStep | None,
        cheat: CheatDetector | None = None,
        clock: Callable[[], float] = time.time,
        problem: str | None = None,
    ) -> None:
        self.config = config
        self.engine = engine
        self.verifier_spec_arg = verifier
        self.self_edit = self_edit
        self.cheat = cheat if cheat is not None else CheatDetector()
        self.clock = clock
        self.problem = problem
        self.sandbox = config.sandbox_dir
        self.results = config.results_dir
        self.trajectory_path = self.results / TRAJECTORY_FILENAME
        self.lock: VerifierLock | None = None
        self.spec: VerifierSpec | None = None
        #: sha256 of ``VERIFIER.json``'s bytes as first written / as recorded on first run.
        self.lock_sha256: str | None = None
        #: The loop-owned scaffold: blob id of ``SCAFFOLD.md`` and the commit that set it.
        self.scaffold_blob: str | None = None
        self.scaffold_sha: str | None = None
        self._state: TrajectoryState | None = None
        self._pending: _PendingEdit | None = None

    # ----------------------------------------------------------------- setup

    async def prepare(self) -> TrajectoryState:
        """Create dirs, git repo, PROBLEM/NOTES/SCAFFOLD, taxonomy and verifier lock; read the trajectory."""
        self.sandbox.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)
        write_or_check_taxonomy(self.results)

        if not (self.sandbox / ".git").is_dir():
            await run_git(self.sandbox, "init", "-q", check=True)

        problem_path = self.sandbox / PROBLEM_FILENAME
        if not problem_path.exists():
            if not self.problem:
                raise ContractViolationError(
                    f"--problem is required on the first run (no {PROBLEM_FILENAME} in {self.sandbox})"
                )
            problem_path.write_text(self.problem.rstrip("\n") + "\n", encoding="utf-8")
        (self.sandbox / NOTES_FILENAME).touch()
        scaffold_path = self.sandbox / SCAFFOLD_FILENAME
        if not scaffold_path.exists():
            scaffold_path.write_text(DEFAULT_SCAFFOLD, encoding="utf-8")

        first_lock = load_verifier_lock(self.sandbox) is None
        lock, spec = write_or_load_verifier(
            self.verifier_spec_arg, self.sandbox, now_ms=int(self.clock() * 1000)
        )
        self.lock, self.spec = lock, spec
        self.lock_sha256 = sha256_file(verifier_lock_path(self.sandbox))

        # Track the lock (and PROBLEM.md) so `git status` sees any later change to them;
        # seed the scaffold only when HEAD has none — a dirty tracked SCAFFOLD.md is
        # drift to be reconciled, never something to commit as the loop's own.
        if await self._head_scaffold_blob() is None:
            await self._commit_if_dirty(
                [VERIFIER_LOCK_FILENAME, SCAFFOLD_FILENAME, PROBLEM_FILENAME],
                "rsi: lock verifier and seed scaffold",
            )
        else:
            await self._commit_if_dirty(
                [VERIFIER_LOCK_FILENAME, PROBLEM_FILENAME], "rsi: lock verifier"
            )

        state = read_trajectory(self.trajectory_path)
        self._state = state
        startup_round = max(0, state.next_round - 1)
        self._check_lock_provenance(state, first_lock=first_lock, round_no=startup_round)
        await self._adopt_scaffold(state, startup_round)
        self._pending = await self._rebuild_pending(state)
        # Validate pending-edit provenance before restoring an event-owned blob;
        # a missing commit must refuse the resume rather than reconstructing an
        # unjudged scaffold and continuing under it.
        await self._reconcile_scaffold(startup_round, when="startup")
        logger.info(
            "rsi.loop.prepared",
            sandbox=str(self.sandbox),
            results=str(self.results),
            next_round=state.next_round,
            best_score=state.best_score,
            prior_pass=state.prior_pass,
            scaffold_sha=(self.scaffold_sha or "")[:12],
            pending_self_edit=None if self._pending is None else self._pending.sha[:12],
        )
        return state

    async def _commit_if_dirty(self, paths: list[str], message: str) -> str | None:
        status = await run_git(self.sandbox, "status", "--porcelain", "--", *paths, check=True)
        if not status.stdout.strip():
            return None
        await run_git(self.sandbox, "add", "--", *paths, check=True)
        await run_git(
            self.sandbox,
            "commit",
            "-q",
            "--no-verify",
            "--only",
            "-m",
            message,
            "--",
            *paths,
            check=True,
        )
        head = await run_git(self.sandbox, "rev-parse", "HEAD", check=True)
        return head.stdout.strip()

    def _check_lock_provenance(
        self, state: TrajectoryState, *, first_lock: bool, round_no: int
    ) -> None:
        """Bind the lock to the results dir: write ``verifier_locked`` once, refuse a drifted lock later."""
        assert self.lock is not None and self.lock_sha256 is not None
        expected = {
            "command_sha256": self.lock.command_sha256,
            "file_sha256s": dict(sorted(self.lock.file_sha256s.items())),
            "lock_sha256": self.lock_sha256,
        }
        recorded = [e for e in state.events if e.event == "verifier_locked"]
        if first_lock or not recorded:
            if not first_lock:
                logger.warning(
                    "rsi.verifier.provenance_adopted",
                    hint="VERIFIER.json predates provenance events; binding it from now on",
                )
            self._append_event("verifier_locked", round_no, expected)
            return
        last = recorded[-1]
        for key, want in expected.items():
            have = last.details.get(key)
            if key == "file_sha256s":
                have = dict(sorted(dict(have or {}).items())) if isinstance(have, dict) else have
            if have != want:
                raise FrozenVerifierError(
                    f"{VERIFIER_LOCK_FILENAME} in the sandbox differs from the lock recorded in "
                    f"{self.trajectory_path} ({key} changed); the sandbox's lock file was "
                    "rewritten since the first run and a resume under it is refused"
                )

    # --------------------------------------------------------- scaffold ownership

    async def _head_scaffold_blob(self) -> str | None:
        got = await run_git(
            self.sandbox, "rev-parse", "--verify", "-q", f"HEAD:{SCAFFOLD_FILENAME}"
        )
        return got.stdout.strip() or None if got.ok else None

    async def _worktree_scaffold_blob(self) -> str | None:
        path = self.sandbox / SCAFFOLD_FILENAME
        if path.is_symlink() or not path.is_file():
            return None
        got = await run_git(self.sandbox, "hash-object", "--", SCAFFOLD_FILENAME)
        return got.stdout.strip() or None if got.ok else None

    async def _blob_exists(self, blob: str) -> bool:
        return (await run_git(self.sandbox, "cat-file", "-e", f"{blob}^{{blob}}")).ok

    async def _adopt_scaffold(self, state: TrajectoryState, round_no: int) -> None:
        """Resolve the loop-owned scaffold blob: newest event carrying one, else HEAD's, then seed."""
        recorded = [
            str(e.details["scaffold_blob"])
            for e in state.events
            if e.event in _SCAFFOLD_EVENTS and e.details.get("scaffold_blob")
        ]
        head_blob = await self._head_scaffold_blob()
        if recorded:
            blob = recorded[-1]
            if not await self._blob_exists(blob):
                raise ContractViolationError(
                    f"the scaffold version recorded in {self.trajectory_path} ({blob[:12]}) is "
                    "not in the sandbox repository any more; inspect the sandbox before resuming"
                )
            self.scaffold_blob = blob
        else:
            if head_blob is None:
                raise ContractViolationError(
                    f"{SCAFFOLD_FILENAME} is not committed in the sandbox repository"
                )
            self.scaffold_blob = head_blob
        recorded_shas = [
            str(e.details["scaffold_sha"])
            for e in state.events
            if e.event in _SCAFFOLD_EVENTS and e.details.get("scaffold_sha")
        ]
        self.scaffold_sha = (
            recorded_shas[-1] if recorded and recorded_shas else await self._last_scaffold_commit()
        )
        if not recorded:
            self._append_event(
                "scaffold_seeded",
                round_no,
                {"scaffold_sha": self.scaffold_sha, "scaffold_blob": self.scaffold_blob},
            )

    async def _last_scaffold_commit(self) -> str | None:
        head = await run_git(self.sandbox, "log", "-n1", "--format=%H", "--", SCAFFOLD_FILENAME)
        return head.stdout.strip() or None

    async def _reconcile_scaffold(self, round_no: int, *, when: str) -> bool:
        """Put ``SCAFFOLD.md`` (working tree and HEAD) back to the loop-owned blob; log drift.

        Returns ``True`` when something had to be restored.
        """
        assert self.scaffold_blob is not None
        head_blob = await self._head_scaffold_blob()
        wt_blob = await self._worktree_scaffold_blob()
        if head_blob == self.scaffold_blob and wt_blob == self.scaffold_blob:
            return False
        path = self.sandbox / SCAFFOLD_FILENAME
        if path.is_symlink() or (path.exists() and not path.is_file()):
            _remove_path(path)
        await run_git(
            self.sandbox,
            "update-index",
            "--add",
            "--cacheinfo",
            f"{_REGULAR_FILE_MODE},{self.scaffold_blob},{SCAFFOLD_FILENAME}",
            check=True,
        )
        await run_git(self.sandbox, "checkout-index", "-f", "--", SCAFFOLD_FILENAME, check=True)
        restored_sha: str | None = None
        if head_blob != self.scaffold_blob:
            await run_git(
                self.sandbox,
                "commit",
                "-q",
                "--no-verify",
                "--only",
                "-m",
                f"rsi: restore scaffold after round {round_no}",
                "--",
                SCAFFOLD_FILENAME,
                check=True,
            )
            restored_sha = (
                await run_git(self.sandbox, "rev-parse", "HEAD", check=True)
            ).stdout.strip()
        logger.warning(
            "rsi.scaffold.drift",
            round=round_no,
            when=when,
            head_moved=head_blob != self.scaffold_blob,
            worktree_moved=wt_blob != self.scaffold_blob,
            restored_sha=(restored_sha or "")[:12],
        )
        self._append_event(
            "scaffold_drift",
            round_no,
            {
                "when": when,
                "head_moved": head_blob != self.scaffold_blob,
                "worktree_moved": wt_blob != self.scaffold_blob,
                "restored_sha": restored_sha,
                "scaffold_sha": self.scaffold_sha,
                "scaffold_blob": self.scaffold_blob,
            },
        )
        return True

    # ------------------------------------------------------------------- run

    async def run(self) -> LoopOutcome:
        state = self._state if self._state is not None else await self.prepare()
        assert self.lock is not None and self.spec is not None  # set by prepare()
        lock, spec = self.lock, self.spec
        cfg = self.config

        records: list[RoundRecord] = list(state.records)
        best_score = state.best_score
        previous_score = state.previous_score
        prior_pass = state.prior_pass
        start = state.next_round
        round_no = start
        rounds_run = 0
        engine_failures = 0
        consecutive_failures = 0
        self_edits = 0
        rollbacks = 0
        pending: _PendingEdit | None = self._pending
        stop = StopReason.ROUNDS_EXHAUSTED

        while True:
            if cfg.rounds and round_no > start + cfg.rounds - 1:
                stop = StopReason.ROUNDS_EXHAUSTED
                break
            if (self.sandbox / STOP_FILENAME).exists():
                logger.info("rsi.loop.stop_file", sandbox=str(self.sandbox), round=round_no)
                stop = StopReason.STOP_FILE
                break

            # A prior invocation may have recorded all evidence for a pending
            # edit and then exited before it could append the judgment event.
            # Resolve that historical window before starting another engine
            # round. The round carrying the last piece of evidence owns the
            # event's round number, including when this is a restart.
            if pending is not None and len(pending.rounds_after) >= pending.judgment_window:
                judged = await self._judge_pending(pending, pending.rounds_after[-1].round)
                if judged == "failed":
                    stop = StopReason.ROLLBACK_FAILED
                    break
                if judged == "rolled_back":
                    rollbacks += 1
                pending = None

            logger.info("rsi.round.start", round=round_no, slug=cfg.slug)
            await self._reconcile_scaffold(round_no, when="before_round")
            scaffold_sha = self.scaffold_sha
            started = int(self.clock())

            # I1: before the engine.
            tamper = self._lock_mismatch(lock)
            if tamper is not None:
                record = self._void_tamper_record(round_no, started, scaffold_sha, tamper)
                records.append(record)
                append_jsonl(self.trajectory_path, record.to_json_line())
                stop = StopReason.VERIFIER_TAMPERED
                break

            snapshot = self.cheat.snapshot_before(self.sandbox, self.results)
            metrics_before = self._metrics_line_count()
            prompt = build_round_prompt(
                round_no=round_no,
                results_dir=self.results,
                scaffold_text=self._read_text(self.sandbox / SCAFFOLD_FILENAME),
                verifier_command=lock.command,
            )
            result = await self.engine.run(
                prompt, cwd=self.sandbox, timeout_seconds=cfg.round_timeout_seconds
            )
            ended = max(int(self.clock()), started)

            # I1: after the engine.
            tamper = self._lock_mismatch(lock)
            if tamper is not None:
                record = self._void_tamper_record(
                    round_no, started, scaffold_sha, tamper, ended=ended, exit_code=result.exit_code
                )
                records.append(record)
                append_jsonl(self.trajectory_path, record.to_json_line())
                stop = StopReason.VERIFIER_TAMPERED
                break

            # The agent must not be able to add, replace, or delete the
            # supervisor-owned history before reconciliation appends its own
            # scaffold event.  Metrics remain agent-owned and are still read
            # below as normal.
            trajectory_tamper = self.cheat.trajectory_change(
                self.results, snapshot, trajectory=self.trajectory_path
            )
            if trajectory_tamper is not None:
                had_metrics, agent_score, report_problem = self._agent_metrics(
                    round_no, metrics_before
                )
                verdict = await self.cheat.verdict_after(
                    sandbox=self.sandbox,
                    results=self.results,
                    snapshot=snapshot,
                    lock=lock,
                    measured=None,
                    agent_reported_score=agent_score,
                    lock_sha256=self.lock_sha256,
                    self_report_problem=report_problem,
                )
                evidence = self.cheat.quarantine_trajectory(
                    self.results, snapshot, round_no, trajectory=self.trajectory_path
                )
                self._append_event(
                    "trajectory_tampered",
                    round_no,
                    {"detail": trajectory_tamper, "evidence": evidence, "after": "engine"},
                )
                categories = classify_round(
                    engine_exit=result.exit_code,
                    timed_out=result.timed_out,
                    verifier=None,
                    previous_score=previous_score,
                    best_score=best_score,
                    had_metrics_line=had_metrics,
                    cheat=verdict,
                    prior_pass=prior_pass,
                )
                record = RoundRecord(
                    round=round_no,
                    started=started,
                    ended=max(ended, int(self.clock()), started),
                    exit=result.exit_code,
                    categories=categories,
                    scaffold_sha=scaffold_sha,
                    void=verdict.fired,
                    agent_reported_score=agent_score,
                )
                records.append(record)
                append_jsonl(self.trajectory_path, record.to_json_line())
                rounds_run += 1
                stop = (
                    StopReason.VERIFIER_TAMPERED
                    if FailureCategory.VERIFIER_TAMPERED in verdict.categories
                    else StopReason.CHEAT_DETECTED
                )
                break

            await self._reconcile_scaffold(round_no, when="after_engine")
            # ``_reconcile_scaffold`` may append a supervisor event.  Make that
            # expected append part of the baseline before the detector checks
            # the agent's result-directory writes.
            snapshot = self.cheat.refresh_trajectory(snapshot, trajectory=self.trajectory_path)

            outcome: VerifierOutcome | None = await run_verifier(
                spec, self.sandbox, cfg.verifier_timeout_seconds
            )
            had_metrics, agent_score, report_problem = self._agent_metrics(round_no, metrics_before)
            verdict = await self.cheat.verdict_after(
                sandbox=self.sandbox,
                results=self.results,
                snapshot=snapshot,
                lock=lock,
                measured=outcome,
                agent_reported_score=agent_score,
                lock_sha256=self.lock_sha256,
                self_report_problem=report_problem,
            )
            # ``verdict_after`` observes supervisor-owned trajectory tampering,
            # but its verdict must not leave forged history in place.  Restore
            # the snapshot taken after the loop's own scaffold reconciliation
            # before appending the void record, so resume can only trust the
            # quarantined pre-round history.
            trajectory_tamper = self.cheat.trajectory_change(
                self.results, snapshot, trajectory=self.trajectory_path
            )
            if trajectory_tamper is not None:
                evidence = self.cheat.quarantine_trajectory(
                    self.results, snapshot, round_no, trajectory=self.trajectory_path
                )
                self._append_event(
                    "trajectory_tampered",
                    round_no,
                    {"detail": trajectory_tamper, "evidence": evidence, "after": "verifier"},
                )
            categories = classify_round(
                engine_exit=result.exit_code,
                timed_out=result.timed_out,
                verifier=outcome,
                previous_score=previous_score,
                best_score=best_score,
                had_metrics_line=had_metrics,
                cheat=verdict,
                prior_pass=prior_pass,
            )
            measured_score = outcome.score if outcome is not None and outcome.passed else None
            record = RoundRecord(
                round=round_no,
                started=started,
                ended=max(ended, int(self.clock()), started),
                exit=result.exit_code,
                score=measured_score,
                passed=outcome.passed if outcome is not None else False,
                categories=categories,
                scaffold_sha=scaffold_sha,
                void=verdict.fired,
                agent_reported_score=agent_score,
                verifier_wall_seconds=outcome.wall_seconds if outcome is not None else None,
            )
            records.append(record)
            append_jsonl(self.trajectory_path, record.to_json_line())
            logger.info(
                "rsi.round.recorded",
                round=round_no,
                exit_code=result.exit_code,
                score=measured_score,
                passed=record.passed,
                categories=sorted(c.value for c in categories),
                void=record.void,
            )
            rounds_run += 1

            if verdict.fired:
                # I5: a cheat stops the loop; the operator restarts by hand.
                stop = (
                    StopReason.VERIFIER_TAMPERED
                    if FailureCategory.VERIFIER_TAMPERED in verdict.categories
                    else StopReason.CHEAT_DETECTED
                )
                logger.error("rsi.loop.stopped_on_cheat", round=round_no, reasons=verdict.reasons)
                break

            # Score bookkeeping (I2: only the measured score ever lands here).
            previous_score = measured_score
            if measured_score is not None and (best_score is None or measured_score > best_score):
                best_score = measured_score
            if record.passed and not record.void:
                prior_pass = True
            if pending is not None:
                pending.rounds_after.append(record)

            # A terminal engine failure still produced a verifier observation.
            # Settle a pending self-edit whose evidence window closes on this
            # round before the consecutive-failure guard stops the invocation;
            # otherwise a restart would run one more round under an edit that
            # was already due for judgment.
            if pending is not None and len(pending.rounds_after) >= pending.judgment_window:
                judged = await self._judge_pending(pending, round_no)
                if judged == "failed":
                    stop = StopReason.ROLLBACK_FAILED
                    break
                if judged == "rolled_back":
                    rollbacks += 1
                pending = None

            if result.timed_out or result.exit_code != 0:
                engine_failures += 1
                consecutive_failures += 1
                logger.warning(
                    "rsi.round.engine_failed",
                    round=round_no,
                    exit_code=result.exit_code,
                    timed_out=result.timed_out,
                    consecutive=consecutive_failures,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_ENGINE_FAILURES:
                    logger.error(
                        "rsi.loop.aborting",
                        consecutive_failures=consecutive_failures,
                        hint="is the claude CLI installed?",
                    )
                    stop = StopReason.ENGINE_FAILURES
                    round_no += 1
                    break
            else:
                consecutive_failures = 0

            # Judge a pending edit from its accumulated evidence, which can span
            # invocations. Proposal timing remains on the existing invocation
            # schedule so a restart does not create an extra edit as a side effect
            # of catching up on a completed judgment window.
            if pending is not None and len(pending.rounds_after) >= pending.judgment_window:
                judged = await self._judge_pending(pending, round_no)
                if judged == "failed":
                    stop = StopReason.ROLLBACK_FAILED
                    break
                if judged == "rolled_back":
                    rollbacks += 1
                pending = None

            if (
                cfg.self_edit_every
                and rounds_run % cfg.self_edit_every == 0
                and self.self_edit is not None
                and self_edits < cfg.self_edit_budget
                and pending is None
            ):
                try:
                    sha = await self._self_edit(records, round_no, best_score, lock)
                except FrozenVerifierError as exc:
                    # I1/I5: the step (or its own guard) touched the verifier; stop, restore nothing.
                    logger.error("rsi.self_edit.tampered", round=round_no, detail=str(exc))
                    self._append_event(
                        "verifier_tampered",
                        round_no,
                        {"detail": str(exc), "after": "self_edit"},
                    )
                    stop = StopReason.VERIFIER_TAMPERED
                    break
                if sha is not None:
                    self_edits += 1
                    counted = [r for r in records if not r.void]
                    pending = _PendingEdit(
                        sha=sha,
                        committed_after_round=round_no,
                        judgment_window=cfg.self_edit_every,
                        judgment_window_source="recorded",
                        best_before=best_score,
                        passed_before=bool(counted and counted[-1].passed),
                        prior_scores=tuple(
                            r.score for r in counted if r.passed and r.score is not None
                        ),
                    )
                tamper = self._lock_mismatch(lock)
                if tamper is not None:
                    # I1 after a self-edit: the round already ran, so log an event and stop.
                    self._append_event(
                        "verifier_tampered", round_no, {"detail": tamper, "after": "self_edit"}
                    )
                    stop = StopReason.VERIFIER_TAMPERED
                    break

            round_no += 1

        self._pending = pending
        logger.info(
            "rsi.loop.done",
            rounds_run=rounds_run,
            stop_reason=stop.value,
            best_score=best_score,
            self_edits=self_edits,
            rollbacks=rollbacks,
            results=str(self.results),
        )
        return LoopOutcome(
            rounds_run=rounds_run,
            stop_reason=stop,
            best_score=best_score,
            self_edits=self_edits,
            rollbacks=rollbacks,
            engine_failures=engine_failures,
        )

    # ------------------------------------------------------------- helpers

    def _lock_mismatch(self, lock: VerifierLock) -> str | None:
        """Pinned files, the on-disk lock parsed against ``lock``, and its bytes; ``None`` if intact."""
        try:
            check_verifier_lock(lock, self.sandbox)
        except FrozenVerifierError as exc:
            logger.error("rsi.verifier.tampered", detail=str(exc))
            return str(exc)
        mismatch = lock_file_mismatch(self.sandbox, lock, expected_sha256=self.lock_sha256)
        if mismatch is not None:
            logger.error("rsi.verifier.tampered", detail=mismatch)
        return mismatch

    def _void_tamper_record(
        self,
        round_no: int,
        started: int,
        scaffold_sha: str | None,
        detail: str,
        *,
        ended: int | None = None,
        exit_code: int = -1,
    ) -> RoundRecord:
        del detail  # logged by _lock_mismatch; the record carries the category
        return RoundRecord(
            round=round_no,
            started=started,
            ended=max(started, int(self.clock()) if ended is None else ended),
            exit=exit_code,
            categories=frozenset({FailureCategory.VERIFIER_TAMPERED}),
            scaffold_sha=scaffold_sha,
            void=True,
        )

    def _append_event(self, event: str, round_no: int, details: dict[str, Any]) -> None:
        line = LoopEvent(event=event, round=round_no, ts=int(self.clock()), details=details)
        append_jsonl(self.trajectory_path, line.to_json_line())

    @staticmethod
    def _read_text(path: Path) -> str:
        """Agent-written text, decoded leniently: bytes the agent wrote can never crash a round."""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError):
            return ""
        except OSError as exc:
            logger.warning("rsi.loop.unreadable", path=str(path), error=str(exc))
            return ""

    def _metrics_line_count(self) -> int:
        path = self.results / METRICS_FILENAME
        if not path.exists():
            return 0
        with path.open("rb") as fh:
            return sum(1 for _ in fh)

    def _agent_metrics(
        self, round_no: int, skip_lines: int
    ) -> tuple[bool, float | None, str | None]:
        """``(had_metrics_line, agent_reported_score, problem)`` from lines appended this round.

        ``problem`` describes a ``score`` that is a number but not a finite
        one (``1e999``, ``NaN``): it is reported as ``None`` and the detector
        flags it. Never raises for agent-written content.
        """
        path = self.results / METRICS_FILENAME
        if not path.exists():
            return False, None, None
        found = False
        reported: float | None = None
        problem: str | None = None
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for index, raw in enumerate(fh):
                    if index < skip_lines or not raw.strip():
                        continue
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict) or payload.get("step") != round_no:
                        continue
                    found = True
                    score = payload.get("score")
                    if isinstance(score, bool) or not isinstance(score, (int, float)):
                        reported, problem = None, None
                    elif not math.isfinite(float(score)):
                        reported = None
                        problem = f"metrics line for step {round_no} reports score {score!r}"
                    else:
                        reported, problem = float(score), None
        except OSError as exc:
            logger.warning("rsi.loop.metrics_unreadable", path=str(path), error=str(exc))
            return False, None, None
        return found, reported, problem

    # ---------------------------------------------------------- self-edit

    async def _self_edit(
        self,
        records: list[RoundRecord],
        round_no: int,
        best_score: float | None,
        lock: VerifierLock,
    ) -> str | None:
        assert self.self_edit is not None  # caller checked
        forbidden = tuple(self._verifier_needles(lock))
        needles = (*forbidden, VERIFIER_LOCK_MARKER)
        # I4: SCAFFOLD.md and NOTES.md are read only as regular files inside the
        # sandbox, never through a symlink the round may have planted.
        scaffold_text = redact(self._read_sandbox_file(SCAFFOLD_FILENAME), needles)
        notes_lines = self._read_sandbox_file(NOTES_FILENAME).splitlines()
        notes_tail = redact("\n".join(notes_lines[-NOTES_TAIL_LINES:]), needles)
        inputs = SelfEditInputs(
            round_index=round_no,
            best_score=best_score,
            rounds=tuple(RoundSummary.from_record(r) for r in records),
            taxonomy_counts=taxonomy_counts(records),
            scaffold_text=scaffold_text,
            notes_tail=notes_tail,
            forbidden=forbidden,
        )
        before = await self._tree_state()
        results_before = self.cheat.snapshot_before(self.sandbox, self.results)
        logger.info("rsi.self_edit.start", round=round_no, head=before.head[:12])
        try:
            sha = await self.self_edit.propose(inputs)
        except asyncio.CancelledError:
            await self._cleanup_cancelled_self_edit(before, results_before, round_no, lock)
            raise
        except Exception:
            # A failing proposal can still have written supervisor-owned
            # history before raising (for example, while rejecting a guarded
            # verifier edit). Restore that history before the failure escapes.
            if (
                self.cheat.trajectory_change(
                    self.results, results_before, trajectory=self.trajectory_path
                )
                is not None
            ):
                self.cheat.quarantine_trajectory(
                    self.results, results_before, round_no, trajectory=self.trajectory_path
                )
            raise
        trajectory_tamper = self.cheat.trajectory_change(
            self.results, results_before, trajectory=self.trajectory_path
        )
        trajectory_reason: str | None = None
        if trajectory_tamper is not None:
            evidence = self.cheat.quarantine_trajectory(
                self.results, results_before, round_no, trajectory=self.trajectory_path
            )
            trajectory_reason = (
                f"{trajectory_tamper}; quarantined evidence={evidence or '<unavailable>'}"
            )
        # I3: the results dir is outside the sandbox's git status, so it is
        # compared separately; a self-edit that wrote there loses its edit.
        results_changed = self.cheat.results_changes(self.results, results_before)
        try:
            kept, rejection = await self._verify_self_edit(
                before,
                sha,
                lock,
                results_changed=results_changed,
                trajectory_changed=trajectory_reason,
            )
        except asyncio.CancelledError:
            await self._cleanup_cancelled_self_edit(before, results_before, round_no, lock)
            raise
        if rejection is not None:
            self._append_event(
                "self_edit_rejected", round_no, {"proposed": sha, "reason": rejection}
            )
            return None
        if kept is None:
            logger.info("rsi.self_edit.no_change", round=round_no)
            return None
        try:
            blob = await run_git(
                self.sandbox,
                "rev-parse",
                "--verify",
                "-q",
                f"{kept}:{SCAFFOLD_FILENAME}",
                check=True,
            )
        except asyncio.CancelledError:
            await self._cleanup_cancelled_self_edit(before, results_before, round_no, lock)
            raise
        self.scaffold_blob = blob.stdout.strip()
        self.scaffold_sha = kept
        self._append_event(
            "self_edit",
            round_no,
            {
                "scaffold_sha": kept,
                "scaffold_blob": self.scaffold_blob,
                "judgment_window": self.config.self_edit_every,
            },
        )
        logger.info("rsi.self_edit.kept", round=round_no, scaffold_sha=kept[:12])
        return kept

    async def _cleanup_cancelled_self_edit(
        self,
        before: _TreeState,
        results_before: Any,
        round_no: int,
        lock: VerifierLock,
    ) -> None:
        """Restore supervisor history after cancellation without hiding verifier tamper.

        ``CancelledError`` inherits ``BaseException`` and otherwise bypasses the
        proposal failure guard. Preserve changed trajectory bytes as evidence,
        then discard an ordinary partial proposal. A verifier or pinned-file
        mutation is deliberately left for the normal lock check to stop on.
        """
        try:
            if (
                self.cheat.trajectory_change(
                    self.results, results_before, trajectory=self.trajectory_path
                )
                is not None
            ):
                evidence = self.cheat.quarantine_trajectory(
                    self.results, results_before, round_no, trajectory=self.trajectory_path
                )
                logger.warning(
                    "rsi.self_edit.cancelled_history_restored",
                    round=round_no,
                    evidence=evidence,
                )

            guarded = {VERIFIER_LOCK_FILENAME, *lock.file_sha256s}
            changed = set(await self._changed_since(before))
            head_now = (await run_git(self.sandbox, "rev-parse", "HEAD")).stdout.strip()
            if head_now != before.head:
                diff = await run_git(self.sandbox, "diff", "--name-only", before.head, head_now)
                if diff.ok:
                    changed.update(p for p in diff.stdout.splitlines() if p.strip())
                else:
                    changed.update(guarded)
            if changed & guarded:
                logger.warning(
                    "rsi.self_edit.cancelled_verifier_tamper",
                    round=round_no,
                    paths=sorted(changed & guarded),
                )
                return
            await self._discard_to(before)
        except BaseException as exc:
            logger.warning(
                "rsi.self_edit.cancelled_cleanup_failed",
                round=round_no,
                error=repr(exc),
            )

    def _verifier_needles(self, lock: VerifierLock) -> list[str]:
        """Everything I4 keeps out of the summary: command, hashes, lock text, pinned file bodies."""
        needles: list[str] = list(iter_forbidden(lock))
        needles.extend(lock.file_sha256s.values())
        lock_path = verifier_lock_path(self.sandbox)
        if not lock_path.is_symlink() and lock_path.is_file():
            needles.append(self._read_text(lock_path).strip())
        for rel in lock.file_sha256s:
            path = self.sandbox / rel
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if os.lstat(path).st_size > _NEEDLE_MAX_BYTES:
                    continue
                body = self._read_text(path).strip()
            except OSError:
                continue
            if len(body) >= _NEEDLE_MIN_CHARS:
                needles.append(body)
        # Non-empty, deduplicated; too-short bodies would redact ordinary text.
        out: list[str] = []
        for needle in needles:
            if needle and needle not in out:
                out.append(needle)
        return out

    def _read_sandbox_file(self, name: str) -> str:
        """Text of ``<sandbox>/<name>`` if it is a regular file (no symlink); ``""`` otherwise."""
        path = self.sandbox / name
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            logger.warning("rsi.loop.unreadable", path=str(path), error=str(exc))
            return ""
        if not stat.S_ISREG(st.st_mode):
            logger.warning(
                "rsi.self_edit.not_a_regular_file",
                path=str(path),
                hint="symlinks and special files are never read into the self-edit summary (I4)",
            )
            return ""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            logger.warning("rsi.loop.unreadable", path=str(path), error=str(exc))
            return ""
        with os.fdopen(fd, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")

    async def _status_paths(self) -> dict[str, str]:
        """``{path: XY}`` for every dirty path; untracked *and* ignored files listed individually."""
        status = await run_git(
            self.sandbox,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored=traditional",
            "-z",
            check=True,
        )
        return _parse_status_z(status.stdout)

    async def _tree_state(self) -> _TreeState:
        head = (await run_git(self.sandbox, "rev-parse", "HEAD", check=True)).stdout.strip()
        tree = await run_git(self.sandbox, "write-tree")
        index_tree = tree.stdout.strip() if tree.ok else None
        status = await self._status_paths()
        dirty: dict[str, str | None] = {}
        kept: dict[str, bytes] = {}
        for rel, xy in status.items():
            path = self.sandbox / rel
            digest = _digest_or_none(path, cheap=xy == "!!")
            dirty[rel] = digest
            if digest is None or not _is_content_digest(digest):
                continue
            try:
                if os.lstat(path).st_size <= _RESTORE_MAX_BYTES:
                    kept[rel] = path.read_bytes()
            except OSError as exc:
                logger.warning("rsi.self_edit.snapshot_skipped", path=rel, error=str(exc))
        return _TreeState(head=head, index_tree=index_tree, dirty=dirty, kept=kept)

    async def _changed_since(self, before: _TreeState) -> list[str]:
        """Paths whose content differs from the snapshot: new, modified (dirty before included), gone."""
        after = await self._status_paths()
        changed: set[str] = set()
        for rel, xy in after.items():
            now = _digest_or_none(self.sandbox / rel, cheap=xy == "!!")
            if rel not in before.dirty or now != before.dirty[rel]:
                changed.add(rel)
        for rel, digest in before.dirty.items():
            cheap = digest is not None and digest.startswith("stat:")
            if rel not in after and _digest_or_none(self.sandbox / rel, cheap=cheap) != digest:
                changed.add(rel)
        return sorted(changed)

    async def _verify_self_edit(
        self,
        before: _TreeState,
        sha: str | None,
        lock: VerifierLock,
        *,
        results_changed: Sequence[str] = (),
        trajectory_changed: str | None = None,
    ) -> tuple[str | None, str | None]:
        """I3: keep ``sha`` only if it changed exactly SCAFFOLD.md and touched nothing else.

        Returns ``(kept_sha, rejection_reason)``; ``(None, None)`` is a step
        that changed nothing. ``results_changed`` lists results-dir entries
        the step created or modified; any such entry rejects the edit (the
        results themselves are never discarded — ``metrics.jsonl`` and the
        desktop's files are not the loop's to delete). ``trajectory_changed``
        carries the content-based supervisor-history failure after its changed
        bytes have been quarantined.

        Raises:
            FrozenVerifierError: the step touched ``VERIFIER.json`` or a
                pinned file, in the working tree or in a commit. Nothing is
                restored then — the tree is left for the operator and the
                loop must stop (I1, I5), never quietly put the verifier back
                and carry on.
        """
        changed = await self._changed_since(before)
        head_after = (await run_git(self.sandbox, "rev-parse", "HEAD")).stdout.strip()
        guarded = {VERIFIER_LOCK_FILENAME, *lock.file_sha256s}
        touched_guarded = set(changed) & guarded
        if head_after != before.head:
            diff = await run_git(self.sandbox, "diff", "--name-only", before.head, head_after)
            committed = {p for p in diff.stdout.splitlines() if p.strip()}
            touched_guarded |= committed & guarded if diff.ok else guarded
        if touched_guarded:
            raise FrozenVerifierError(
                f"self-edit step touched {sorted(touched_guarded)} (the verifier lock or a file "
                "it pins); the tree is left untouched for inspection and the loop stops"
            )
        reason: str | None = None
        if trajectory_changed is not None:
            reason = trajectory_changed
        elif results_changed:
            reason = f"self-edit wrote to the results dir: {sorted(results_changed)}"
        elif sha is None:
            if changed or head_after != before.head:
                reason = "step returned None but left changes behind"
        elif not sha.strip() or head_after != sha.strip():
            reason = f"returned sha {sha!r} is not HEAD ({head_after[:12]})"
        elif changed:
            reason = f"self-edit changed paths in the working tree: {changed}"
        else:
            diff = await run_git(self.sandbox, "diff", "--name-only", before.head, sha.strip())
            touched = {p for p in diff.stdout.splitlines() if p.strip()}
            if not diff.ok or touched != {SCAFFOLD_FILENAME}:
                reason = f"self-edit touched {sorted(touched)}, only {SCAFFOLD_FILENAME} is allowed"
            else:
                listed = await run_git(
                    self.sandbox, "ls-tree", sha.strip(), "--", SCAFFOLD_FILENAME
                )
                mode = listed.stdout.split(" ", 1)[0] if listed.stdout else ""
                if mode != _REGULAR_FILE_MODE:
                    reason = f"{SCAFFOLD_FILENAME} in {sha.strip()[:12]} has mode {mode or '?'}"
        if reason is None:
            return (sha.strip() if sha else None), None
        logger.warning("rsi.self_edit.rejected", reason=reason, discarded_paths=changed)
        await self._discard_to(before)
        return None, reason

    async def _discard_to(self, before: _TreeState) -> None:
        """Undo what the step did and nothing else: HEAD/index back, touched paths restored."""
        head_now = (await run_git(self.sandbox, "rev-parse", "HEAD")).stdout.strip()
        if head_now != before.head:
            await run_git(self.sandbox, "reset", "-q", "--soft", before.head, check=True)
        if before.index_tree is not None:
            await run_git(self.sandbox, "read-tree", before.index_tree, check=True)
        changed = await self._changed_since(before)
        results_root = self.results.resolve()
        removed: list[str] = []
        for rel in sorted(changed, key=lambda p: (p.count("/"), p), reverse=True):
            path = self.sandbox / rel
            if _is_protected(path, results_root):
                continue
            try:
                if rel in before.kept:
                    _remove_path(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(before.kept[rel])
                elif rel in before.dirty:
                    digest = before.dirty[rel]
                    if digest is None:
                        _remove_path(path)
                    elif digest.startswith("link:"):
                        _remove_path(path)
                        os.symlink(digest[5:], path)
                    else:
                        logger.warning("rsi.self_edit.restore_impossible", path=rel)
                elif await self._in_index(rel):
                    if path.is_symlink() or path.is_dir():
                        _remove_path(path)
                    await run_git(self.sandbox, "checkout", "--", rel, check=True)
                else:
                    _remove_path(path)
                    removed.append(rel)
            except (OSError, ContractViolationError) as exc:
                logger.warning("rsi.self_edit.cleanup_failed", path=rel, error=str(exc))
        _prune_empty_parents(self.sandbox, removed)

    async def _in_index(self, rel: str) -> bool:
        listed = await run_git(self.sandbox, "ls-files", "-z", "--", rel)
        return listed.ok and bool(listed.stdout.strip("\0"))

    # ------------------------------------------------------------ rollback

    async def _rebuild_pending(self, state: TrajectoryState) -> _PendingEdit | None:
        """The newest ``self_edit`` not yet judged, reconstructed from the trajectory."""
        pending_sha: str | None = None
        pending_round = 0
        pending_blob: str | None = None
        pending_window: int | None = None
        pending_window_source = "recorded"
        for event in state.events:
            if event.event == "self_edit":
                raw_sha = event.details.get("scaffold_sha")
                if raw_sha is None or not str(raw_sha).strip():
                    raise ContractViolationError(
                        f"self-edit event at round {event.round} is missing scaffold_sha; "
                        "refusing to resume without rollback provenance"
                    )
                pending_sha = str(raw_sha).strip()
                pending_round = event.round
                raw_blob = event.details.get("scaffold_blob")
                pending_blob = None if raw_blob is None else str(raw_blob).strip()
                raw_window = event.details.get("judgment_window")
                if raw_window is None:
                    # Events written before #456 did not persist their window.
                    # Preserve the old invocation's usual behavior when the
                    # operator still supplies a schedule, but use one round as
                    # a visible fail-safe when proposals are now disabled.
                    if self.config.self_edit_every > 0:
                        pending_window = self.config.self_edit_every
                        pending_window_source = "legacy_config"
                    else:
                        pending_window = _LEGACY_PENDING_WINDOW
                        pending_window_source = "legacy_minimum"
                elif (
                    isinstance(raw_window, bool)
                    or not isinstance(raw_window, int)
                    or raw_window <= 0
                ):
                    raise ContractViolationError(
                        f"self-edit event at round {event.round} has invalid judgment_window "
                        f"{raw_window!r}; refusing to resume without rollback provenance"
                    )
                else:
                    pending_window = raw_window
                    pending_window_source = "recorded"
            elif event.event == "rollback_failed":
                judged = event.details.get("reverted") or event.details.get("scaffold_sha")
                if judged is not None and str(judged) == pending_sha:
                    raise ContractViolationError(
                        f"pending self-edit {pending_sha[:12]} has a persisted rollback_failed; "
                        "refusing to resume under an edit the prior run could not undo"
                    )
            elif event.event in {"rollback", "self_edit_kept"}:
                judged = event.details.get("reverted") or event.details.get("scaffold_sha")
                if judged is not None and str(judged) == pending_sha:
                    pending_sha = None
                    pending_blob = None
                    pending_window = None
        if pending_sha is None:
            return None
        ancestor = await run_git(self.sandbox, "merge-base", "--is-ancestor", pending_sha, "HEAD")
        if not ancestor.ok:
            raise ContractViolationError(
                f"pending self-edit {pending_sha[:12]} is no longer in HEAD's history; "
                "refusing to resume without rollback provenance"
            )
        committed_blob = await run_git(
            self.sandbox,
            "rev-parse",
            "--verify",
            "-q",
            f"{pending_sha}:{SCAFFOLD_FILENAME}",
        )
        committed_blob_text = committed_blob.stdout.strip()
        if not committed_blob.ok or not committed_blob_text:
            raise ContractViolationError(
                f"pending self-edit {pending_sha[:12]} has no verifiable {SCAFFOLD_FILENAME}; "
                "refusing to resume without rollback provenance"
            )
        if pending_blob is not None and pending_blob != committed_blob_text:
            raise ContractViolationError(
                f"pending self-edit {pending_sha[:12]} records scaffold blob {pending_blob[:12]}, "
                f"but its commit contains {committed_blob_text[:12]}; refusing to resume"
            )
        if pending_window is None:
            raise ContractViolationError(
                f"pending self-edit {pending_sha[:12]} has no judgment window; "
                "refusing to resume without rollback provenance"
            )
        counted_before = [r for r in state.records if not r.void and r.round <= pending_round]
        scores_before = [r.score for r in counted_before if r.passed and r.score is not None]
        pending = _PendingEdit(
            sha=pending_sha,
            committed_after_round=pending_round,
            judgment_window=pending_window,
            judgment_window_source=pending_window_source,
            best_before=max(scores_before) if scores_before else None,
            passed_before=bool(counted_before and counted_before[-1].passed),
            prior_scores=tuple(scores_before),
            rounds_after=[r for r in state.records if not r.void and r.round > pending_round],
        )
        logger.info(
            "rsi.self_edit.pending_resumed",
            scaffold_sha=pending_sha[:12],
            rounds_after=len(pending.rounds_after),
            judgment_window=pending.judgment_window,
            judgment_window_source=pending.judgment_window_source,
        )
        return pending

    async def _judge_pending(
        self, pending: _PendingEdit, round_no: int
    ) -> Literal["kept", "rolled_back", "failed"]:
        """Revert the scaffold commit if the rounds since it got worse by more than the noise."""
        after = pending.rounds_after
        scores_after = [r.score for r in after if r.passed and r.score is not None]
        best_after = max(scores_after) if scores_after else None
        scored_problem = pending.best_before is not None or best_after is not None
        if scored_problem:
            if pending.best_before is None:
                degraded = False
            elif best_after is None:
                degraded = True
            else:
                noise = self._noise_floor(pending.prior_scores)
                degraded = pending.best_before - best_after > noise
        else:
            degraded = pending.passed_before and any(not r.passed for r in after)
        if not degraded:
            logger.info(
                "rsi.self_edit.kept_after_evaluation",
                scaffold_sha=pending.sha[:12],
                best_before=pending.best_before,
                best_after=best_after,
            )
            self._append_event(
                "self_edit_kept",
                round_no,
                {
                    "scaffold_sha": pending.sha,
                    "judgment_window": pending.judgment_window,
                    "judgment_window_source": pending.judgment_window_source,
                    "best_before": pending.best_before,
                    "best_after": best_after,
                },
            )
            return "kept"
        try:
            revert_sha = await self._revert_scaffold_commit(pending.sha)
        except ContractViolationError as exc:
            logger.error("rsi.rollback.failed", scaffold_sha=pending.sha, error=str(exc))
            self._append_event(
                "rollback_failed",
                round_no,
                {"reverted": pending.sha, "error": str(exc)[-500:]},
            )
            return "failed"
        self._append_event(
            "rollback",
            round_no,
            {
                "reverted": pending.sha,
                "revert_sha": revert_sha,
                "judgment_window": pending.judgment_window,
                "judgment_window_source": pending.judgment_window_source,
                "best_before": pending.best_before,
                "best_after": best_after,
                "scaffold_sha": revert_sha,
                "scaffold_blob": self.scaffold_blob,
            },
        )
        logger.warning(
            "rsi.rollback",
            reverted=pending.sha[:12],
            best_before=pending.best_before,
            best_after=best_after,
        )
        return "rolled_back"

    async def _revert_scaffold_commit(self, sha: str) -> str:
        """Put ``SCAFFOLD.md`` back to ``sha^``'s version and commit only that path.

        Unlike ``git revert`` this does not need a clean index: whatever the
        round agent left staged stays staged and uncommitted.

        Raises:
            ContractViolationError: ``sha`` is not a scaffold-only commit, or
                the restore/commit failed.
        """
        touched = await run_git(
            self.sandbox, "diff-tree", "--no-commit-id", "--name-only", "-r", sha
        )
        names = [n for n in touched.stdout.splitlines() if n]
        if not touched.ok or names != [SCAFFOLD_FILENAME]:
            raise ContractViolationError(
                f"refusing to revert {sha[:12]}: it touches {names or 'nothing'}, "
                f"not only {SCAFFOLD_FILENAME}"
            )
        subject = (await run_git(self.sandbox, "log", "-n1", "--format=%s", sha)).stdout.strip()
        path = self.sandbox / SCAFFOLD_FILENAME
        if path.is_symlink() or (path.exists() and not path.is_file()):
            _remove_path(path)
        await run_git(self.sandbox, "checkout", f"{sha}^", "--", SCAFFOLD_FILENAME, check=True)
        await run_git(
            self.sandbox,
            "commit",
            "-q",
            "--no-verify",
            "--only",
            "-m",
            f'Revert "{subject}"\n\nrsi: rollback of scaffold self-edit {sha}',
            "--",
            SCAFFOLD_FILENAME,
            check=True,
        )
        head = (await run_git(self.sandbox, "rev-parse", "HEAD", check=True)).stdout.strip()
        blob = await self._head_scaffold_blob()
        if blob is None:
            raise ContractViolationError(f"{SCAFFOLD_FILENAME} vanished from HEAD after rollback")
        self.scaffold_blob = blob
        self.scaffold_sha = head
        return head

    def _noise_floor(self, prior_scores: Sequence[float]) -> float:
        if self.config.noise_floor is not None:
            return self.config.noise_floor
        if len(prior_scores) < 2:
            return 0.0
        return statistics.pstdev(prior_scores)


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #


def _is_content_digest(digest: str) -> bool:
    return not digest.startswith(("link:", "special:", "stat:")) and digest != "dir"


def _remove_path(path: Path) -> None:
    """Remove a file, symlink or directory tree without following symlinks."""
    if path.is_symlink() or not path.is_dir():
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            child.rmdir()
    path.rmdir()


def _is_protected(path: Path, results_root: Path) -> bool:
    """Never discard results or metrics, even if they live inside the sandbox (symlinks excepted)."""
    if path.is_symlink():
        return False
    if path.name == METRICS_FILENAME:
        return True
    try:
        path.resolve().relative_to(results_root)
    except (OSError, ValueError):
        return False
    return True


def _prune_empty_parents(sandbox: Path, rels: Iterable[str]) -> None:
    root = sandbox.resolve()
    for rel in rels:
        parent = (sandbox / rel).parent.resolve()
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


def describe_plan(config: RsiConfig, verifier: VerifierSpec | None) -> dict[str, Any]:
    """The ``--dry-run`` view: resolved paths and what a run would do. Touches nothing, never raises for disk state."""
    sandbox, results = config.sandbox_dir, config.results_dir
    resuming = (sandbox / PROBLEM_FILENAME).exists()
    lock_status = "absent"
    try:
        lock = load_verifier_lock(sandbox)
    except FrozenVerifierError as exc:
        lock_status = f"unreadable: {exc}"
        lock = None
    if lock is not None:
        try:
            check_verifier_lock(
                lock, sandbox, expected_command=None if verifier is None else verifier.command
            )
            lock_status = "intact"
        except FrozenVerifierError as exc:
            lock_status = f"MISMATCH: {exc}"
    elif lock_status == "absent" and verifier is not None:
        lock_status = f"would lock {shlex.quote(verifier.command)}"
    next_round: int | None = 1
    trajectory_status = "absent"
    if results.exists():
        try:
            state = read_trajectory(results / TRAJECTORY_FILENAME)
        except ContractViolationError as exc:
            next_round = None
            trajectory_status = f"UNREADABLE: {exc}"
        else:
            next_round = state.next_round
            trajectory_status = (
                f"{len(state.records)} round line(s), {len(state.events)} event(s)"
                if (results / TRAJECTORY_FILENAME).exists()
                else "absent"
            )
    return {
        "sandbox": str(sandbox),
        "results": str(results),
        "rounds": config.rounds,
        "resuming": "yes" if resuming else "no",
        "next_round": next_round,
        "trajectory": trajectory_status,
        "verifier_lock": lock_status,
        "taxonomy_digest": TAXONOMY_DIGEST,
        "self_edit_every": config.self_edit_every,
        "self_edit_budget": config.self_edit_budget,
    }
