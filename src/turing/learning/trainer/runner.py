"""Trainer protocol, ``StubTrainer``, and a lazy-import ``RealLoraTrainer``.

The trainer turns a ``TrainingJob`` into a ``TrainingResult`` (an adapter
file on disk plus its sha256 and an eval score). Real training pulls in
torch+peft (and optionally unsloth); the stub is deterministic and fast
enough to drive end-to-end pipeline tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from turing.learning.trainer.job import TrainingJob

EmitFn = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class TrainingResult:
    adapter_path: Path
    sha256: str
    eval_score: float


class Trainer(Protocol):
    def train(
        self,
        job: TrainingJob,
        artifact_dir: Path,
        emit: Optional[EmitFn] = None,
    ) -> TrainingResult: ...


class StubTrainer:
    """A deterministic, dependency-free trainer for unit tests + dry runs."""

    def __init__(self, *, eval_score: float = 0.5) -> None:
        self._eval_score = eval_score

    def train(
        self,
        job: TrainingJob,
        artifact_dir: Path,
        emit: Optional[EmitFn] = None,
    ) -> TrainingResult:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        blob = f"stub-adapter:{job.job_id}:{job.base_model}:{job.method}".encode("utf-8")
        adapter_path = artifact_dir / f"{job.job_id}.adapter.bin"
        adapter_path.write_bytes(blob)
        if emit is not None:
            emit({"kind": "step", "step": 1, "loss": 1.0})
            emit({"kind": "complete", "eval_score": self._eval_score})
        return TrainingResult(
            adapter_path=adapter_path,
            sha256=hashlib.sha256(blob).hexdigest(),
            eval_score=self._eval_score,
        )


class RealLoraTrainer:
    """LoRA trainer. Imports torch lazily; runs a low-rank update on CPU.

    A LoRA adapter is a low-rank decomposition ``ΔW = B @ A`` applied on top
    of a frozen weight matrix ``W``. This implementation trains a toy ``B``
    and ``A`` against a fixed mean-squared target so the trainer's wiring
    (artifact path, sha256, eval score, emit hook) can be exercised end-to-end
    on a CPU fixture in CI without pulling a full HuggingFace model.

    The full Phase B/C trainers (slices 24/25) replace this implementation
    with peft + an actual base model.
    """

    def __init__(
        self, *, device: str = "cuda", rank: int = 4, dim: int = 32, steps: int = 5
    ) -> None:
        self._device = device
        self._rank = rank
        self._dim = dim
        self._steps = steps

    def train(
        self,
        job: TrainingJob,
        artifact_dir: Path,
        emit: Optional[EmitFn] = None,
    ) -> TrainingResult:
        import torch

        torch.manual_seed(0)
        device = torch.device(self._device)
        a = torch.zeros(self._rank, self._dim, device=device, requires_grad=True)
        b = torch.zeros(self._dim, self._rank, device=device, requires_grad=True)
        torch.nn.init.kaiming_uniform_(a, a=5**0.5)
        x = torch.randn(8, self._dim, device=device)
        target = torch.randn(8, self._dim, device=device)

        opt = torch.optim.SGD([a, b], lr=1e-2)
        loss_value = float("nan")
        for step in range(self._steps):
            opt.zero_grad()
            delta = x @ a.t() @ b.t()
            loss = ((delta - target) ** 2).mean()
            loss.backward()
            opt.step()
            loss_value = float(loss.detach())
            if emit is not None:
                emit({"kind": "step", "step": step + 1, "loss": loss_value})

        artifact_dir.mkdir(parents=True, exist_ok=True)
        adapter_path = artifact_dir / f"{job.job_id}.adapter.pt"
        torch.save({"A": a.detach().cpu(), "B": b.detach().cpu()}, adapter_path)
        blob = adapter_path.read_bytes()
        sha = hashlib.sha256(blob).hexdigest()
        if emit is not None:
            emit({"kind": "complete", "eval_score": loss_value})
        return TrainingResult(adapter_path=adapter_path, sha256=sha, eval_score=loss_value)
