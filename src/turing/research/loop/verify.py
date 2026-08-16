"""``python -m turing.research.loop.verify`` — check the metrics chain, offline.

Walks a path for every directory holding a ``metrics.jsonl`` — one attempt,
one round, one loop, or an entire results root — and runs both
:func:`~turing.research.loop.integrity.verify_metrics_chain` and
:func:`~turing.research.loop.integrity.reconcile_summary` against each one.

**Exit codes — three, not two:**

* ``0`` — every run found is complete and clean.
* ``1`` — at least one run **failed**: a broken chain, or a summary that is
  present and disagrees with its log. Also the code for finding **no** runs
  at all.
* ``2`` — nothing failed, but at least one run is **incomplete**: an intact
  chain with no ``metrics.json`` beside it, or a chain that could not be read
  consistently because a writer is still appending to it.

``1`` wins over ``2`` when both are present — a real failure is the thing an
operator has to act on, and it must not be masked by an unfinished run
elsewhere in the tree.

``2`` exists because ``metrics.json`` is written once, when an attempt ends,
while the chain grows after every step. Verifying a round *while it runs*, or
verifying a results root containing an attempt that a closed subscription
window killed, therefore finds intact chains with no summary beside them —
through nobody's fault, and **permanently**, because the operator's honest
re-drive rotates that trio into ``prior-N/`` exactly as it found it. Reporting
those as ``1`` was a false alarm that never cleared, and this tool's whole
premise is that a verifier which cries wolf teaches its operator to ignore it.
Reporting them as ``0`` would be the opposite mistake: a pre-writeup gate must
still refuse to wave through numbers that are not final. Hence a third code
that is neither "all good" nor "something is wrong" — see
:class:`~turing.research.loop.integrity.ReconcileState`.

``2`` means *unfinished*, and only that. A round that finished having lost an
attempt to a contained harness crash is **not** unfinished, and used to report
``2`` anyway — permanently, since that attempt's directory would never gain a
summary — which made a pre-writeup gate demanding ``0`` unsatisfiable on a
tree where nothing was wrong that the round had not already disclosed. The
fix is at the emitter, as it was for the false ``1``: ``RoundRunner`` now
writes a terminal, log-derived ``metrics.json`` for a contained attempt (see
``runner._close_out_crashed_attempt``), so the state this tool reports is
decided by whether the round is over, not by which line of the runner raised.
Nothing here was loosened to achieve that — an intact chain with no summary
is still ``2``, which is exactly what a killed process leaves behind.

``2`` is also what a run **being written right now** reports, and that is the
second false ``1`` this tool has had to stop emitting. ``MetricsWriter``
appends its JSONL line before rewriting the sidecar, and rewrites that sidecar
by truncating it — so a verifier reading during either window sees an honest
attempt in a shape that reads as damage, and used to call it a broken chain.
Measured against a real writer, 1633 of 1640 checks reported ``FAIL`` on data
nobody had touched. Since this document positively invites verifying a round
while it runs, an operator met that routinely, which is exactly how a verifier
teaches its operator to ignore it.
:func:`~turing.research.loop.integrity.verify_metrics_chain` now re-reads
before believing such a failure — stable damage still FAILs, a writer that
finished in the meantime verifies clean, and a pair that keeps changing under
the verifier reports
:attr:`~turing.research.loop.integrity.ChainState.IN_FLIGHT`, which lands
here as ``2``. Nothing was loosened to get there: a tamper pinned to a line is
never re-read at all, and no re-read can turn a failing chain into a passing
one.

Finding **no** runs at all stays ``1`` rather than ``2``. It is not an
unfinished run; it is a path that contains no runs — almost always the wrong
path — and an empty directory reporting anything softer would be exactly
backwards: it is not "verified", it is "nothing was checked".

**What a clean result here does and does not mean.** This tool can only tell
you the metrics log was not casually altered after the fact, and that its
summary was not doctored on top of an honest log. It cannot tell you the
numbers inside are genuine — ``research/OPEN-QUESTIONS.md`` R2 records that
agent-authored code runs as the operator today, with nothing preventing it
from rewriting the chain and its header together, and even a perfectly
intact chain says nothing about whether a wrong verifier's numbers are
*meaningful*. That is why every invocation of this CLI, human-readable or
``--json``, ends with the qualifier verbatim: see :data:`HONESTY_LINE`. A bare
``OK`` would be read as proof of authenticity it cannot provide.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from turing.research.loop.integrity import (
    STAGING_DIR_NAME as _STAGING_DIR_NAME,
)
from turing.research.loop.integrity import (
    ChainState,
    ReconcileState,
    reconcile_summary,
    verify_metrics_chain,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from turing.research.loop.integrity import ChainVerdict, ReconcileVerdict

__all__ = [
    "EXIT_FAILED",
    "EXIT_INCOMPLETE",
    "EXIT_OK",
    "HONESTY_LINE",
    "RunState",
    "RunVerdict",
    "build_parser",
    "find_runs",
    "format_run_verdict",
    "main",
    "verify_run",
]

#: Printed verbatim on every invocation, human or ``--json``. Not decoration —
#: see the module docstring for why a bare ``OK`` would overclaim.
HONESTY_LINE = "detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11"

#: The process exit codes, named so a caller (a shell gate, a test) never has
#: to restate the contract as bare integers. See the module docstring.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INCOMPLETE = 2


class RunState(str, Enum):  # noqa: UP042
    """One run directory's combined verdict across both checks.

    Mirrors :class:`~turing.research.loop.integrity.ReconcileState` at the
    run level, because the two checks compose into the same three answers:
    the record is good, the record is wrong, or the record is not finished.
    """

    OK = "ok"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


@dataclasses.dataclass(frozen=True, slots=True)
class RunVerdict:
    """Both checks' results for one run directory, plus the combined verdict.

    ``state`` is the composition of the two checks, and the composition is
    asymmetric on purpose:

    * a broken chain is :attr:`RunState.FAILED` no matter what the summary
      says — including when the summary is merely absent. "Unfinished" is an
      explanation for a *missing* summary, never for a chain that does not
      recompute, and letting an absent summary downgrade a chain break would
      hand a tamperer a one-file delete that softens the verdict;
    * an intact chain with a present-but-disagreeing summary stays
      :attr:`RunState.FAILED`, exactly as before this state existed;
    * only an intact chain with **no** summary is
      :attr:`RunState.INCOMPLETE` — the shape of a run still going, or one
      killed before its single end-of-attempt summary write.

    ``ok`` is a derived property rather than a field so it cannot disagree
    with ``state``; it is ``True`` for :attr:`RunState.OK` alone, so a caller
    that only ever looks at ``ok`` treats an unfinished run as not-passing,
    which is the safe reading.

    ``chain`` and ``reconcile`` are ``None`` only for the synthetic "no runs
    found at all" record :func:`main` emits when a search turns up nothing; a
    real run directory always has both.
    """

    path: str
    state: RunState
    chain: ChainVerdict | None
    reconcile: ReconcileVerdict | None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """``True`` only for :attr:`RunState.OK` — never for INCOMPLETE."""
        return self.state is RunState.OK


def find_runs(root: Path) -> list[Path]:
    """Every directory under (and including) ``root`` that holds a ``metrics.jsonl``.

    ``Path.rglob`` on ``"metrics.jsonl"`` matches at every depth, including
    depth zero, so pointing this at a single attempt directory works exactly
    like pointing it at a whole results root — the caller does not need to
    know which kind of directory it was handed. Sorted for a deterministic
    report across runs of this tool.

    **Skips every candidate under a** :data:`~turing.research.loop.runner._STAGING_DIR_NAME`
    **directory.** ``runner._rotate_stale_metrics`` builds a rotation's
    destination inside ``attempts/<problem-id>/.rotating/`` before committing
    it, in one rename, to a numbered ``prior-N/``; a crash in that window can
    leave a real, honest ``metrics.jsonl`` sitting there with no summary
    beside it, because the commit that would give it one never landed. That
    directory is a rotation in progress, not a run — reporting it as one used
    to make an otherwise complete, verified attempt look INCOMPLETE (or, with
    a genuinely broken leftover, FAILED) on the strength of a file nobody
    but ``_rotate_stale_metrics`` was ever meant to read, and that the very
    next attempt into this directory heals on its own. The check is by path
    *segment*, not prefix, so a legitimate ``prior-N/`` — which this walk
    must keep finding — is never caught by it.

    The narrow corollary: if the crash landed on the final rename and the
    staged run was the *only* run under ``root``, this returns nothing and
    the CLI reports ``no metrics.jsonl found`` (exit 1) until the next
    attempt into that directory heals it. That is a "nothing checked" report,
    not a clean one, and it is honest about which.
    """
    return sorted(
        {
            path.parent
            for path in root.rglob("metrics.jsonl")
            if _STAGING_DIR_NAME not in path.relative_to(root).parts
        }
    )


def _combine(chain: ChainVerdict, reconcile: ReconcileVerdict) -> RunState:
    """Fold the two checks into one run state — see :class:`RunVerdict`.

    Order matters. Both *failures* are asked about first, so a chain that
    does not recompute is FAILED even when the summary is the thing that is
    missing: only the *summary's* absence is explained by "the attempt did
    not finish", and a killed process leaves a truncated line in a chain that
    then fails on its own terms, which is a real finding worth an operator's
    attention. Checking the incompletions first would let a deleted
    ``metrics.json`` quietly soften a broken chain from ``1`` to ``2``.

    :attr:`~turing.research.loop.integrity.ChainState.IN_FLIGHT` — the chain
    could not be read consistently because a writer is appending to it — is
    folded into INCOMPLETE, deliberately reusing the state that already
    means "nothing here is wrong, but these numbers are not final" rather
    than adding a fourth. It composes with the summary exactly as it should:
    a live attempt has no ``metrics.json`` either, so both halves say
    "unfinished" and the run reports ``2``. It never outranks a real
    failure — a *present, disagreeing* summary beside an in-flight chain is
    still FAILED, because a doctored summary is a finding about the data
    whatever the log is doing while it is read.
    """
    if chain.state is ChainState.FAILED:
        return RunState.FAILED
    if reconcile.state is ReconcileState.FAILED:
        return RunState.FAILED
    if chain.state is ChainState.IN_FLIGHT or reconcile.state is ReconcileState.INCOMPLETE:
        return RunState.INCOMPLETE
    return RunState.OK


async def verify_run(directory: Path) -> RunVerdict:
    """Run both checks against one directory and combine them into one verdict."""
    chain, reconcile = await asyncio.gather(
        verify_metrics_chain(directory), reconcile_summary(directory)
    )
    return RunVerdict(
        path=str(directory),
        state=_combine(chain, reconcile),
        chain=chain,
        reconcile=reconcile,
    )


async def _verify_all(root: Path) -> list[RunVerdict]:
    run_dirs = find_runs(root)
    if not run_dirs:
        # FAILED, not INCOMPLETE: nothing here is half-done, there is simply
        # nothing here — nearly always the wrong path. An operator who
        # mistyped a root must not get the gentler code reserved for "a real
        # run is still in flight".
        return [
            RunVerdict(
                path=str(root),
                state=RunState.FAILED,
                chain=None,
                reconcile=None,
                reason=f"no metrics.jsonl found under {root}",
            )
        ]
    return list(await asyncio.gather(*(verify_run(directory) for directory in run_dirs)))


def format_run_verdict(verdict: RunVerdict) -> str:
    """One human-readable line for one run — exactly what this CLI prints.

    Public because it is not only this CLI's output any more: the loop stamps
    a machine-readable verdict beside every attempt's summary
    (:func:`turing.research.loop.results.write_attempt_verdict`) so the
    desktop's metrics pane can show whether the curve it is drawing verifies,
    and the human sentence in that file has to be *this* sentence. A second
    formatter would drift, and the two would eventually describe the same
    verdict differently — the operator reading the badge and the operator
    reading the terminal must not be told different stories.
    """
    return _format_human(verdict)


def _format_human(verdict: RunVerdict) -> str:
    if verdict.state is RunState.OK:
        lines_checked = verdict.chain.lines_checked if verdict.chain is not None else 0
        return f"{verdict.path}: OK ({lines_checked} line(s) checked)"

    if verdict.state is RunState.INCOMPLETE:
        # The word INCOMPLETE leads, and the line says the chain is intact in
        # the same breath — an operator scanning a wall of output has to be
        # able to tell at a glance that this path is not an accusation. The
        # line count is kept in the same position as the OK line's so the two
        # read as the same shape of report.
        assert verdict.chain is not None
        assert verdict.reconcile is not None
        if verdict.chain.state is ChainState.IN_FLIGHT:
            # "chain intact" would be a claim this run has not earned: the
            # chain was never read consistently, because something is writing
            # it. Say that instead, in the same position, so the line still
            # reads as a status rather than an accusation.
            return (
                f"{verdict.path}: INCOMPLETE ({verdict.chain.lines_checked} line(s) read, "
                f"chain still being written) {verdict.chain.reason}"
            )
        return (
            f"{verdict.path}: INCOMPLETE ({verdict.chain.lines_checked} line(s) checked, "
            f"chain intact) {verdict.reconcile.reason}"
        )

    if verdict.chain is None and verdict.reconcile is None:
        return f"{verdict.path}: FAIL {verdict.reason}"

    parts: list[str] = []
    assert verdict.chain is not None
    assert verdict.reconcile is not None
    if not verdict.chain.ok:
        parts.append(
            f"chain: {verdict.chain.reason} (first_bad_index={verdict.chain.first_bad_index})"
        )
    if verdict.reconcile.state is ReconcileState.INCOMPLETE:
        # Reached only alongside a chain failure (an intact chain plus an
        # absent summary is INCOMPLETE, handled above). Say so inline rather
        # than listing the absent summary as if it were a second finding:
        # the chain break is what the operator has to act on.
        parts.append(f"summary: {verdict.reconcile.reason} [incomplete, not itself the failure]")
    elif not verdict.reconcile.ok:
        mismatches = list(verdict.reconcile.mismatches)
        parts.append(f"summary: {verdict.reconcile.reason} (mismatches={mismatches})")
    return f"{verdict.path}: FAIL " + "; ".join(parts)


def _to_json_record(verdict: RunVerdict) -> dict[str, object]:
    """One JSON object per run, for a machine consumer.

    ``ok`` is kept alongside the new ``state`` rather than replaced by it, and
    stays ``False`` for an incomplete run: a script written against the
    two-state output keys on ``ok`` and must not start reading "unfinished"
    as "good" the day this field arrives. ``state`` is what distinguishes
    ``"incomplete"`` from ``"failed"``, and it is present on every record —
    including the synthetic no-runs-found one, which is ``"failed"``.
    """
    record: dict[str, object] = {
        "path": verdict.path,
        "ok": verdict.ok,
        "state": verdict.state.value,
        "note": HONESTY_LINE,
    }
    if verdict.chain is None and verdict.reconcile is None:
        record["reason"] = verdict.reason
        return record
    assert verdict.chain is not None
    assert verdict.reconcile is not None
    record["chain"] = {
        "ok": verdict.chain.ok,
        # As with the run-level pair above: ``ok`` keeps its two-state
        # meaning for scripts written before this field existed, and stays
        # ``False`` for an in-flight read, while ``state`` is what
        # distinguishes "this chain is wrong" from "a writer is holding it
        # open". A consumer that keys on ``ok`` alone loses nothing and is
        # never told an unverified chain is good.
        "state": verdict.chain.state.value,
        "lines_checked": verdict.chain.lines_checked,
        "first_bad_index": verdict.chain.first_bad_index,
        "reason": verdict.chain.reason,
    }
    record["reconcile"] = {
        "ok": verdict.reconcile.ok,
        "state": verdict.reconcile.state.value,
        "mismatches": list(verdict.reconcile.mismatches),
        "reason": verdict.reconcile.reason,
    }
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turing-research-verify",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Verify the metrics hash chain and summary reconciliation for every "
            "research-loop run found under a path (one attempt, one round, one "
            "loop, or an entire results root)."
        ),
        epilog=(
            "exit codes:\n"
            f"  {EXIT_OK}  every run found is complete and clean\n"
            f"  {EXIT_FAILED}  at least one run FAILED: a broken chain, or a summary that is\n"
            "     present and disagrees with its log. Also: no runs found at all.\n"
            f"  {EXIT_INCOMPLETE}  nothing failed, but at least one run is INCOMPLETE: an intact\n"
            "     chain with no metrics.json beside it, i.e. an attempt still\n"
            "     running or killed before it finished, or a chain a writer is\n"
            "     still appending to. A real failure outranks\n"
            f"     an incomplete run, so {EXIT_FAILED} wins when both are present.\n"
            "\n"
            f"Every invocation prints the qualifier verbatim: {HONESTY_LINE!r}."
        ),
    )
    parser.add_argument(
        "path",
        type=Path,
        help="a results root, a round directory, or a single attempt directory",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON object per run instead of a human-readable line",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root: Path = args.path

    if not root.exists():
        sys.stderr.write(f"{root}: no such file or directory\n")
        sys.stdout.write(HONESTY_LINE + "\n")
        return EXIT_FAILED
    if not root.is_dir():
        sys.stderr.write(f"{root}: not a directory\n")
        sys.stdout.write(HONESTY_LINE + "\n")
        return EXIT_FAILED

    verdicts = asyncio.run(_verify_all(root))

    if args.json:
        for verdict in verdicts:
            sys.stdout.write(json.dumps(_to_json_record(verdict), sort_keys=False) + "\n")
    else:
        for verdict in verdicts:
            sys.stdout.write(_format_human(verdict) + "\n")

    sys.stdout.write(HONESTY_LINE + "\n")

    # A real failure outranks an incomplete run: an operator who fixes only
    # what the exit code told them about must never be steered to the
    # unfinished attempt while a broken chain sits unmentioned in the same
    # tree. Both are still named in the printed output above; only the single
    # integer has to choose.
    if any(verdict.state is RunState.FAILED for verdict in verdicts):
        return EXIT_FAILED
    if any(verdict.state is RunState.INCOMPLETE for verdict in verdicts):
        return EXIT_INCOMPLETE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
