"""Torch DPO trainer wiring: stub backend round-trips through the registry."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from turing.coordinator.adapters.registry import AdapterRegistry
from turing.learning.trainer import (
    InMemoryObjectStore,
    TorchDPOTrainer,
    TrainerPublisher,
    TrainingJob,
)
from turing.transport.signer import MessageSigner

if TYPE_CHECKING:
    from pathlib import Path


def _job(*, dataset_path: Path) -> TrainingJob:
    return TrainingJob(
        job_id="dpo-1",
        dataset_url=f"file://{dataset_path}",
        dataset_sha256="0" * 64,
        base_model="qwen2.5-7b",
        method="dpo",
        hyperparameters={"beta": 0.1, "epochs": 1},
    )


def _stub_backend(*, job: TrainingJob, dataset_path: Path, out_path: Path) -> bytes:
    payload = (
        f"dpo[{job.base_model}|{dataset_path.read_bytes().decode('utf-8', errors='replace')}]"
    ).encode()
    out_path.write_bytes(payload)
    return payload


def test_torch_dpo_trainer_writes_adapter(tmp_path: Path) -> None:
    dataset = tmp_path / "pairs.jsonl"
    dataset.write_text(
        json.dumps({"prompt": "p", "chosen": "c", "rejected": "r"}) + "\n",
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "out"
    trainer = TorchDPOTrainer(backend=_stub_backend, dataset_path=dataset)
    result = trainer.train(_job(dataset_path=dataset), artifact_dir=artifact_dir)
    assert result.adapter_path.exists()
    assert result.sha256 == hashlib.sha256(result.adapter_path.read_bytes()).hexdigest()


def test_publisher_signs_manifest_registry_accepts(tmp_path: Path) -> None:
    dataset = tmp_path / "pairs.jsonl"
    dataset.write_text(
        json.dumps({"prompt": "p", "chosen": "c", "rejected": "r"}) + "\n",
        encoding="utf-8",
    )
    trainer = TorchDPOTrainer(backend=_stub_backend, dataset_path=dataset)
    result = trainer.train(_job(dataset_path=dataset), artifact_dir=tmp_path / "art")

    signer = MessageSigner.generate()
    publisher = TrainerPublisher(
        signer=signer, object_store=InMemoryObjectStore(), facility_name="dgx-1"
    )
    manifest = publisher.publish(_job(dataset_path=dataset), result, version="rs-dpo@v1")

    registry = AdapterRegistry(worker_base_model="qwen2.5-7b", trusted_issuers=[signer.public_key])
    registry.verify(manifest, result.adapter_path.read_bytes())


def test_real_backend_imports_torch_lazily(tmp_path: Path) -> None:
    """Constructing TorchDPOTrainer with backend=None must not fail when
    torch isn't installed locally — the import only happens when train()
    runs on the H100 facility."""
    dataset = tmp_path / "pairs.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    trainer = TorchDPOTrainer(backend=None, dataset_path=dataset)
    assert trainer is not None
