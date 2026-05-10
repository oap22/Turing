"""Tests for AdapterRegistry — the deep module hiding adapter trust.

Adapters are LoRAs distributed via the NATS object store. Workers must verify
SHA256 + Ed25519 signature + base-model match before loading, or a tampered
object-store entry could poison a model. The registry also tracks lifecycle
state — STAGED until a worker confirms the adapter at its actual quantization,
then LIVE.
"""

from __future__ import annotations

import hashlib

import pytest

from turing.coordinator.adapters import (
    AdapterManifest,
    AdapterRegistry,
    AdapterState,
    BaseModelMismatchError,
    HashMismatchError,
    UnknownAdapterError,
)
from turing.coordinator.adapters.manifest import (
    CURRENT_ADAPTER_MANIFEST_VERSION,
    ManifestVersionError,
)
from turing.transport.signer import MessageSigner, SignatureError


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _sign_manifest(
    signer: MessageSigner,
    *,
    name: str = "research-summarize",
    version: str = "1.0.0",
    base_model: str = "qwen2.5:14b",
    sha256: str | None = None,
    eval_score: float = 0.71,
) -> AdapterManifest:
    blob = b"adapter-bytes-" + name.encode() + version.encode()
    digest = sha256 if sha256 is not None else _sha(blob)
    unsigned = AdapterManifest(
        name=name,
        version=version,
        base_model=base_model,
        sha256=digest,
        eval_score=eval_score,
        signer_public_key=signer.public_key,
        signature=b"",
        schema_version=CURRENT_ADAPTER_MANIFEST_VERSION,
    )
    payload = unsigned.signing_bytes()
    signed = signer.sign(payload)
    return unsigned.with_signature(signed.signature)


@pytest.fixture()
def issuer() -> MessageSigner:
    return MessageSigner.generate()


@pytest.fixture()
def registry(issuer: MessageSigner) -> AdapterRegistry:
    return AdapterRegistry(
        worker_base_model="qwen2.5:14b",
        trusted_issuers=[issuer.public_key],
    )


def test_verify_accepts_well_formed_signed_manifest(
    registry: AdapterRegistry, issuer: MessageSigner
) -> None:
    blob = b"adapter-bytes-research-summarize1.0.0"
    manifest = _sign_manifest(issuer, sha256=_sha(blob))

    registry.verify(manifest, blob)  # does not raise


def test_verify_rejects_blob_whose_hash_does_not_match(
    registry: AdapterRegistry, issuer: MessageSigner
) -> None:
    manifest = _sign_manifest(issuer, sha256=_sha(b"original"))

    with pytest.raises(HashMismatchError):
        registry.verify(manifest, b"tampered-bytes")


def test_verify_rejects_forged_signature(registry: AdapterRegistry, issuer: MessageSigner) -> None:
    blob = b"adapter-bytes-research-summarize1.0.0"
    manifest = _sign_manifest(issuer, sha256=_sha(blob))
    forged = manifest.with_signature(b"\x00" * 64)

    with pytest.raises(SignatureError):
        registry.verify(forged, blob)


def test_verify_rejects_unknown_signer(issuer: MessageSigner) -> None:
    other = MessageSigner.generate()
    registry = AdapterRegistry(
        worker_base_model="qwen2.5:14b",
        trusted_issuers=[issuer.public_key],  # `other` not trusted
    )
    blob = b"adapter-bytes-research-summarize1.0.0"
    manifest = _sign_manifest(other, sha256=_sha(blob))

    with pytest.raises(SignatureError):
        registry.verify(manifest, blob)


def test_verify_rejects_base_model_mismatch(
    registry: AdapterRegistry, issuer: MessageSigner
) -> None:
    blob = b"adapter-bytes-research-summarize1.0.0"
    manifest = _sign_manifest(issuer, sha256=_sha(blob), base_model="llama3:70b")

    with pytest.raises(BaseModelMismatchError):
        registry.verify(manifest, blob)


def test_manifest_rejects_mismatched_schema_version(issuer: MessageSigner) -> None:
    raw = _sign_manifest(issuer).to_dict()
    raw["schema_version"] = CURRENT_ADAPTER_MANIFEST_VERSION + 1

    with pytest.raises(ManifestVersionError):
        AdapterManifest.from_dict(raw)


def test_register_then_query_returns_staged(
    registry: AdapterRegistry, issuer: MessageSigner
) -> None:
    blob = b"adapter-bytes-research-summarize1.0.0"
    manifest = _sign_manifest(issuer, sha256=_sha(blob))

    registry.register(manifest, blob)

    state = registry.state_of(name="research-summarize", version="1.0.0")
    assert state is AdapterState.STAGED


def test_promote_transitions_staged_to_live(
    registry: AdapterRegistry, issuer: MessageSigner
) -> None:
    blob = b"adapter-bytes-research-summarize1.0.0"
    manifest = _sign_manifest(issuer, sha256=_sha(blob))
    registry.register(manifest, blob)

    registry.promote(name="research-summarize", version="1.0.0")

    assert registry.state_of(name="research-summarize", version="1.0.0") is AdapterState.LIVE


def test_promote_unknown_adapter_raises(registry: AdapterRegistry) -> None:
    with pytest.raises(UnknownAdapterError):
        registry.promote(name="nope", version="0.0.0")


def test_register_refuses_to_overwrite_existing_version(
    registry: AdapterRegistry, issuer: MessageSigner
) -> None:
    blob = b"adapter-bytes-research-summarize1.0.0"
    manifest = _sign_manifest(issuer, sha256=_sha(blob))
    registry.register(manifest, blob)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(manifest, blob)
