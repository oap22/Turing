"""Tests for the trainer publisher: signs and publishes adapter manifests."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from turing.coordinator.adapters.manifest import AdapterManifest
from turing.coordinator.adapters.registry import AdapterRegistry
from turing.learning.trainer.job import TrainingJob
from turing.learning.trainer.object_store import InMemoryObjectStore
from turing.learning.trainer.publisher import TrainerPublisher
from turing.learning.trainer.runner import TrainingResult
from turing.transport.signer import MessageSigner, SignatureError

if TYPE_CHECKING:
    from pathlib import Path


def _job() -> TrainingJob:
    return TrainingJob(
        job_id="j-1",
        dataset_url="file:///dev/null",
        dataset_sha256="0" * 64,
        base_model="qwen2.5-7b",
        method="sft",
        hyperparameters={},
    )


def _result(tmp_path: Path, content: bytes = b"adapter-bytes") -> TrainingResult:
    p = tmp_path / "adapter.bin"
    p.write_bytes(content)
    return TrainingResult(
        adapter_path=p,
        sha256=hashlib.sha256(content).hexdigest(),
        eval_score=0.42,
    )


class TestPublish:
    def test_uploads_blob_and_returns_signed_manifest(self, tmp_path: Path) -> None:
        signer = MessageSigner.generate()
        store = InMemoryObjectStore()
        publisher = TrainerPublisher(signer=signer, object_store=store, facility_name="dgx-1")
        manifest = publisher.publish(_job(), _result(tmp_path), version="1.0.0")

        assert isinstance(manifest, AdapterManifest)
        assert manifest.signer_public_key == signer.public_key
        # Blob is uploaded under a deterministic key
        assert manifest.sha256 in store.keys()  # noqa: SIM118

    def test_manifest_passes_adapter_registry_verification(self, tmp_path: Path) -> None:
        signer = MessageSigner.generate()
        store = InMemoryObjectStore()
        publisher = TrainerPublisher(signer=signer, object_store=store, facility_name="dgx-1")
        manifest = publisher.publish(_job(), _result(tmp_path), version="1.0.0")

        registry = AdapterRegistry(
            worker_base_model="qwen2.5-7b",
            trusted_issuers=[signer.public_key],
        )
        # Passes signature + sha256 + base_model checks
        registry.verify(manifest, store.get(manifest.sha256))

    def test_facility_name_appears_in_manifest_name(self, tmp_path: Path) -> None:
        signer = MessageSigner.generate()
        publisher = TrainerPublisher(
            signer=signer, object_store=InMemoryObjectStore(), facility_name="dgx-1"
        )
        manifest = publisher.publish(_job(), _result(tmp_path), version="1.0.0")
        assert "dgx-1" in manifest.name


class TestFacilityKeyIsolation:
    """A facility key must not be mistaken for a worker key.

    The registry's trust set is constructed by the caller, but the publisher
    exposes its public key under a distinct property so deployment code can
    register facilities into a separate trust namespace.
    """

    def test_publisher_exposes_facility_public_key(self) -> None:
        signer = MessageSigner.generate()
        publisher = TrainerPublisher(
            signer=signer, object_store=InMemoryObjectStore(), facility_name="dgx-1"
        )
        assert publisher.facility_public_key == signer.public_key

    def test_unsigned_blob_rejected_by_registry(self, tmp_path: Path) -> None:
        signer = MessageSigner.generate()
        other = MessageSigner.generate()
        store = InMemoryObjectStore()
        publisher = TrainerPublisher(signer=signer, object_store=store, facility_name="dgx-1")
        manifest = publisher.publish(_job(), _result(tmp_path), version="1.0.0")

        # Worker only trusts a different key — must reject this manifest.
        registry = AdapterRegistry(
            worker_base_model="qwen2.5-7b",
            trusted_issuers=[other.public_key],
        )
        with pytest.raises(SignatureError):
            registry.verify(manifest, store.get(manifest.sha256))
