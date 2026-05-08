"""CLI: mine candidate EvalCase rows from arxiv abstracts.

Reads a JSON fixture (real arxiv-API integration is a follow-up; the schema
matches what the API returns) and emits one candidate EvalCase per paper as
JSONL on stdout. The operator hand-curates the candidates before checking
them into ``evals/research-summarize/``.

Usage:
    python scripts/mine_arxiv_eval_cases.py --fixtures fixtures.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from turing.learning.eval_set.arxiv_miner import (  # noqa: E402
    ArxivPaper,
    mine_eval_case,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mine_arxiv_eval_cases")
    parser.add_argument(
        "--fixtures",
        required=True,
        type=Path,
        help="Path to a JSON file holding [{arxiv_id, title, abstract, body}, ...]",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.fixtures.is_file():
        print(f"fixtures file not found: {args.fixtures}", file=sys.stderr)
        return 2
    raw = json.loads(args.fixtures.read_text(encoding="utf-8"))
    for entry in raw:
        paper = ArxivPaper(
            arxiv_id=entry["arxiv_id"],
            title=entry["title"],
            abstract=entry["abstract"],
            body=entry["body"],
        )
        case = mine_eval_case(paper)
        print(json.dumps(case))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
