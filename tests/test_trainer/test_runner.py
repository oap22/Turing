"""Tests for the Trainer protocol and stub implementation.

The real LoRA path uses torch+peft which aren't a hard dep; the in-tree
``StubTrainer`` lets the rest of the trainer pipeline be exercised in unit
tests, and a separate ``slow``-marked integration test imports torch lazily
and skips if unavailable.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

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


# ── RealLoraTrainer with a mocked torch ────────────────────────────────────
#
# torch/peft are not a hard dependency, so the `slow` test above skips in CI.
# To exercise RealLoraTrainer.train's wiring (artifact path, sha256, emit hook,
# eval score) on every run without a real model load, we inject a tiny fake
# `torch` into sys.modules. The fake honours just the surface train() touches.


class _FakeTensor:
    """A no-op tensor: every op returns a tensor; float() yields a fixed loss."""

    def __init__(self, value: float = 0.125) -> None:
        self._value = value

    def t(self) -> _FakeTensor:
        return self

    def detach(self) -> _FakeTensor:
        return self

    def cpu(self) -> _FakeTensor:
        return self

    def mean(self) -> _FakeTensor:
        return self

    def backward(self) -> None:
        return None

    def __matmul__(self, other: object) -> _FakeTensor:
        return self

    def __rmatmul__(self, other: object) -> _FakeTensor:
        return self

    def __sub__(self, other: object) -> _FakeTensor:
        return self

    def __pow__(self, other: object) -> _FakeTensor:
        return self

    def __float__(self) -> float:
        return self._value


def _make_fake_torch(*, save_raises: bool = False) -> ModuleType:
    mod = ModuleType("torch")

    def _save(obj: object, path: object) -> None:
        if save_raises:
            raise RuntimeError("disk full")
        from pathlib import Path

        Path(path).write_bytes(b"fake-adapter-bytes")

    mod.manual_seed = lambda seed: None  # type: ignore[attr-defined]
    mod.device = lambda spec: spec  # type: ignore[attr-defined]
    mod.zeros = lambda *a, **k: _FakeTensor()  # type: ignore[attr-defined]
    mod.randn = lambda *a, **k: _FakeTensor()  # type: ignore[attr-defined]
    mod.save = _save  # type: ignore[attr-defined]
    mod.nn = SimpleNamespace(init=SimpleNamespace(kaiming_uniform_=lambda t, a=0.0: None))  # type: ignore[attr-defined]
    mod.optim = SimpleNamespace(  # type: ignore[attr-defined]
        SGD=lambda params, lr: SimpleNamespace(zero_grad=lambda: None, step=lambda: None)
    )
    return mod


class TestRealLoraTrainerMockedTorch:
    def test_train_completes_with_fake_torch(self, tmp_path: Path) -> None:
        from turing.learning.trainer.runner import RealLoraTrainer, TrainingResult

        with patch.dict(sys.modules, {"torch": _make_fake_torch()}):
            trainer = RealLoraTrainer(device="cpu", steps=3)
            result = trainer.train(_job(), artifact_dir=tmp_path)

        assert isinstance(result, TrainingResult)
        assert result.adapter_path.exists()
        assert result.adapter_path.suffix == ".pt"
        assert len(result.sha256) == 64
        # eval_score is the final loss surfaced by float(loss.detach()).
        assert result.eval_score == 0.125

    def test_emit_receives_step_and_complete_events(self, tmp_path: Path) -> None:
        from turing.learning.trainer.runner import RealLoraTrainer

        events: list[dict] = []
        with patch.dict(sys.modules, {"torch": _make_fake_torch()}):
            RealLoraTrainer(device="cpu", steps=2).train(
                _job(), artifact_dir=tmp_path, emit=events.append
            )

        kinds = [e["kind"] for e in events]
        assert kinds == ["step", "step", "complete"]
        assert events[0]["step"] == 1
        assert events[-1]["eval_score"] == 0.125

    def test_training_failure_propagates(self, tmp_path: Path) -> None:
        from turing.learning.trainer.runner import RealLoraTrainer

        # A failure inside the backend (here: torch.save) is not swallowed.
        with (
            patch.dict(sys.modules, {"torch": _make_fake_torch(save_raises=True)}),
            pytest.raises(RuntimeError, match="disk full"),
        ):
            RealLoraTrainer(device="cpu", steps=1).train(_job(), artifact_dir=tmp_path)
