"""Aggregate eval harness: load JSONL, run cases against a worker, score."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from turing.learning.eval_set import EvalCase, run_eval_set
from turing.llm.base import LLMResponse


def _write_cases(path: Path, cases: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(c) for c in cases) + "\n", encoding="utf-8")


def _case(
    *,
    case_id: str,
    expected_claims: list[str] | None = None,
    expected_citations: list[str] | None = None,
    voice_ref_id: str | None = None,
) -> dict:
    return {
        "id": case_id,
        "input": "summarise this paper",
        "source_refs": ["arxiv:2401.00001"],
        "expected_claims": expected_claims or [],
        "expected_citations": expected_citations or [],
        "voice_ref_id": voice_ref_id,
        "axes": ["claim_preservation", "citation_correctness"],
    }


def _judge(content: str) -> AsyncMock:
    fake = AsyncMock()
    fake.complete = AsyncMock(return_value=LLMResponse(content=content, model="judge"))
    return fake


@pytest.mark.asyncio
async def test_loads_jsonl_and_runs_each_case(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    _write_cases(
        cases_path,
        [
            _case(case_id="rs-001", expected_citations=["[1]"]),
            _case(case_id="rs-002", expected_citations=["[2]"]),
        ],
    )

    async def fake_worker(case: EvalCase) -> str:
        return f"output for {case.id} with [1] and [2]"

    report = await run_eval_set(
        cases_path=cases_path,
        worker=fake_worker,
        judge=_judge('{"recalled": [], "missing": []}'),
        voice_refs={},
        embed=lambda _t: [0.0] * 8,
    )
    assert report.case_count == 2
    assert report.aggregate >= 0.0
    assert {r.case_id for r in report.per_case} == {"rs-001", "rs-002"}


@pytest.mark.asyncio
async def test_per_axis_breakdown_in_report(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    _write_cases(
        cases_path,
        [_case(case_id="rs-001", expected_citations=["[1]"])],
    )

    async def fake_worker(case: EvalCase) -> str:
        return "no citation here"

    report = await run_eval_set(
        cases_path=cases_path,
        worker=fake_worker,
        judge=_judge('{"recalled": [], "missing": []}'),
        voice_refs={},
        embed=lambda _t: [0.0] * 8,
    )
    # Citation axis takes a 0.0 because the output omits [1]
    assert report.per_axis["citation_correctness"] == 0.0


@pytest.mark.asyncio
async def test_voice_axis_skipped_when_voice_ref_id_null(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    _write_cases(
        cases_path,
        [_case(case_id="rs-001", voice_ref_id=None)],
    )

    async def fake_worker(case: EvalCase) -> str:
        return "any output"

    report = await run_eval_set(
        cases_path=cases_path,
        worker=fake_worker,
        judge=_judge('{"recalled": [], "missing": []}'),
        voice_refs={},
        embed=lambda _t: [0.0] * 8,
    )
    # Skipped axes don't appear in the breakdown.
    assert "voice_match" not in report.per_axis


@pytest.mark.asyncio
async def test_voice_axis_present_when_voice_ref_resolves(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    _write_cases(
        cases_path,
        [_case(case_id="rs-001", voice_ref_id="ref-alpha")],
    )

    async def fake_worker(case: EvalCase) -> str:
        return "any output"

    report = await run_eval_set(
        cases_path=cases_path,
        worker=fake_worker,
        judge=_judge('{"recalled": [], "missing": []}'),
        voice_refs={"ref-alpha": [0.1] * 8},
        embed=lambda _t: [0.1] * 8,
    )
    assert "voice_match" in report.per_axis


@pytest.mark.asyncio
async def test_report_dumps_json(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    _write_cases(cases_path, [_case(case_id="rs-001")])

    async def fake_worker(case: EvalCase) -> str:
        return "x"

    report = await run_eval_set(
        cases_path=cases_path,
        worker=fake_worker,
        judge=_judge('{"recalled": [], "missing": []}'),
        voice_refs={},
        embed=lambda _t: [0.0] * 8,
    )
    body = report.to_json()
    parsed = json.loads(body)
    assert parsed["case_count"] == 1
    assert "aggregate" in parsed
    assert "per_axis" in parsed
    assert isinstance(parsed["per_case"], list)
