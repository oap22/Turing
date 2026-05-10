"""Tests for the Trainer protocol and stub implementation.

The real LoRA path uses torch+peft which aren't a hard dep; the in-tree
``StubTrainer`` lets the rest of the trainer pipeline be exercised in unit
tests, and a separate ``slow``-marked integration test imports torch lazily
and skips if unavailable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from turing.learning.trainer.job import TrainingJob
from turing.learning.trainer.runner import StubTrainer, TrainingResult

if TYPE_CHECKING:
    from pathlib import Path


def _job(method: str = "sft") -> TrainingJob:
    return TrainingJob(
        job_id="j-x",
        dataset_url="file:///dev/null",
        dataset_sha256="0" * 64,
        base_model="qwen2.5-7b",
        method=method,
        hyperparameters={"lr": 1e-4},
    )


class TestStubTrainer:
    def test_writes_adapter_blob_to_artifact_dir(self, tmp_path: Path) -> None:
        trainer = StubTrainer()
        result = trainer.train(_job(), artifact_dir=tmp_path)
        assert isinstance(result, TrainingResult)
        assert result.adapter_path.exists()
        assert result.adapter_path.parent == tmp_path

    def test_result_includes_sha256_and_eval_score(self, tmp_path: Path) -> None:
        trainer = StubTrainer()
        result = trainer.train(_job(), artifact_dir=tmp_path)
        assert len(result.sha256) == 64
        assert isinstance(result.eval_score, float)

    def test_metrics_logged_via_emit_callback(self, tmp_path: Path) -> None:
        events: list[dict] = []
        trainer = StubTrainer()
        trainer.train(_job(), artifact_dir=tmp_path, emit=events.append)
        kinds = {e.get("kind") for e in events}
        assert "step" in kinds or "complete" in kinds


@pytest.mark.slow
class TestRealLoraTrainer:
    """Real LoRA on a CPU fixture. Skipped when torch+peft are unavailable."""

    def test_completes_end_to_end_on_cpu_fixture(self, tmp_path: Path) -> None:
        torch = pytest.importorskip("torch")
        pytest.importorskip("peft")
        from turing.learning.trainer.runner import RealLoraTrainer

        # Tiny CPU fixture: 2-token sequences, 1 layer.
        trainer = RealLoraTrainer(device="cpu")
        result = trainer.train(_job(), artifact_dir=tmp_path)
        assert result.adapter_path.exists()
        assert torch is not None
