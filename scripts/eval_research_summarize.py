"""CLI: run the research-summarize eval set against a worker.

Usage:
    python scripts/eval_research_summarize.py \\
        --cases evals/research-summarize/cases.jsonl \\
        --fake-worker

The ``--fake-worker`` flag is the safe default while real-worker dispatch
isn't wired through the runtime bus. When the wiring lands a follow-up will
add ``--worker <specialty>`` and dial through NATS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Make ``turing`` importable when running from a checkout without `pip install -e`.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from turing.learning.eval_set import EvalCase, run_eval_set  # noqa: E402


async def _fake_worker(case: EvalCase) -> str:
    """Echo every expected citation so the citation axis scores 1.0
    for the smoke test. This lets the harness verify wiring without
    a real worker."""
    return f"output for {case.id}: " + " ".join(case.expected_citations)


class _FakeJudge:
    """Stand-in judge that says everything was recalled — keeps the smoke
    test deterministic without dialing the cloud LLM."""

    async def complete(self, messages: list[Any], system: str = "", **_: Any) -> Any:
        # Try to recall every claim mentioned in the user prompt.
        from turing.llm.base import LLMResponse

        user = messages[0].content if messages else ""
        # Best-effort parse of the expected_claims list out of the prompt.
        recalled: list[str] = []
        try:
            start = user.index("[")
            end = user.index("]", start)
            recalled = json.loads(user[start : end + 1])
        except (ValueError, json.JSONDecodeError):
            pass
        return LLMResponse(
            content=json.dumps({"recalled": recalled, "missing": []}),
            model="fake-judge",
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="eval_research_summarize")
    parser.add_argument(
        "--cases",
        required=True,
        type=Path,
        help="Path to evals/<specialty>/*.jsonl",
    )
    parser.add_argument(
        "--fake-worker",
        action="store_true",
        help="Use the fake worker stand-in (default until real-worker dispatch wires through)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional path to also write the JSON report",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.cases.is_file():
        print(f"cases file not found: {args.cases}", file=sys.stderr)
        return 2
    if not args.fake_worker:
        print(
            "real-worker dispatch not yet wired; pass --fake-worker for now",
            file=sys.stderr,
        )
        return 2

    report = await run_eval_set(
        cases_path=args.cases,
        worker=_fake_worker,
        judge=_FakeJudge(),
        voice_refs={},
        embed=lambda text: [float(text.count(c)) for c in "abcdefghijklmnop"],
    )
    body = report.to_json()
    print(body)
    if args.out is not None:
        args.out.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))
