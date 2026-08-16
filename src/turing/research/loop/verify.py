"""``python -m turing.research.loop.verify`` — check the metrics chain, offline.

Walks a path for every directory holding a ``metrics.jsonl`` — one attempt,
one round, one loop, or an entire results root — and runs both
:func:`~turing.research.loop.integrity.verify_metrics_chain` and
:func:`~turing.research.loop.integrity.reconcile_summary` against each one.
Exit code ``0`` means every run it found passed both checks; anything else,
including finding **no** runs at all, is ``1``. An operator wiring this into a
pre-writeup check needs the exit code to mean something, and an empty
directory reporting success would be exactly backwards: it is not "verified",
it is "nothing was checked".

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
from pathlib import Path
from typing import TYPE_CHECKING

from turing.research.loop.integrity import reconcile_summary, verify_metrics_chain

if TYPE_CHECKING:
    from collections.abc import Sequence

    from turing.research.loop.integrity import ChainVerdict, ReconcileVerdict

__all__ = ["HONESTY_LINE", "RunVerdict", "build_parser", "find_runs", "main", "verify_run"]

#: Printed verbatim on every invocation, human or ``--json``. Not decoration —
#: see the module docstring for why a bare ``OK`` would overclaim.
HONESTY_LINE = "detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11"


@dataclasses.dataclass(frozen=True, slots=True)
class RunVerdict:
    """Both checks' results for one run directory, plus the combined verdict.

    ``ok`` is ``chain.ok and reconcile.ok`` — a run with an intact chain but a
    doctored summary, or vice versa, is not a passing run. ``chain`` and
    ``reconcile`` are ``None`` only for the synthetic "no runs found at all"
    record :func:`main` emits when a search turns up nothing; a real run
    directory always has both.
    """

    path: str
    ok: bool
    chain: ChainVerdict | None
    reconcile: ReconcileVerdict | None
    reason: str | None = None


def find_runs(root: Path) -> list[Path]:
    """Every directory under (and including) ``root`` that holds a ``metrics.jsonl``.

    ``Path.rglob`` on ``"metrics.jsonl"`` matches at every depth, including
    depth zero, so pointing this at a single attempt directory works exactly
    like pointing it at a whole results root — the caller does not need to
    know which kind of directory it was handed. Sorted for a deterministic
    report across runs of this tool.
    """
    return sorted({path.parent for path in root.rglob("metrics.jsonl")})


async def verify_run(directory: Path) -> RunVerdict:
    """Run both checks against one directory and combine them into one verdict."""
    chain, reconcile = await asyncio.gather(
        verify_metrics_chain(directory), reconcile_summary(directory)
    )
    return RunVerdict(
        path=str(directory),
        ok=chain.ok and reconcile.ok,
        chain=chain,
        reconcile=reconcile,
    )


async def _verify_all(root: Path) -> list[RunVerdict]:
    run_dirs = find_runs(root)
    if not run_dirs:
        return [
            RunVerdict(
                path=str(root),
                ok=False,
                chain=None,
                reconcile=None,
                reason=f"no metrics.jsonl found under {root}",
            )
        ]
    return list(await asyncio.gather(*(verify_run(directory) for directory in run_dirs)))


def _format_human(verdict: RunVerdict) -> str:
    if verdict.ok:
        lines_checked = verdict.chain.lines_checked if verdict.chain is not None else 0
        return f"{verdict.path}: OK ({lines_checked} line(s) checked)"

    if verdict.chain is None and verdict.reconcile is None:
        return f"{verdict.path}: FAIL {verdict.reason}"

    parts: list[str] = []
    assert verdict.chain is not None
    assert verdict.reconcile is not None
    if not verdict.chain.ok:
        parts.append(
            f"chain: {verdict.chain.reason} (first_bad_index={verdict.chain.first_bad_index})"
        )
    if not verdict.reconcile.ok:
        mismatches = list(verdict.reconcile.mismatches)
        parts.append(f"summary: {verdict.reconcile.reason} (mismatches={mismatches})")
    return f"{verdict.path}: FAIL " + "; ".join(parts)


def _to_json_record(verdict: RunVerdict) -> dict[str, object]:
    record: dict[str, object] = {"path": verdict.path, "ok": verdict.ok, "note": HONESTY_LINE}
    if verdict.chain is None and verdict.reconcile is None:
        record["reason"] = verdict.reason
        return record
    assert verdict.chain is not None
    assert verdict.reconcile is not None
    record["chain"] = {
        "ok": verdict.chain.ok,
        "lines_checked": verdict.chain.lines_checked,
        "first_bad_index": verdict.chain.first_bad_index,
        "reason": verdict.chain.reason,
    }
    record["reconcile"] = {
        "ok": verdict.reconcile.ok,
        "mismatches": list(verdict.reconcile.mismatches),
        "reason": verdict.reconcile.reason,
    }
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turing-research-verify",
        description=(
            "Verify the metrics hash chain and summary reconciliation for every "
            "research-loop run found under a path (one attempt, one round, one "
            "loop, or an entire results root)."
        ),
        epilog=f"Every invocation prints the qualifier verbatim: {HONESTY_LINE!r}.",
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
        return 1
    if not root.is_dir():
        sys.stderr.write(f"{root}: not a directory\n")
        sys.stdout.write(HONESTY_LINE + "\n")
        return 1

    verdicts = asyncio.run(_verify_all(root))

    if args.json:
        for verdict in verdicts:
            sys.stdout.write(json.dumps(_to_json_record(verdict), sort_keys=False) + "\n")
    else:
        for verdict in verdicts:
            sys.stdout.write(_format_human(verdict) + "\n")

    sys.stdout.write(HONESTY_LINE + "\n")

    return 0 if all(verdict.ok for verdict in verdicts) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
