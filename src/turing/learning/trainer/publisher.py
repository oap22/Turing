"""TrainerPublisher — sign and publish a finished adapter.

The publisher is the trainer's only write path back to the cluster. It uploads
the adapter blob to the object store under its sha256 and emits a signed
``AdapterManifest`` that workers verify before loading. The signing key is
the *facility* key, not a worker key — deployment registers facility keys in
a separate trust namespace so a compromised worker can't issue adapters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turing.coordinator.adapters.manifest import (
    CURRENT_ADAPTER_MANIFEST_VERSION,
    AdapterManifest,
)

if TYPE_CHECKING:
    from turing.learning.trainer.job import TrainingJob
    from turing.learning.trainer.object_store import ObjectStore
    from turing.learning.trainer.runner import TrainingResult
    from turing.transport.signer import MessageSigner


class TrainerPublisher:
    def __init__(
        self,
        *,
        signer: MessageSigner,
        object_store: ObjectStore,
        facility_name: str,
    ) -> None:
        self._signer = signer
        self._store = object_store
        self._facility = facility_name

    @property
    def facility_public_key(self) -> bytes:
        """Distinct from worker keys: trust this in a separate namespace."""
        return self._signer.public_key

    def publish(
        self,
        job: TrainingJob,
        result: TrainingResult,
        *,
        version: str,
    ) -> AdapterManifest:
        blob = result.adapter_path.read_bytes()
        # Upload first so the manifest can never reference a missing blob.
        self._store.put(result.sha256, blob)

        unsigned = AdapterManifest(
            name=f"{self._facility}/{job.base_model}",
            version=version,
            base_model=job.base_model,
            sha256=result.sha256,
            eval_score=result.eval_score,
            signer_public_key=self._signer.public_key,
            signature=b"",
            schema_version=CURRENT_ADAPTER_MANIFEST_VERSION,
        )
        signed = self._signer.sign(unsigned.signing_bytes())
        return unsigned.with_signature(signed.signature)
