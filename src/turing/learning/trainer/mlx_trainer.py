"""MLX LoRA SFT trainer (Phase B fast-iteration / fallback path).

Runs LoRA SFT for 7B-class models on Apple Silicon via Apple's ``mlx_lm``.
The backend is injected so non-macOS hosts (and CI) can drive the wiring
with a stub; the real backend is a thin wrapper that lazy-imports
``mlx_lm.lora`` on first ``train()`` call.

Output is a checkpoint blob the existing TrainerPublisher (slice 23) signs
into a manifest the AdapterRegistry (slice 20) accepts. Eval-gated
promotion happens through the gate from slice 22.
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


class MLXLoraTrainer:
    """Trainer protocol implementation backed by mlx_lm.

    ``backend=None`` selects the real mlx_lm path, lazy-imported. Tests
    pass an explicit stub so the wiring is verifiable without MLX.
    """

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
        out_path = artifact_dir / f"{job.job_id}.adapter.safetensors"

        if self._backend is None:
            backend = self._real_backend()
        else:
            backend = self._backend

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
            eval_score=0.0,  # populated by the eval-set harness, not the trainer
        )

    @staticmethod
    def _real_backend() -> BackendFn:
        def run(*, job, dataset_path, out_path):  # type: ignore[no-untyped-def]
            # Lazy import — only happens on Apple Silicon when train() runs.
            from mlx_lm import (
                lora as mlx_lora,  # type: ignore[import-not-found]  # noqa: F401  # pragma: no cover
            )

            raise NotImplementedError(
                "real mlx_lm backend not wired in this slice; pass an explicit "
                "backend or wait for the runtime-bus integration that hooks "
                "the SFT entry point through"
            )

        return run
