"""``python -m turing.research.rsi`` — the operator entry point for the RSI workstation loop.

Mirrors ``scripts/rsi-loop.sh``'s flags (``--slug``, ``--results-root``,
``--problem``, ``--rounds``, ``--dry-run``) and adds the engine's own:
``--verifier``, ``--verifier-file``, ``--self-edit-every``,
``--self-edit-budget``, ``--noise-floor``, ``--round-timeout-seconds``,
``--verifier-timeout-seconds``, ``--workspace-root``, ``--engine``. The bash
script itself ``exec``s this module whenever ``--verifier`` is given,
``TURING_RSI_ENGINE=python`` is set, or the slug's sandbox already holds a
``VERIFIER.json``, so the desktop's argv contract keeps working unchanged.

What this module guarantees:

* ``--dry-run`` prints the resolved plan (sandbox, results, rounds, start
  round, resuming, verifier lock status, what the lock would pin, taxonomy
  digest, and every reason the real run would be refused) and exits 0
  **without creating or writing anything** — not the sandbox, not the lock,
  not ``taxonomy.json``. The plan and the real run share one pre-flight
  (:func:`resolve_plan`), so a plan that shows no ``REFUSED`` line passes
  every pre-flight check that can be made by reading. The on-disk pre-flight
  in :meth:`RsiLoop.prepare` (``git init``, adopting the committed
  ``SCAFFOLD.md``, binding the lock to the ``verifier_locked`` event) can
  still refuse (exit 2) or stop as a tamper (exit 3) after that; those
  states are not visible to a read-only plan.
* Before any disk write it refuses, with exit 2: a missing ``--problem`` or
  ``--verifier`` on a first run; a resume whose ``--verifier`` (or
  ``--verifier-file`` set) differs from the lock (the lock wins; it is never
  silently ignored); a sandbox that already ran rounds but has **no**
  ``VERIFIER.json`` (a removed lock is never re-locked — I1); a first run
  whose lock would pin **no** file although the command names one (so
  ``--verifier "python grade.py"`` cannot leave ``grade.py`` unfrozen by
  accident); a corrupt ``trajectory.json``; a taxonomy digest mismatch (I6).
  The on-disk pre-flight itself — directories, ``git init``, ``PROBLEM.md``,
  the lock write — is :meth:`RsiLoop.prepare`.
* A lock that no longer matches the sandbox when the loop starts (a pinned
  file changed while nothing was running) is a tamper, not a usage error: it
  exits 3 and appends a ``verifier_tampered`` event line to
  ``trajectory.json`` (append-only, I7) so the desktop's trajectory pane and
  later aggregates see the stop.
* ``--engine fake`` is refused unless ``--dry-run`` or the environment
  variable ``TURING_RSI_ALLOW_FAKE_ENGINE=1`` is set, so a demo engine can
  never be picked by accident for a real run.
* Exit codes: 0 normal completion, 2 usage / pre-flight refusal, 3 the loop
  stopped on a cheat or tamper (:data:`EXIT_STOPPED`), 130 interrupted by
  Ctrl-C (:data:`EXIT_INTERRUPTED`; no trajectory line is written for the
  round that was in flight).

What it does not do:

* It does not run rounds. That is :class:`turing.research.rsi.loop.RsiLoop`,
  which this module only assembles; every invariant about measuring,
  self-editing and stopping lives there and in the sibling modules.
* It does not kill the engine's process tree on Ctrl-C; that is the engine's
  job (:mod:`turing.research.rsi.engine`).
* It does not configure logging beyond structlog's defaults.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from turing.research.contracts import ContractViolationError, FrozenVerifierError
from turing.research.rsi.cheat import git_env
from turing.research.rsi.contracts import (
    SLUG_PATTERN,
    VERIFIER_LOCK_FILENAME,
    Engine,
    LoopEvent,
    RsiConfig,
    VerifierLock,
    VerifierSpec,
    check_verifier_lock,
    compute_verifier_lock,
)
from turing.research.rsi.loop import (
    PROBLEM_FILENAME,
    TRAJECTORY_FILENAME,
    append_jsonl,
    read_trajectory,
)
from turing.research.rsi.taxonomy import (
    TAXONOMY_DIGEST,
    TAXONOMY_FILENAME,
    TAXONOMY_VERSION,
)
from turing.research.rsi.verifier import load_verifier_lock, sandbox_file_tokens

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger(__name__)

__all__ = [
    "ALLOW_FAKE_ENGINE_ENV",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_STOPPED",
    "EXIT_USAGE",
    "Plan",
    "build_parser",
    "count_round_lines",
    "main",
    "resolve_plan",
]

#: Normal completion (rounds exhausted, STOP file, or consecutive engine failures).
EXIT_OK: int = 0
#: Usage error or pre-flight refusal (bad flag, missing --problem/--verifier, taxonomy drift).
EXIT_USAGE: int = 2
#: The loop stopped because the cheat detector fired or the verifier lock was broken.
EXIT_STOPPED: int = 3
#: Ctrl-C / SIGINT while a round was in flight; nothing was recorded for that round.
EXIT_INTERRUPTED: int = 130
#: Set to ``1`` to allow ``--engine fake`` outside ``--dry-run``.
ALLOW_FAKE_ENGINE_ENV: str = "TURING_RSI_ALLOW_FAKE_ENGINE"


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {text!r}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {text!r}")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {text!r}") from exc
    if not value > 0 or value == float("inf"):
        raise argparse.ArgumentTypeError(f"expected a positive finite number, got {text!r}")
    return value


def _non_negative_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a non-negative number, got {text!r}") from exc
    if value < 0 or value != value or value == float("inf"):
        raise argparse.ArgumentTypeError(f"expected a non-negative finite number, got {text!r}")
    return value


def _slug(text: str) -> str:
    if not SLUG_PATTERN.match(text):
        raise argparse.ArgumentTypeError(f"--slug must match {SLUG_PATTERN.pattern} (got {text!r})")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m turing.research.rsi",
        description=(
            "Run the RSI workstation loop: round after round of `claude -p` in a sandbox, "
            "graded by a frozen verifier, categorised by a frozen taxonomy."
        ),
    )
    parser.add_argument(
        "--slug", required=True, type=_slug, help="sandbox/results name; ^[a-z0-9-]+$"
    )
    parser.add_argument(
        "--results-root",
        required=True,
        type=Path,
        help="results stream here under loop-rsi-<slug>/ (made absolute against the cwd)",
    )
    parser.add_argument(
        "--problem",
        default=None,
        help="the goal; required on the first run (ignored on resume, PROBLEM.md wins)",
    )
    parser.add_argument(
        "--rounds",
        default=10,
        type=_non_negative_int,
        help="rounds to run this invocation; 0 means unlimited (default 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved plan and exit 0 without touching disk",
    )
    parser.add_argument(
        "--verifier",
        default=None,
        help=(
            "shell command run from the sandbox after every round; exit code is pass/fail, "
            "last stdout line matching score=<float> is the score. Required on the first "
            f"run, locked into {VERIFIER_LOCK_FILENAME}; a different command on resume is refused"
        ),
    )
    parser.add_argument(
        "--verifier-file",
        action="append",
        default=[],
        metavar="RELPATH",
        help=(
            "sandbox-relative file whose sha256 becomes part of the verifier lock; repeatable. "
            "Only the command's FIRST token is pinned automatically (./grade.sh), so "
            "`--verifier 'python grade.py'` needs `--verifier-file grade.py`"
        ),
    )
    parser.add_argument(
        "--self-edit-every",
        default=3,
        type=_non_negative_int,
        help="let the engine rewrite SCAFFOLD.md every N rounds; 0 disables (default 3)",
    )
    parser.add_argument(
        "--self-edit-budget",
        default=3,
        type=_non_negative_int,
        help="max self-edits per invocation (default 3; ADR 0011 §15 R=3)",
    )
    parser.add_argument(
        "--noise-floor",
        default=None,
        type=_non_negative_float,
        help="override the rollback noise floor (default: population stdev of prior scores)",
    )
    parser.add_argument(
        "--round-timeout-seconds",
        default=1800.0,
        type=_positive_float,
        help="wall-clock cap per engine round (default 1800)",
    )
    parser.add_argument(
        "--verifier-timeout-seconds",
        default=600.0,
        type=_positive_float,
        help="wall-clock cap per verifier run (default 600)",
    )
    parser.add_argument(
        "--workspace-root",
        default=Path("~/turing-workspace"),
        type=Path,
        help="sandboxes live under here as rsi-<slug>/ (default ~/turing-workspace)",
    )
    parser.add_argument(
        "--engine",
        choices=("claude", "fake"),
        default="claude",
        help=(
            "which engine runs a round; 'fake' is for --dry-run and demos only and is refused "
            f"otherwise unless {ALLOW_FAKE_ENGINE_ENV}=1"
        ),
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> RsiConfig:
    # Absolute paths: the bash bridge used to `cd` before exec, and the desktop
    # panes watch a path, so a relative --results-root must not depend on cwd.
    return RsiConfig(
        slug=args.slug,
        results_root=Path(args.results_root).expanduser().absolute(),
        workspace_root=Path(args.workspace_root).expanduser().absolute(),
        rounds=args.rounds,
        self_edit_every=args.self_edit_every,
        self_edit_budget=args.self_edit_budget,
        noise_floor=args.noise_floor,
        round_timeout_seconds=args.round_timeout_seconds,
        verifier_timeout_seconds=args.verifier_timeout_seconds,
    )


# --------------------------------------------------------------------------- #
# The plan (dry-run and the real run share one pre-flight)
# --------------------------------------------------------------------------- #


def count_round_lines(trajectory: Path) -> int:
    """Round lines in ``trajectory.json``: JSONL lines without an ``event`` key.

    Bash-era four-key lines count. Parsing is
    :func:`turing.research.rsi.loop.read_trajectory`'s, so a line the real
    run would refuse (not JSON, not an object, malformed record) raises
    :class:`~turing.research.contracts.ContractViolationError` here too — the
    plan must never promise a start round the run will not honour.
    """
    return len(read_trajectory(trajectory).records)


def _taxonomy_status(results: Path) -> tuple[str, str | None]:
    """``(status line, refusal)``; the refusal is ``None`` when the run may proceed."""
    path = results / TAXONOMY_FILENAME
    if not path.exists():
        return "absent (will be written on first run)", None
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"UNREADABLE ({exc})", f"{path} is unreadable: {exc}"
    digest = stored.get("digest") if isinstance(stored, dict) else None
    if digest == TAXONOMY_DIGEST:
        return "present, matches", None
    return (
        f"present, MISMATCH (stored {digest!r})",
        f"taxonomy digest mismatch in {path}: stored {digest!r}, this code is "
        f"{TAXONOMY_DIGEST!r}; results are only comparable under one taxonomy — "
        "use a new results root (I6)",
    )


def _lock_history(sandbox: Path) -> bool:
    """True when the sandbox's git history has ever tracked ``VERIFIER.json``."""
    if not (sandbox / ".git").is_dir():
        return False
    try:
        proc = subprocess.run(
            ["git", "log", "--all", "--oneline", "--", VERIFIER_LOCK_FILENAME],
            cwd=str(sandbox),
            env=git_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _prior_run_evidence(
    sandbox: Path, results: Path, *, resuming: bool, round_lines: int | None
) -> list[str]:
    """Why a sandbox without a lock is *not* a first run; empty for a genuine first run."""
    evidence: list[str] = []
    if resuming:
        evidence.append(f"{PROBLEM_FILENAME} exists")
    if round_lines:
        evidence.append(f"{TRAJECTORY_FILENAME} already holds {round_lines} round line(s)")
    if (results / TAXONOMY_FILENAME).exists():
        evidence.append(f"{TAXONOMY_FILENAME} exists in the results dir")
    if _lock_history(sandbox):
        evidence.append(f"git history in the sandbox tracked {VERIFIER_LOCK_FILENAME}")
    return evidence


@dataclass(frozen=True, slots=True)
class _VerifierPlan:
    status: str
    pinned: tuple[str, ...]
    refusals: tuple[str, ...]
    tampered: bool


def _plan_first_run_lock(spec: VerifierSpec, sandbox: Path) -> _VerifierPlan:
    """What the first run would write, computed by reading only."""
    try:
        would = compute_verifier_lock(spec, sandbox, now_ms=0)
    except ContractViolationError as exc:
        return _VerifierPlan(
            status=f"absent; --verifier-file cannot be pinned: {exc}",
            pinned=(),
            refusals=(str(exc),),
            tampered=False,
        )
    pinned = tuple(sorted(would.file_sha256s))
    named = [t for t in sandbox_file_tokens(spec.command, sandbox) if t not in pinned]
    refusals: list[str] = []
    if not pinned:
        if named:
            refusals.append(
                f"--verifier {spec.command!r} would pin NO file, yet it names the sandbox "
                f"file(s) {', '.join(named)}; the agent could rewrite them unnoticed. Pass "
                f"--verifier-file {named[0]} (repeatable) or make the script the first "
                f"token (./{named[0]}) so the lock covers it (I1)"
            )
            status = f"absent; would pin NOTHING although the command names {', '.join(named)}"
        else:
            status = (
                f"absent (will be written from --verifier to {VERIFIER_LOCK_FILENAME}); "
                "pinned files: NONE — only the command string is frozen"
            )
    else:
        status = (
            f"absent (will be written from --verifier to {VERIFIER_LOCK_FILENAME}); "
            f"pinned files: {', '.join(pinned)}"
        )
        if named:
            status += f"; WARNING unpinned sandbox file(s) named by the command: {', '.join(named)}"
    return _VerifierPlan(status=status, pinned=pinned, refusals=tuple(refusals), tampered=False)


def _plan_existing_lock(
    lock: VerifierLock, sandbox: Path, spec: VerifierSpec | None
) -> _VerifierPlan:
    pinned = tuple(sorted(lock.file_sha256s))
    shown = ", ".join(pinned) or "none"
    refusals: list[str] = []
    if spec is not None and spec.command != lock.command:
        refusals.append(
            f"--verifier differs from the locked verifier in {sandbox / VERIFIER_LOCK_FILENAME}; "
            "the lock wins. Resume without --verifier, or start a new slug for a new verifier."
        )
    if spec is not None and set(spec.files) - set(pinned):
        refusals.append(
            "--verifier-file differs from the locked verifier's pinned files in "
            f"{VERIFIER_LOCK_FILENAME}; the lock wins and a different pin set is refused"
        )
    try:
        check_verifier_lock(lock, sandbox, expected_command=None)
    except FrozenVerifierError as exc:
        return _VerifierPlan(
            status=(
                f"present, BROKEN: {exc}; the run will stop with exit {EXIT_STOPPED} and "
                "record a verifier_tampered event"
            ),
            pinned=pinned,
            refusals=tuple(refusals),
            tampered=True,
        )
    if refusals:
        status = f"present, intact, DIFFERS from --verifier (pinned files: {shown})"
    elif spec is not None:
        status = f"present, intact, matches --verifier (pinned files: {shown})"
    else:
        status = f"present, intact (pinned files: {shown})"
    return _VerifierPlan(status=status, pinned=pinned, refusals=tuple(refusals), tampered=False)


@dataclass(frozen=True, slots=True)
class Plan:
    """What a run would do; rendered by ``--dry-run`` and enforced before a real run.

    ``refusals`` are the reasons the real run exits 2 before touching disk;
    an empty tuple means the run starts. ``start_round`` is ``None`` when the
    trajectory could not be parsed (that is itself a refusal).
    """

    config: RsiConfig
    engine: str
    resuming: bool
    start_round: int | None
    verifier_status: str
    taxonomy_status: str
    pinned_files: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    lock_tampered: bool = False

    def render(self) -> str:
        cfg = self.config
        rounds = "unlimited" if cfg.rounds == 0 else str(cfg.rounds)
        start = "UNKNOWN (trajectory unreadable)" if self.start_round is None else self.start_round
        lines = [
            f"sandbox: {cfg.sandbox_dir}",
            f"results: {cfg.results_dir}",
            f"rounds: {rounds}",
            f"start round: {start}",
            f"resuming: {'yes' if self.resuming else 'no'}",
            f"engine: {self.engine}",
            f"verifier lock: {self.verifier_status}",
            f"taxonomy: version {TAXONOMY_VERSION}, digest {TAXONOMY_DIGEST}, "
            f"{TAXONOMY_FILENAME} {self.taxonomy_status}",
            f"self-edit: every {cfg.self_edit_every or 'never'} round(s), "
            f"budget {cfg.self_edit_budget}, noise floor "
            f"{'auto (population stdev)' if cfg.noise_floor is None else cfg.noise_floor}",
            f"timeouts: round {cfg.round_timeout_seconds:g}s, "
            f"verifier {cfg.verifier_timeout_seconds:g}s",
        ]
        lines.extend(f"REFUSED (exit {EXIT_USAGE}): {reason}" for reason in self.refusals)
        return "\n".join(lines)


def resolve_plan(
    config: RsiConfig,
    *,
    engine: str,
    verifier: VerifierSpec | None,
    problem: str | None = None,
) -> Plan:
    """Compute the plan by reading only; never creates or modifies anything.

    Every refusal :func:`main` applies before a disk write is computed here,
    in the order the run would hit them, so ``--dry-run`` shows exactly what
    a real invocation would do.
    """
    sandbox = config.sandbox_dir
    results = config.results_dir
    resuming = (sandbox / PROBLEM_FILENAME).is_file()
    refusals: list[str] = []

    start_round: int | None
    round_lines: int | None
    try:
        round_lines = count_round_lines(results / TRAJECTORY_FILENAME)
        start_round = round_lines + 1
    except ContractViolationError as exc:
        round_lines = None
        start_round = None
        refusals.append(
            f"{exc} (truncate the partial last line of {TRAJECTORY_FILENAME} by hand, "
            "or start a new slug)"
        )

    taxonomy_status, taxonomy_refusal = _taxonomy_status(results)
    if taxonomy_refusal is not None:
        refusals.append(taxonomy_refusal)

    pinned: tuple[str, ...] = ()
    tampered = False
    try:
        lock = load_verifier_lock(sandbox)
    except FrozenVerifierError as exc:
        verifier_status = f"UNREADABLE ({exc})"
        refusals.append(f"{VERIFIER_LOCK_FILENAME} is unreadable: {exc}")
    else:
        if lock is not None:
            vp = _plan_existing_lock(lock, sandbox, verifier)
        elif verifier is None:
            vp = _VerifierPlan(
                status="absent; --verifier is required on the first run",
                pinned=(),
                refusals=(
                    f"--verifier is required on the first run (no {VERIFIER_LOCK_FILENAME} "
                    f"in {sandbox})",
                ),
                tampered=False,
            )
        else:
            evidence = _prior_run_evidence(
                sandbox, results, resuming=resuming, round_lines=round_lines
            )
            if evidence:
                ran = round_lines if round_lines is not None else "an unknown number of"
                vp = _VerifierPlan(
                    status="absent, but this slug already ran; a resume never writes a new lock",
                    pinned=(),
                    refusals=(
                        f"{VERIFIER_LOCK_FILENAME} is missing from a sandbox that already ran "
                        f"{ran} round(s) ({'; '.join(evidence)}); the lock was removed — "
                        f"inspect `git log -- {VERIFIER_LOCK_FILENAME}` in {sandbox}, restore "
                        "it, or start a new slug. A resume never re-locks the verifier (I1)",
                    ),
                    tampered=False,
                )
            else:
                vp = _plan_first_run_lock(verifier, sandbox)
        verifier_status = vp.status
        pinned = vp.pinned
        tampered = vp.tampered
        refusals.extend(vp.refusals)

    if not resuming and not problem:
        refusals.append(
            f"--problem is required on the first run (no {PROBLEM_FILENAME} in {sandbox})"
        )

    return Plan(
        config=config,
        engine=engine,
        resuming=resuming,
        start_round=start_round,
        verifier_status=verifier_status,
        taxonomy_status=taxonomy_status,
        pinned_files=pinned,
        refusals=tuple(refusals),
        lock_tampered=tampered,
    )


# --------------------------------------------------------------------------- #
# Assembly and the real run
# --------------------------------------------------------------------------- #


def _build_engine(name: str) -> Engine:
    if name == "claude":
        from turing.research.rsi.engine import ClaudeCliEngine

        return ClaudeCliEngine()
    if name == "fake":
        from turing.research.rsi.engine import FakeEngine, ok_result

        # A demo engine that "does nothing" every round; the verifier still grades it.
        return FakeEngine(default=ok_result())
    raise ContractViolationError(f"unknown engine {name!r}")


async def _run(
    config: RsiConfig, *, engine_name: str, problem: str | None, verifier: VerifierSpec | None
) -> int:
    """Assemble the loop and run it; :meth:`RsiLoop.prepare` owns the on-disk pre-flight."""
    from turing.research.rsi.loop import RsiLoop
    from turing.research.rsi.scaffold import ScaffoldSelfEditStep

    engine = _build_engine(engine_name)
    self_edit = (
        ScaffoldSelfEditStep(engine, config, config.sandbox_dir)
        if config.self_edit_every > 0 and config.self_edit_budget > 0
        else None
    )
    loop = RsiLoop(config, engine=engine, verifier=verifier, self_edit=self_edit, problem=problem)
    outcome = await loop.run()
    logger.info(
        "rsi.cli.finished",
        rounds_run=outcome.rounds_run,
        stop_reason=outcome.stop_reason.value,
        best_score=outcome.best_score,
        self_edits=outcome.self_edits,
        rollbacks=outcome.rollbacks,
        exit_code=outcome.exit_code,
    )
    print(
        f"done: {outcome.rounds_run} round(s) completed this invocation "
        f"({outcome.stop_reason.value}) — results in {config.results_dir}"
    )
    return int(outcome.exit_code)


def _refuse(message: str) -> int:
    print(f"error: {message}", flush=True)
    logger.error("rsi.cli.refused", reason=message)
    return EXIT_USAGE


def _record_startup_tamper(plan: Plan, exc: FrozenVerifierError) -> None:
    """Append a ``verifier_tampered`` event so the trajectory shows the stop (I7: append-only).

    The results dir is created by :meth:`RsiLoop.prepare` before the lock is
    checked, so it exists on this path; if it somehow does not, nothing is
    created here — a tamper report must not be the first write to a results
    dir that never had a run.
    """
    results = plan.config.results_dir
    if not results.is_dir():
        return
    event = LoopEvent(
        event="verifier_tampered",
        round=plan.start_round if plan.start_round is not None else 0,
        ts=int(time.time()),
        details={"when": "startup", "detail": str(exc)},
    )
    append_jsonl(results / TRAJECTORY_FILENAME, event.to_json_line())


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code rather than calling :func:`sys.exit`."""
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else EXIT_USAGE

    if args.engine == "fake" and not args.dry_run and os.environ.get(ALLOW_FAKE_ENGINE_ENV) != "1":
        parser.print_usage()
        return _refuse(
            f"--engine fake is for --dry-run and demos only; set {ALLOW_FAKE_ENGINE_ENV}=1 "
            "to run it for real"
        )

    try:
        config = _config_from_args(args)
        verifier: VerifierSpec | None = None
        if args.verifier is not None:
            verifier = VerifierSpec(command=args.verifier, files=tuple(args.verifier_file))
        elif args.verifier_file:
            raise ContractViolationError("--verifier-file requires --verifier")
    except ContractViolationError as exc:
        parser.print_usage()
        return _refuse(str(exc))

    plan = resolve_plan(config, engine=args.engine, verifier=verifier, problem=args.problem)
    if args.dry_run:
        print(plan.render())
        return EXIT_OK

    # Every pre-flight refusal happens here, before any disk write.
    if plan.refusals:
        if len(plan.refusals) > 1:
            for extra in plan.refusals[1:]:
                print(f"also: {extra}", flush=True)
        return _refuse(plan.refusals[0])

    logger.info("rsi.cli.plan", **_plan_fields(plan))
    if not plan.resuming:
        print(
            f"verifier lock will pin: {', '.join(plan.pinned_files) or 'NOTHING (command string only)'}",
            flush=True,
        )
    try:
        return asyncio.run(
            _run(config, engine_name=args.engine, problem=args.problem, verifier=verifier)
        )
    except FrozenVerifierError as exc:
        # The lock no longer matches the sandbox: a tamper, not a usage error.
        print(f"error: verifier tampered — {exc}", flush=True)
        logger.error("rsi.cli.verifier_tampered", error=str(exc), when="startup")
        _record_startup_tamper(plan, exc)
        return EXIT_STOPPED
    except ContractViolationError as exc:
        return _refuse(str(exc))
    except KeyboardInterrupt:
        print(
            "interrupted: no trajectory line was written for the round in flight; "
            "check for a still-running engine before restarting",
            flush=True,
        )
        logger.warning("rsi.cli.interrupted", sandbox=str(config.sandbox_dir))
        return EXIT_INTERRUPTED


def _plan_fields(plan: Plan) -> dict[str, Any]:
    return {
        "sandbox": str(plan.config.sandbox_dir),
        "results": str(plan.config.results_dir),
        "rounds": plan.config.rounds,
        "start_round": plan.start_round,
        "resuming": plan.resuming,
        "engine": plan.engine,
        "pinned_files": list(plan.pinned_files),
        "taxonomy_digest": TAXONOMY_DIGEST,
    }
