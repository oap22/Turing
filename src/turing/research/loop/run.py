"""``python -m turing.research.loop.run`` — the loop-1 driver, resumable.

``RoundRunner`` and ``NoiseFloorRunner`` were written, tested and left with no
caller in ``src/``. This module is that caller: it loads
:class:`~turing.research.loop.settings.ResearchLoopSettings`, assembles the real
components, and drives **the noise floor, then round 0**, resuming from whatever
is already on disk::

    python -m turing.research.loop.run \\
        --loop-slug 2026-08-16-speedup-baseline \\
        --harness-root /Users/Shared/turing/harness \\
        --reference-root /Users/Shared/turing/reference \\
        --scaffold-repo /Users/Shared/turing/turing-skills \\
        --yes

Kill it at any point and run the identical command again: completed attempts are
reused, an interrupted attempt continues from its checkpoint, an attempt
suspended on an operator decision re-enters that wait, and a round that had
already finished *under this exact configuration* is returned as it was written
— a no-op success, not a refusal. A finished round measured some other way
(a different corpus, scaffold, cap or criterion under the same slug) is refused
before anything is driven, as is ``--no-resume`` against a finished round: the
trajectory is append-only, and a re-measurement gets a new slug. One driver
owns a results tree at a time — ``<loop-dir>/.driver.lock`` — and a second is
refused with the first's pid. See the "Resume" section of
``docs/research-agent.md`` for the completeness table this rests on, and
:meth:`~turing.research.loop.trajectory.TrajectoryStore.load_stored_attempt` for
the rules themselves.

**Two flags gate what an invocation touches.**

* ``--yes`` acknowledges that the run spends subscription compute. Without it
  a non-``--dry-run`` invocation runs the whole preflight — corpus, scaffold
  sha, backend client, both configs, the plan — and then refuses with exit
  ``2`` and one line, having written nothing under the results root. It is
  the difference between "show me what would run" and "run it", and it is
  deliberately not the default: the driver is invoked by hand and by
  schedulers alike, and a scheduler that spends a subscription window should
  have said so in its command line.
* ``--dry-run`` does everything ``--yes`` would validate and nothing it would
  spend. Concretely, a dry run **does** load the corpus and refuse a missing
  harness script, read the scaffold repo's ``HEAD``, read
  ``ResearchLoopSettings`` / ``SolverSettings`` / ``BackendSettings`` and
  construct the Anthropic SDK client (a missing credential refuses here, at
  zero compute; no request is sent), construct the
  :class:`~turing.research.loop.noise_floor.NoiseFloorConfig` and
  :class:`~turing.research.loop.runner.RoundConfig` the real run would use —
  so ``--seeds 1 --dry-run`` refuses exactly as ``--seeds 1`` would — and
  then perform every read-only check the runners perform before their first
  write: bind the eval-set hash against the corpus the same way they do,
  read ``.driver.lock`` and refuse if another driver holds the tree, and
  read ``trajectory.json`` / ``round-00/round.json`` and refuse if round 0
  finished under this slug but measured differently. It **does not** create
  the results directory, take the lock, materialise a workspace, or call a
  model. Exit ``0`` from a dry run means the identical command with ``--yes``
  in place of ``--dry-run`` gets past preflight.

**What this driver deliberately does not do**, each refused loudly rather than
faked:

* **Rounds greater than 0.** A round *N* > 0 needs its parent's
  :class:`~turing.research.contracts.RoundRecord` to compute a delta against,
  and this driver does not load one — ``round-NN/round.json`` can be decoded
  (:func:`~turing.research.loop.trajectory.decode_round_record`, used only so a
  restart after a finished round is a no-op), but nothing here selects a
  parent, loads it and validates its lineage. It also needs loop 2's self-edit step, which is what
  makes round *N* differ from round 0 at all and is explicitly out of scope
  (``docs/research-agent.md`` § What is deliberately NOT built yet). Passing
  ``--round`` a positive number refuses and names both.
* **Run model-authored commands.** The solver is built with no
  ``CommandRunner``, so a proposal that asks to run one raises out of
  ``propose_and_apply`` and escalates to the operator as a harness failure.
  That refusal is the safety-layer seam, and wiring an executor into it
  belongs to the sandbox work (RES-14), not here.
* **Drop privileges.** The workspace root is whatever
  ``TURING_RESEARCH_WORKSPACE_ROOT`` says; nothing here becomes the ``turing``
  user. ADR 0011 R2 is open, and a driver that pretended otherwise would be
  worse than one that says so.
* **Supply pass criteria.** No problem in the speedup corpus declares a
  binary bar, so every attempt runs to its cap and lands in
  ``FAILED_WITHIN_CAP`` holding its best score. That is the brief's solver
  shape, not a missing feature.

Exit codes: ``0`` the phase finished (or, on a restart, was already finished),
``2`` the run was refused (every refusal names the missing piece; a missing
``--yes`` is one of them), ``130`` interrupted — which, with resume, is a pause
rather than a loss.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from turing.research.contracts import ContractViolationError, EngineIdentity
from turing.research.loop.escalation import build_operator_channel
from turing.research.loop.noise_floor import NoiseFloorConfig, NoiseFloorRunner
from turing.research.loop.runner import RoundConfig, RoundRunner, find_finished_round
from turing.research.loop.settings import ResearchLoopSettings
from turing.research.loop.solver_bridge import SolverBridge
from turing.research.loop.trajectory import TrajectoryStore
from turing.research.loop.workspace import CopyTreeWorkspaceProvider
from turing.research.problems.adapter import bind_eval_set_hash, fingerprint_corpus
from turing.research.problems.speedup import SpeedupAdapter
from turing.research.solver import InMemoryCheckpointStore, Solver, WorkspaceManager
from turing.research.solver.config import SolverSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from turing.research.contracts import EscalationRequest, Problem

logger = structlog.get_logger(__name__)

__all__ = ["EXIT_INTERRUPTED", "EXIT_OK", "EXIT_REFUSED", "build_parser", "main"]

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_INTERRUPTED = 130

DEFAULT_SEEDS = (1, 2, 3)


class _RunnerOwnsEscalation:
    """The solver-side escalation seam, deliberately unreachable on this path.

    :class:`~turing.research.solver.Solver` takes an escalation channel because
    its standalone :meth:`~turing.research.solver.Solver.run` loop needs one.
    The round runner drives :meth:`propose_and_apply` instead and owns
    escalation itself, so anything reaching here would mean two components were
    both asking the operator about one attempt.
    """

    async def raise_escalation(self, request: EscalationRequest) -> None:
        raise ContractViolationError(
            "the round runner owns escalation on this path; the solver must not raise one"
        )

    async def await_decision(self, request_id: str) -> None:
        raise ContractViolationError(
            "the round runner owns escalation on this path; the solver must not wait on one"
        )


def _scaffold_sha(repo: Path) -> str:
    """The frozen scaffold fork's ``HEAD``, which *is* the scaffold version.

    ``EngineIdentity.scaffold_git_sha`` is lineage, not decoration: it is the
    answer to "what changed between round N and round N+1" and the thing
    ``git revert`` acts on. Read from the repo rather than accepted as a flag
    so it cannot disagree with the tree the run actually used.
    """
    if not (repo / ".git").exists():
        raise ContractViolationError(
            f"--scaffold-repo {repo} is not a git checkout; every round records the "
            "scaffold commit it was measured with, and a round with no lineage cannot "
            "be compared with any other. See scripts/bootstrap-turing-skills.sh"
        )
    try:
        completed = subprocess.run(
            # Fixed argv, no shell: the only interpolation is the repo path.
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContractViolationError(
            f"could not read HEAD of the scaffold repo {repo}: {exc}"
        ) from exc
    return completed.stdout.strip()


def _load_corpus(
    harness_root: Path, reference_root: Path
) -> tuple[SpeedupAdapter, tuple[Problem, ...]]:
    """Build the speedup corpus and refuse a corpus that cannot be measured.

    ``missing_harness_scripts`` is the mechanical form of the catalog's own
    admission that the ``{harness}`` benchmark drivers are *declared, not
    written*: producing them means running the 21.9 s, 24.5 s and 507 s
    baselines, which is a separate job. Discovering that mid-attempt would cost
    a subscription window; discovering it here costs nothing.
    """
    adapter = SpeedupAdapter(harness_root=harness_root, reference_root=reference_root)
    missing = adapter.missing_harness_scripts()
    if missing:
        listed = "\n  ".join(str(path) for path in missing)
        raise ContractViolationError(
            f"{len(missing)} declared harness script(s) do not exist under "
            f"{harness_root}:\n  {listed}\n"
            "The corpus declares its benchmark drivers and artifact dumpers; writing "
            "them means re-running the frozen baselines, which is a separate job. The "
            "run is refused here, at zero compute, rather than losing an attempt to it."
        )
    return adapter, adapter.load()


def _build_runner(
    *,
    solver: SolverBridge,
    store: TrajectoryStore,
    settings: ResearchLoopSettings,
    escalations_dir: Path,
) -> RoundRunner:
    return RoundRunner(
        solver=solver,
        workspaces=CopyTreeWorkspaceProvider(settings.research_workspace_root),
        trajectory=store,
        escalations=build_operator_channel(escalations_dir, settings),
    )


async def _drive(args: argparse.Namespace) -> int:
    settings = ResearchLoopSettings()
    solver_settings = SolverSettings()

    adapter, corpus = _load_corpus(args.harness_root, args.reference_root)
    undecided = [
        f"{spec.id}: {loophole.description}"
        for spec in adapter.specs
        for loophole in spec.undecided_loopholes
    ]
    if undecided:
        logger.warning(
            "research.run.undecided_loopholes",
            loopholes=undecided,
            detail=(
                "these questions have no operator ruling; loop 1 can still measure, but "
                "loop 2's cheat detector cannot be written against an undecided rule"
            ),
        )

    # Imported here rather than at module scope: ``from_settings`` constructs
    # the Anthropic SDK client, and a ``--help`` or a refused preflight must
    # not need the SDK installed or a credential present.
    from turing.research.backends.adapter import ProposalAdapter
    from turing.research.backends.claude import BACKEND_NAME, BackendSettings, ClaudeBackend

    backend_settings = BackendSettings()
    backend = ClaudeBackend.from_settings(backend_settings)
    engine = EngineIdentity(
        backend=BACKEND_NAME,
        orchestrator_model=backend_settings.orchestrator_model,
        substep_model=backend_settings.substep_model,
        scaffold_git_sha=_scaffold_sha(args.scaffold_repo),
    )
    solver = SolverBridge(
        Solver(
            backend=ProposalAdapter(backend),
            # The round runner owns checkpointing on this path — every
            # checkpoint that matters is written by ``TrajectoryStore`` under
            # the results root — so the solver-side store is never read back.
            # An SQLite file here would be theatre.
            store=InMemoryCheckpointStore(),
            workspaces=WorkspaceManager(settings.research_workspace_root),
            escalations=_RunnerOwnsEscalation(),
            # runner=None: no CommandRunner, so a proposal asking to execute
            # model-authored commands refuses and escalates.
            # TODO(RES-14): the sandbox / privilege-drop layer attaches here.
        )
    )

    store = TrajectoryStore(settings.research_results_root, args.loop_slug)
    eval_set_material = adapter.eval_set_material()
    harness_identity = adapter.harness_identity()
    eval_set_hash = fingerprint_corpus(corpus, extra=eval_set_material)

    # Both configs are built *before* the dry-run return so that a dry run
    # validates exactly what a real run would: ``--seeds 1`` refuses inside
    # ``NoiseFloorConfig.__post_init__`` here, on both paths, rather than
    # passing a dry run it would fail for real. ``eval_set_material`` rides
    # on the configs so the runners re-derive the hash with the same
    # ``extra`` it was fingerprinted with — one definition of the hash,
    # ``fingerprint_corpus``, called the same way on both sides.
    floor_config = NoiseFloorConfig(
        run_id=f"{args.loop_slug}-noise-floor",
        eval_set_hash=eval_set_hash,
        engine=engine,
        seeds=tuple(args.seeds),
        default_cap=solver_settings.default_cap(),
        escalate_on_cap_exhaustion=solver_settings.research_escalate_on_cap_exhausted,
        eval_set_material=eval_set_material,
        harness_identity=harness_identity,
    )
    round_config = RoundConfig(
        round_index=0,
        run_id=f"{args.loop_slug}-round-00",
        parent_round_id=None,
        eval_set_hash=eval_set_hash,
        engine=engine,
        seed=args.seeds[0],
        default_cap=solver_settings.default_cap(),
        escalate_on_cap_exhaustion=solver_settings.research_escalate_on_cap_exhausted,
        eval_set_material=eval_set_material,
        harness_identity=harness_identity,
    )

    # The rest of the preflight: every check the runners perform before
    # their first write, performed here on both the dry-run and the real
    # path so exit 0 from a dry run means the ``--yes`` run gets past all
    # of them. Each is read-only. In order:
    #
    # 1. The hash binding both runners perform — the same call, so a
    #    promise the runners would refuse is refused here.
    # 2. Whether another driver holds this results tree (the lock is only
    #    *read* here; the real path takes it below, after the gates).
    # 3. Whether round 0 already finished under this run_id and, if so,
    #    whether it was the identical measurement. A finished round measured
    #    some other way is refused before a single seed is re-driven under
    #    the new configuration; an identical one is a no-op later.
    try:
        bind_eval_set_hash(corpus, eval_set_hash, extra=eval_set_material)
        store.refuse_if_driver_lock_live()
        if not args.noise_floor_only:
            await find_finished_round(store, round_config, resume=args.resume)
    except ContractViolationError:
        await backend.aclose()
        raise

    sys.stdout.write(
        f"loop      {store.loop_dir}\n"
        f"workspace {settings.research_workspace_root}\n"
        f"corpus    {len(corpus)} problem(s), eval_set_hash {eval_set_hash}\n"
        f"engine    {engine.backend} / {engine.orchestrator_model} @ "
        f"{engine.scaffold_git_sha[:12]}\n"
        f"seeds     {list(floor_config.seeds)} (round 0 seeds with {round_config.seed})\n"
        f"cap       {round_config.default_cap.max_steps} steps / "
        f"{round_config.default_cap.max_tokens} tokens / "
        f"{round_config.default_cap.max_wall_clock_seconds:g} s\n"
        "answer escalations with:\n"
        f"  python -m turing.research.loop.cli --loop-dir <dir> --list\n"
    )
    if args.dry_run:
        sys.stdout.write(
            "dry run: everything above assembled and validated; nothing was driven and "
            "nothing was written under the results root\n"
        )
        await backend.aclose()
        return EXIT_OK
    if not args.yes:
        await backend.aclose()
        raise ContractViolationError(
            "this run spends subscription compute; re-run with --yes to acknowledge that, "
            "or --dry-run to stop here (nothing was written)"
        )

    # Past both gates. The backend is closed on every exit from here —
    # refusal, interruption or success — so a refused run does not leak the
    # SDK client it built in preflight.
    try:
        return await _drive_past_gates(
            args,
            settings=settings,
            store=store,
            solver=solver,
            corpus=corpus,
            floor_config=floor_config,
            round_config=round_config,
        )
    finally:
        await backend.aclose()


async def _drive_past_gates(
    args: argparse.Namespace,
    *,
    settings: ResearchLoopSettings,
    store: TrajectoryStore,
    solver: SolverBridge,
    corpus: Sequence[Problem],
    floor_config: NoiseFloorConfig,
    round_config: RoundConfig,
) -> int:
    """The floor, then round 0, under the driver lock. Every write happens here."""
    # The lock is the first write, and it is taken only past every read-only
    # preflight and both gates: a dry run and a refused run leave the results
    # root exactly as they found it. Held for the whole invocation — floor
    # and round — so a second driver cannot slip in between the two.
    async with store.driver_lock():
        await store.ensure_layout()

        # The floor is measured every invocation, and on a restart that costs
        # nothing: ``NoiseFloorRunner.run`` reuses every complete seed and
        # re-derives its cells from their checkpoints, producing the same
        # report it produced the first time. Short-circuiting on
        # ``has_noise_floor`` instead would be cheaper by one directory walk
        # and wrong: nothing decodes ``noise-floor.json`` back into a
        # :class:`NoiseFloorReport`, so round 0 would be driven with no floor
        # at all — no delta, no saturation verdict, and a
        # ``research.round.no_noise_floor`` warning nobody asked for.
        floor_runner = NoiseFloorRunner(
            _build_runner(
                solver=solver,
                store=store,
                settings=settings,
                escalations_dir=store.noise_floor_dir / "escalations",
            ),
            store,
        )
        report = await floor_runner.run(corpus, floor_config, resume=args.resume)
        sys.stdout.write(
            f"noise floor: {len(report.floors)} cell(s) over seeds {list(report.seeds)}"
            f" (reused {list(floor_runner.skipped_seeds)})\n"
        )

        if args.noise_floor_only:
            return EXIT_OK

        round_runner = _build_runner(
            solver=solver,
            store=store,
            settings=settings,
            escalations_dir=store.escalations_dir(0),
        )
        outcome = await round_runner.run_round(
            corpus,
            round_config,
            noise_floor=report,
            resume=args.resume,
        )
        if outcome.already_finished:
            sys.stdout.write(
                f"round 00: already finished under run_id {outcome.record.run_id!r} with "
                "this eval set, engine and configuration; nothing driven, nothing "
                "written\n"
                f"verdict: {outcome.record.verdict}\n"
            )
            return EXIT_OK
        sys.stdout.write(
            f"round 00: {len(outcome.attempts)} attempt(s), "
            f"{len(round_runner.skipped_problems)} reused, "
            f"{len(round_runner.resumed_problems)} resumed, "
            f"{len(outcome.failures)} lost\n"
            f"verdict: {outcome.record.verdict}\n"
        )
        return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turing-research-run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Drive the research loop's noise floor and round 0 against the speedup "
            "corpus, resuming from whatever is already on disk."
        ),
        epilog=(
            "A run spends subscription compute and must say so: pass --yes, or --dry-run "
            "to assemble and validate everything and stop. Without either, the preflight "
            "runs and the invocation refuses with exit 2 and nothing written.\n"
            "Kill a run at any point and re-run the identical command (--yes included) "
            "to resume; a round that already finished under the identical configuration "
            "is a no-op success, and one finished under another is refused. One driver "
            "per results tree (<loop-dir>/.driver.lock).\n"
            "Rounds greater than 0 are loop 2 and are refused; see the module "
            "docstring for the full list of what is not wired.\n"
            "exit codes:\n"
            f"  {EXIT_OK}  the phase finished (or was already finished)\n"
            f"  {EXIT_REFUSED}  refused — the message names the missing piece\n"
            f"  {EXIT_INTERRUPTED}  interrupted; re-run the same command to resume\n"
        ),
    )
    parser.add_argument(
        "--loop-slug",
        required=True,
        help="names <results-root>/loop-<slug>/; the primary key, never changed once written",
    )
    parser.add_argument(
        "--harness-root",
        type=Path,
        required=True,
        help="benchmark drivers and artifact dumpers, outside every attempt workspace",
    )
    parser.add_argument(
        "--reference-root",
        type=Path,
        required=True,
        help="pinned answers, outside every attempt workspace",
    )
    parser.add_argument(
        "--scaffold-repo",
        type=Path,
        required=True,
        help="the frozen turing-skills fork; its HEAD is recorded as the engine's "
        "scaffold_git_sha (scripts/bootstrap-turing-skills.sh)",
    )
    parser.add_argument(
        "--seeds",
        type=int,
        nargs="+",
        default=list(DEFAULT_SEEDS),
        help="noise-floor seeds (>=3, distinct); the first also seeds round 0",
    )
    parser.add_argument(
        "--round",
        type=int,
        default=0,
        dest="round_index",
        help="which round to drive; only 0 is reachable in loop 1",
    )
    parser.add_argument(
        "--noise-floor-only",
        action="store_true",
        help="measure the floor and stop, without driving round 0",
    )
    parser.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="drive every attempt from scratch instead of reusing finished work",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "assemble and validate every component (corpus, scaffold sha, backend "
            "client, both configs) and print the plan, then exit without driving; "
            "creates nothing under the results root"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "acknowledge that this run spends subscription compute; without it a "
            "non-dry-run invocation refuses (exit 2) after preflight, having written "
            "nothing"
        ),
    )
    return parser


def _refuse_unbuilt_round(round_index: int) -> ContractViolationError:
    return ContractViolationError(
        f"--round {round_index} is not wired, and refusing is the honest answer rather "
        "than driving something that would look like a round. Two pieces are missing, "
        "either sufficient:\n"
        "  1. A round > 0 must compute its deltas against its parent's RoundRecord, and "
        "this driver does not load one: round-NN/round.json can be decoded "
        "(trajectory.decode_round_record, used only for the restart-after-finish no-op), "
        "but nothing here selects a parent, loads it and validates its lineage. Driving "
        "without a parent would produce a round with no lineage, which RoundConfig "
        "refuses anyway.\n"
        "  2. What makes round N differ from round 0 is loop 2's self-edit step, which "
        "is out of scope for loop 1 (docs/research-agent.md, 'What is deliberately NOT "
        "built yet'). Re-running round 0's scaffold under a new index would produce a "
        "second baseline, not a trajectory.\n"
        "Drive --round 0."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.round_index != 0:
            raise _refuse_unbuilt_round(args.round_index)
        return asyncio.run(_drive(args))
    except ContractViolationError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return EXIT_REFUSED
    except KeyboardInterrupt:
        sys.stderr.write(
            "interrupted. Finished attempts are on disk; re-run the identical command "
            "to resume from them.\n"
        )
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
