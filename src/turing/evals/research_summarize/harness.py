"""Eval harness CLI for `research-summarize`.

Loads every `cases/*.jsonl`, runs each case's prompt+sources through a worker
callable, applies the named scoring_fn, and prints a per-category breakdown
plus a single aggregate score in [0, 1].

The worker callable is pluggable so this can run against:
  - a live coordinator+worker (NATS dispatch)
  - a direct LLM provider (smoke test)
  - a fixture replay (CI)

Usage:
    python -m turing.evals.research_summarize.harness --worker fixture
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from .schema import EvalCase
from .scoring import SCORERS, ScoreResult

# Cases live alongside operator-curated data, not in the Python package.
# Layout: <repo_root>/evals/research-summarize/cases/*.jsonl
_REPO_ROOT = Path(__file__).resolve().parents[4]
CASES_DIR = _REPO_ROOT / "evals" / "research-summarize" / "cases"

# Category weights for aggregate. Tweak as priorities shift.
CATEGORY_WEIGHTS = {
    "claim_preservation": 0.45,
    "citation_correctness": 0.30,
    "voice_match": 0.25,
}

WorkerFn = Callable[[EvalCase], str]


def load_cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    for path in sorted(CASES_DIR.glob("*.jsonl")):
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                cases.append(EvalCase.model_validate_json(line))
    return cases


def fixture_worker(case: EvalCase) -> str:
    """Echoes the first must_contain claim. Useful only as a harness smoke test."""
    return " ".join(case.expected.must_contain_claims) or "(empty)"


def run(worker: WorkerFn, cases: list[EvalCase]) -> dict:
    by_category: dict[str, list[ScoreResult]] = {}
    per_case = []
    for case in cases:
        scorer = SCORERS.get(case.scoring_fn)
        if scorer is None:
            raise ValueError(f"Unknown scoring_fn: {case.scoring_fn}")
        try:
            summary = worker(case)
            result = scorer(summary, case)
        except NotImplementedError as e:
            result = ScoreResult(score=0.0, notes=[f"scorer not implemented: {e}"])
        by_category.setdefault(case.category, []).append(result)
        per_case.append({"id": case.id, "category": case.category, "score": result.score})

    cat_scores = {
        cat: sum(r.score for r in results) / len(results) for cat, results in by_category.items()
    }
    aggregate = sum(cat_scores.get(cat, 0.0) * w for cat, w in CATEGORY_WEIGHTS.items())
    return {"aggregate": aggregate, "by_category": cat_scores, "per_case": per_case}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", default="fixture", choices=["fixture"])
    args = parser.parse_args()

    workers = {"fixture": fixture_worker}
    cases = load_cases()
    report = run(workers[args.worker], cases)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
