"""Torch + peft DPO trainer for the H100 facility (Phase C).

The real backend uses ``trl.DPOTrainer`` on top of peft's LoRA — both
heavyweight dependencies that only ship on the training facility. The
backend is injected so the rest of the cluster (the coordinator, workers,
CI) can hold a reference to the trainer module without ever installing
torch.

The output blob is a checkpoint the existing TrainerPublisher (slice 23)
signs into a manifest the AdapterRegistry verifies — same chain as the
MLX trainer (slice 24), different backend.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from turing.learning.trainer.runner import TrainingResult

if TYPE_CHECKING:
    from pathlib import Path

    from turing.learning.trainer.job import TrainingJob

BackendFn = Callable[..., bytes]
"""``backend(*, job, dataset_path, out_path) -> blob_bytes``."""


class TorchDPOTrainer:
    """Phase C DPO trainer; lazy-imports ``trl`` + ``peft`` on first ``train()``."""

    def __init__(
        self,
        *,
        backend: BackendFn | None = None,
        dataset_path: Path,
    ) -> None:
        self._backend = backend
        self._dataset_path = dataset_path

    def train(
        self,
        job: TrainingJob,
        artifact_dir: Path,
        emit: Callable[[dict], None] | None = None,
    ) -> TrainingResult:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        out_path = artifact_dir / f"{job.job_id}.dpo.safetensors"

        backend = self._backend if self._backend is not None else self._real_backend()

        if emit is not None:
            emit({"kind": "started", "job_id": job.job_id})
        blob = backend(
            job=job,
            dataset_path=self._dataset_path,
            out_path=out_path,
        )
        sha = hashlib.sha256(blob).hexdigest()
        if emit is not None:
            emit({"kind": "complete", "sha256": sha})
        return TrainingResult(
            adapter_path=out_path,
            sha256=sha,
            eval_score=0.0,  # populated by the eval harness, not the trainer
        )

    @staticmethod
    def _real_backend() -> BackendFn:
        def run(*, job, dataset_path, out_path):  # type: ignore[no-untyped-def]
            # Lazy import — only happens on the H100 facility where the
            # full peft+trl stack is installed.
            import peft  # type: ignore[import-not-found]  # noqa: F401  # pragma: no cover
            import torch  # type: ignore[import-not-found]  # noqa: F401  # pragma: no cover
            from trl import (
                DPOTrainer,  # type: ignore[import-not-found]  # noqa: F401  # pragma: no cover
            )

            raise NotImplementedError(
                "real torch+peft DPO backend not wired in this slice; pass an "
                "explicit backend, or wait for the runtime-bus integration "
                "that hooks the DPO entry point through"
            )

        return run
