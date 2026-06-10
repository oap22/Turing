"""End-to-end bench cycle (#360): two software-only flywheel turns.

The runner itself enforces every seam invariant as a hard assertion
(:class:`BenchInvariantError`), so the heavy lifting is "the run completes".
This wrapper pins the contract from the outside: the invariant checklist
covers the issue's minimum set, and the artifacts an operator would inspect
actually exist in the sandbox.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from turing.coordinator.flywheel.bench_cycle import BenchCycle

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.slow
@pytest.mark.asyncio
async def test_bench_cycle_runs_two_cycles_and_every_seam_invariant_holds(
    tmp_path: Path,
) -> None:
    report = await BenchCycle(output_dir=tmp_path / "bench").run()

    # The issue's minimum invariant set, by checklist prefix.
    held = "\n".join(report.invariants)
    for needle in (
        "dataset strictly grows; cycle-1 pairs byte-identical in cycle-2 build",
        "adapter built from base, not from the cycle-1 adapter",
        "duplicate question across cycles deduped in the frontier",
        "curation verb wrote its MORNING_CURATION reward row",
        "declined proposal never reaches the runnable queue",
        "approved proposal promoted with origin provenance",
        "edited draft's SFT target is the corrected answer",
        "stub adapter passes canary and goes LIVE",
        "cycle-2 canary fails the -0.5pp rule",
        "cycle-2 adapter is permanently REJECTED",
        "canary reverts to the cycle-1 adapter",
        "worst-failed eval cases archived as hard_examples",
        "notice reaches the morning-review/webui feed",
        "hard_examples present in the next dataset build input",
    ):
        assert needle in held, f"missing invariant: {needle}"

    # The artifact report is inspectable.
    out = report.output_dir
    assert report.report_path.exists()
    summary = json.loads(report.report_path.read_text(encoding="utf-8"))
    assert summary["cycle2"]["canary"]["status"] == "regression"
    assert (out / "artifacts" / "dataset-sft-v1.jsonl").exists()
    assert (out / "artifacts" / "dataset-sft-v2.jsonl").exists()
    assert (out / "artifacts" / "manifest-v2.json").exists()

    # Accumulate-never-replace, verified from the bytes on disk.
    v1 = (out / "artifacts" / "dataset-sft-v1.jsonl").read_bytes()
    v2 = (out / "artifacts" / "dataset-sft-v2.jsonl").read_bytes()
    assert v2.startswith(v1) and len(v2) > len(v1)

    # The vault git log is the curation audit trail: one commit per promotion
    # (3 accepts + 2 edits across both cycles) on top of the seed commit.
    log = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=out / "vault-repo",
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    expected_accepts = 3  # c1: q-c1-accept; c2: promoted_id, q-c2-probe-a
    expected_edits = 2  # c1: q-c1-edit; c2: frontier_ids[0]
    assert log.count("vault: curate accept") == expected_accepts, log
    assert log.count("vault: curate edit") == expected_edits, log
