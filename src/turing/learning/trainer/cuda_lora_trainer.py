"""CUDA LoRA SFT trainer for the H100/DGX facility (Phase B).

ADR 0009 retargets the Phase B SFT trainer from "MLX on the MacBook Pro" to
**CUDA LoRA on the H100/DGX**: the MacBook is retired and ``mlx_lm`` is
Apple-Silicon-only, so it cannot move to Jetson/H100 CUDA. Phase B (SFT) and
Phase C (DPO) now share the same H100 home — the distinction is SFT vs DPO,
not MBP vs H100.

The real backend uses peft's LoRA on top of a CUDA ``torch`` model — both
heavyweight, GPU-only dependencies that ship **only** on the training facility.
The backend is injected so the rest of the cluster (coordinator, Jetson
workers, CI) can import this module and hold a ``Trainer`` reference without
ever installing torch/peft or owning a GPU; tests pass an explicit stub.

The ADR 0009 LoRA guardrails this trainer is built to honour (the concrete
hyperparameter recipe is a separate slice): **retrain the LoRA from the base
model each cycle** on the accumulated dataset rather than stacking adapters,
**LoRA on all linear layers** at a modest rank, and **few epochs** to guard
small-data memorisation.

Output is a checkpoint blob the existing :class:`TrainerPublisher` signs into a
manifest the :class:`AdapterRegistry` accepts after SHA256 + Ed25519
verification — same chain as the DPO trainer, different backend.
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


class CudaLoraTrainer:
    """Phase B SFT trainer; lazy-imports ``peft`` + CUDA ``torch`` on ``train()``.

    ``backend=None`` selects the real CUDA LoRA path, lazy-imported on the
    H100/DGX where the full peft+torch stack is installed. Tests pass an
    explicit stub so the wiring is verifiable without a GPU.
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
            eval_score=0.0,  # populated by the eval-set harness, not the trainer
        )

    @staticmethod
    def _real_backend() -> BackendFn:
        def run(*, job, dataset_path, out_path):  # type: ignore[no-untyped-def]
            # Lazy import — only happens on the H100/DGX facility where the
            # CUDA torch + peft stack is installed. Keeps the module importable
            # on the coordinator, the Jetson workers, and in CI.
            import peft  # type: ignore[import-not-found]  # noqa: F401  # pragma: no cover
            import torch  # type: ignore[import-not-found]  # pragma: no cover

            if not torch.cuda.is_available():  # pragma: no cover
                raise RuntimeError(
                    "CudaLoraTrainer requires a CUDA device; this path runs only "
                    "on the H100/DGX training facility"
                )

            raise NotImplementedError(  # pragma: no cover
                "real CUDA LoRA SFT backend not wired in this slice; pass an "
                "explicit backend, or wait for the runtime-bus integration that "
                "hooks the SFT entry point through"
            )

        return run
