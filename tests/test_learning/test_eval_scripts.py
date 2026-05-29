"""Smoke tests for the two CLI scripts.

We don't shell out — the scripts expose ``main(argv)`` so the tests drive
them as library code. That keeps the test suite Python-only and fast.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))


@pytest.mark.asyncio
async def test_eval_script_runs_against_fake_worker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        json.dumps(
            {
                "id": "rs-001",
                "input": "summarise",
                "source_refs": ["arxiv:2401.00001"],
                "expected_claims": [],
                "expected_citations": ["[1]"],
                "voice_ref_id": None,
                "axes": ["citation_correctness"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    import eval_research_summarize

    exit_code = await eval_research_summarize.main(["--cases", str(cases_path), "--fake-worker"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert exit_code == 0
    assert parsed["case_count"] == 1
    assert "aggregate" in parsed


def test_arxiv_miner_script_emits_jsonl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fixtures = tmp_path / "papers.json"
    fixtures.write_text(
        json.dumps(
            [
                {
                    "arxiv_id": "2401.00001",
                    "title": "Test paper",
                    "abstract": "A claim. Another claim.",
                    "body": "See [1] and [2].",
                }
            ]
        ),
        encoding="utf-8",
    )

    import mine_arxiv_eval_cases

    exit_code = mine_arxiv_eval_cases.main(["--fixtures", str(fixtures)])
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert exit_code == 0
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["id"].startswith("arxiv-")
    assert "[1]" in parsed["expected_citations"]


def test_eval_script_exits_nonzero_when_cases_missing(
    tmp_path: Path,
) -> None:
    # Async main; use asyncio.run() rather than get_event_loop(), which raises
    # "no current event loop" on a sync test after an async one (pytest-asyncio
    # unsets the loop on teardown) on Python 3.12+.
    import asyncio

    import eval_research_summarize

    exit_code = asyncio.run(
        eval_research_summarize.main(["--cases", str(tmp_path / "missing.jsonl"), "--fake-worker"])
    )
    assert exit_code != 0
