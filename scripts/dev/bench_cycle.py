#!/usr/bin/env python3
"""Run the bench cycle: two software-only turns of the research flywheel (#360).

Every seam is real (queues, dispatch, inbox, curation, rewards, dataset
builders, adapter registry, canary gate); only the worker LLM, the trainer,
and the operator are synthetic. Side effects (vault git commits, file moves,
reward rows) land entirely under --output — never point this at a real vault.

Usage:
    python scripts/dev/bench_cycle.py [--output data/bench-cycle]

Exits 0 when every seam invariant held; prints the invariant checklist and
where to find the inspectable artifacts (inbox trees, curated vault + git
log, datasets, adapter manifests, report.json).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from turing.coordinator.flywheel.bench_cycle import (
    BenchCycle,
    BenchInvariantError,
)


async def run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output).resolve()
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        print(
            f"refusing to run: {output_dir} exists and is not an empty directory", file=sys.stderr
        )
        print("pick a fresh --output dir (the bench owns everything under it)", file=sys.stderr)
        return 2

    print(f"bench cycle starting → {output_dir}")
    try:
        report = await BenchCycle(output_dir=output_dir).run()
    except BenchInvariantError as exc:
        print(f"\nBENCH FAILED — seam invariant did not hold:\n  {exc}", file=sys.stderr)
        print(f"partial artifacts left under {output_dir} for inspection", file=sys.stderr)
        return 1

    print(f"\nall {len(report.invariants)} seam invariants held:")
    for name in report.invariants:
        print(f"  ✓ {name}")
    print("\ninspectable artifacts:")
    print(f"  vault (git log = curation audit trail): {report.output_dir / 'vault-repo'}")
    print(f"  datasets + adapter manifests:           {report.output_dir / 'artifacts'}")
    print(f"  held-out eval set:                      {report.output_dir / 'evals'}")
    print(f"  full report:                            {report.report_path}")
    notice = report.summary.get("rejection_notice")
    if notice:
        print("\ncycle-2 rejection notice (as it reaches the morning-review feed):")
        for line in str(notice).splitlines():
            print(f"  {line}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the bench cycle: two software-only turns of the research flywheel (#360)."
    )
    parser.add_argument(
        "--output",
        default="data/bench-cycle",
        help="fresh directory the bench owns (default: data/bench-cycle)",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
