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
import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .schema import EvalCase
from .scoring import SCORERS, ScoreResult

if TYPE_CHECKING:
    from .judge import Judge
    from .scoring import Embedder

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


def run(
    worker: WorkerFn,
    cases: list[EvalCase],
    *,
    embedder: Embedder | None = None,
    judge: Judge | None = None,
) -> dict:
    by_category: dict[str, list[ScoreResult]] = {}
    per_case = []
    for case in cases:
        scorer = SCORERS.get(case.scoring_fn)
        if scorer is None:
            raise ValueError(f"Unknown scoring_fn: {case.scoring_fn}")
        try:
            summary = worker(case)
            if case.scoring_fn == "score_claim_preservation_v1" and embedder is not None:
                result = scorer(summary, case, embedder=embedder)
            elif case.scoring_fn == "score_citation_correctness_v1" and judge is not None:
                result = scorer(summary, case, judge=judge)
            else:
                result = scorer(summary, case)
        except NotImplementedError as e:
            result = ScoreResult(score=0.0, notes=[f"scorer not implemented: {e}"])
        except Exception as e:  # any worker error -> score=0, don't crash the harness
            result = ScoreResult(
                score=0.0,
                notes=[f"worker error ({type(e).__name__}): {e}"],
            )
        by_category.setdefault(case.category, []).append(result)
        per_case.append({"id": case.id, "category": case.category, "score": result.score})

    cat_scores = {
        cat: sum(r.score for r in results) / len(results) for cat, results in by_category.items()
    }
    aggregate = sum(cat_scores.get(cat, 0.0) * w for cat, w in CATEGORY_WEIGHTS.items())
    return {"aggregate": aggregate, "by_category": cat_scores, "per_case": per_case}


def _build_direct_worker(model: str, timeout_s: float) -> WorkerFn:
    """Construct a direct worker against the configured cloud provider.

    Imports happen here (not at module top) so the harness keeps loading
    without an API key set when only the fixture worker is used.
    """
    import os

    from turing.llm.cloud import ClaudeProvider

    from .workers import DirectWorkerConfig, direct_worker

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY must be set for --worker direct")
    provider = ClaudeProvider(api_key=api_key, model=model)
    return direct_worker(
        provider=provider,
        config=DirectWorkerConfig(model=model, timeout_s=timeout_s),
    )


def _build_coordinator_worker(
    *,
    nats_url: str,
    worker_id: str | None,
    deadline_offset_s: float,
) -> WorkerFn:
    """Construct a coordinator worker connected to a live NATS coordinator.

    Like `_build_direct_worker`, imports are local so the harness keeps
    loading without nats-py / config when only `--worker fixture` runs.
    """
    import time

    from turing.coordinator.dispatch import SubtaskDispatchClient
    from turing.transport.nats_bus import NatsBus
    from turing.transport.signed_transport import SignedTransport
    from turing.transport.signer import MessageSigner

    from .workers import CoordinatorWorkerConfig, coordinator_worker

    async def _connect_and_build() -> WorkerFn:
        bus = await NatsBus.connect(url=nats_url, tls_enabled=True, nkey_seed=None, lan_only=True)
        signer = MessageSigner.generate()
        # Trust set wired up out-of-band in production (manifests advertise
        # public keys); for the eval harness we accept any signed reply by
        # adding the signer's own key as trusted (loopback) — replace with
        # a real trust list when this is deployed against the live pool.
        transport = SignedTransport(
            bus=bus,
            signer=signer,
            trusted_keys=[signer.public_key],
            now_ms=lambda: int(time.time() * 1000),
        )
        client = SubtaskDispatchClient(
            transport=transport,
            sender_id="eval-harness",
            now_ms=lambda: int(time.time() * 1000),
        )
        return coordinator_worker(
            client=client,
            config=CoordinatorWorkerConfig(
                specialty="research-summarize",
                worker_id=worker_id,
                deadline_offset_s=deadline_offset_s,
            ),
        )

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_connect_and_build())
    finally:
        loop.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", default="fixture", choices=["fixture", "direct", "coordinator"])
    parser.add_argument("--model", default="claude-sonnet-4-5")
    parser.add_argument("--timeout-s", type=float, default=60.0)
    parser.add_argument(
        "--url",
        default="nats://127.0.0.1:4222",
        help="NATS URL (used with --worker coordinator)",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="If set, dispatch via worker-direct subject; else specialty queue",
    )
    parser.add_argument("--deadline-offset-s", type=float, default=120.0)
    args = parser.parse_args()

    if args.worker == "direct":
        worker: WorkerFn = _build_direct_worker(args.model, args.timeout_s)
    elif args.worker == "coordinator":
        worker = _build_coordinator_worker(
            nats_url=args.url,
            worker_id=args.worker_id,
            deadline_offset_s=args.deadline_offset_s,
        )
    else:
        worker = fixture_worker

    cases = load_cases()
    report = run(worker, cases)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
