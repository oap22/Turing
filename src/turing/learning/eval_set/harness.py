"""Aggregate eval harness.

Loads JSONL cases from ``evals/<specialty>/``, runs each one against a worker
callable, and emits per-case + per-axis + aggregate scores. The worker
callable is injected: tests pass a fake; production wiring (real-worker
dispatch via the runtime bus) is the follow-up that hooks this harness into
the runtime.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from turing.learning.eval_set.case import EvalCase
from turing.learning.eval_set.judge import claim_recall
from turing.learning.eval_set.scorers import citation_exact_match, voice_cosine

WorkerFn = Callable[[EvalCase], Awaitable[str]]
EmbedFn = Callable[[str], Sequence[float]]


@dataclass(frozen=True)
class CaseReport:
    case_id: str
    output: str
    axis_scores: dict[str, float]
    aggregate: float


@dataclass(frozen=True)
class EvalReport:
    case_count: int
    aggregate: float
    per_axis: dict[str, float]
    per_case: list[CaseReport] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "case_count": self.case_count,
                "aggregate": self.aggregate,
                "per_axis": self.per_axis,
                "per_case": [
                    {
                        "case_id": r.case_id,
                        "output": r.output,
                        "axis_scores": r.axis_scores,
                        "aggregate": r.aggregate,
                    }
                    for r in self.per_case
                ],
            },
            indent=2,
        )


async def run_eval_set(
    *,
    cases_path: Path,
    worker: WorkerFn,
    judge: Any,
    voice_refs: Mapping[str, Sequence[float]],
    embed: EmbedFn,
) -> EvalReport:
    cases = _load_cases(cases_path)
    case_reports: list[CaseReport] = []
    axis_buckets: dict[str, list[float]] = {}

    for case in cases:
        output = await worker(case)
        scores: dict[str, float] = {}

        # Axis: citation_correctness — always run, even if not declared on
        # the case, so an output that hallucinates citations gets caught.
        if "citation_correctness" in case.axes or case.expected_citations:
            scores["citation_correctness"] = citation_exact_match(output, case.expected_citations)

        # Axis: claim_preservation — uses the LLM judge.
        if "claim_preservation" in case.axes or case.expected_claims:
            scores["claim_preservation"] = await claim_recall(
                output=output,
                expected_claims=case.expected_claims,
                llm=judge,
            )

        # Axis: voice_match — only when the case has a resolvable
        # voice_ref_id, since the voice anchor is operator-curated and
        # cases ship without one until preferences accumulate.
        if case.voice_ref_id is not None and case.voice_ref_id in voice_refs:
            scores["voice_match"] = voice_cosine(
                output,
                voice_refs[case.voice_ref_id],
                embed=embed,
            )

        case_aggregate = statistics.fmean(scores.values()) if scores else 0.0
        case_reports.append(
            CaseReport(
                case_id=case.id,
                output=output,
                axis_scores=scores,
                aggregate=case_aggregate,
            )
        )
        for axis, value in scores.items():
            axis_buckets.setdefault(axis, []).append(value)

    per_axis = {axis: statistics.fmean(vals) for axis, vals in axis_buckets.items()}
    aggregate = statistics.fmean([r.aggregate for r in case_reports]) if case_reports else 0.0
    return EvalReport(
        case_count=len(case_reports),
        aggregate=aggregate,
        per_axis=per_axis,
        per_case=case_reports,
    )


def _load_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        cases.append(EvalCase.model_validate(json.loads(line)))
    return cases
