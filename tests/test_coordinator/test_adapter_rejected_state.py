"""AdapterState.REJECTED — permanent terminal state on canary regression.

Per ADR 0007 §6: a canary failure transitions the adapter to REJECTED.
Permanent — no held slots, no operator-required unlock. A second STAGED
of the *same* (name, version) is refused; STAGED of a *different*
version proceeds normally.
"""

from __future__ import annotations

import hashlib

import pytest

from turing.coordinator.adapters import (
    AdapterManifest,
    AdapterRegistry,
    AdapterState,
    UnknownAdapterError,
)
from turing.coordinator.adapters.manifest import (
    CURRENT_ADAPTER_MANIFEST_VERSION,
)
from turing.transport.signer import MessageSigner


def _make_manifest(
    signer: MessageSigner,
    *,
    name: str = "research-summarize",
    version: str = "1.0.0",
    base_model: str = "qwen2.5:14b",
) -> tuple[AdapterManifest, bytes]:
    blob = b"adapter-bytes-" + name.encode() + version.encode()
    digest = hashlib.sha256(blob).hexdigest()
    unsigned = AdapterManifest(
        name=name,
        version=version,
        base_model=base_model,
        sha256=digest,
        eval_score=0.71,
        signer_public_key=signer.public_key,
        signature=b"",
        schema_version=CURRENT_ADAPTER_MANIFEST_VERSION,
    )
    payload = unsigned.signing_bytes()
    signed = signer.sign(payload)
    return unsigned.with_signature(signed.signature), blob


def _registry(signer: MessageSigner) -> AdapterRegistry:
    return AdapterRegistry(
        worker_base_model="qwen2.5:14b",
        trusted_issuers=[signer.public_key],
    )


def test_reject_transitions_to_rejected_state() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    manifest, blob = _make_manifest(signer)
    reg.register(manifest, blob)

    reg.reject(name=manifest.name, version=manifest.version)

    assert (
        reg.state_of(name=manifest.name, version=manifest.version)
        is AdapterState.REJECTED
    )


def test_rejected_is_permanent_same_version_refused() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    manifest, blob = _make_manifest(signer)
    reg.register(manifest, blob)
    reg.reject(name=manifest.name, version=manifest.version)

    # Re-registering the SAME (name, version) is refused.
    with pytest.raises(ValueError, match="already registered"):
        reg.register(manifest, blob)


def test_rejected_does_not_block_different_version() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    manifest_v1, blob_v1 = _make_manifest(signer, version="1.0.0")
    reg.register(manifest_v1, blob_v1)
    reg.reject(name=manifest_v1.name, version=manifest_v1.version)

    # A *different* version proceeds normally.
    manifest_v2, blob_v2 = _make_manifest(signer, version="2.0.0")
    reg.register(manifest_v2, blob_v2)
    assert (
        reg.state_of(name=manifest_v2.name, version=manifest_v2.version)
        is AdapterState.STAGED
    )


def test_reject_unknown_adapter_raises() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    with pytest.raises(UnknownAdapterError):
        reg.reject(name="ghost", version="0.0.0")


def test_promote_after_reject_is_refused() -> None:
    """REJECTED is terminal — cannot be promoted to LIVE."""
    signer = MessageSigner.generate()
    reg = _registry(signer)
    manifest, blob = _make_manifest(signer)
    reg.register(manifest, blob)
    reg.reject(name=manifest.name, version=manifest.version)

    with pytest.raises(ValueError, match="REJECTED"):
        reg.promote(name=manifest.name, version=manifest.version)
