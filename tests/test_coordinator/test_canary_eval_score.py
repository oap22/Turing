"""canary_eval_score on LIVE adapters per ADR 0007 §4.

Each LIVE adapter records the quantized canary score it earned on
promotion. The next canary's pass/fail compares against this recorded
value, not against the offline-gate's pre-quantization score.
"""

from __future__ import annotations

import hashlib

import pytest

from turing.coordinator.adapters import (
    AdapterManifest,
    AdapterRegistry,
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
) -> tuple[AdapterManifest, bytes]:
    blob = b"adapter-bytes-" + name.encode() + version.encode()
    digest = hashlib.sha256(blob).hexdigest()
    unsigned = AdapterManifest(
        name=name,
        version=version,
        base_model="qwen2.5:14b",
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


def test_promote_records_canary_eval_score() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    manifest, blob = _make_manifest(signer)
    reg.register(manifest, blob)

    reg.promote(name=manifest.name, version=manifest.version, canary_eval_score=72.4)

    assert (
        reg.canary_eval_score_of(name=manifest.name, version=manifest.version)
        == pytest.approx(72.4)
    )


def test_canary_eval_score_unset_for_staged_adapter() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    manifest, blob = _make_manifest(signer)
    reg.register(manifest, blob)
    # Not yet promoted → no canary score recorded.
    assert (
        reg.canary_eval_score_of(name=manifest.name, version=manifest.version)
        is None
    )


def test_canary_eval_score_for_unknown_adapter_raises() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    with pytest.raises(UnknownAdapterError):
        reg.canary_eval_score_of(name="ghost", version="0.0.0")


def test_prior_live_canary_score_lookup_for_specialty() -> None:
    """`prior_live_canary_score(specialty)` returns the score of the
    LIVE adapter for a specialty, or None if there's no incumbent."""
    signer = MessageSigner.generate()
    reg = _registry(signer)
    # No incumbent yet.
    assert reg.prior_live_canary_score(name="research-summarize") is None

    manifest, blob = _make_manifest(signer, version="1.0.0")
    reg.register(manifest, blob)
    reg.promote(name="research-summarize", version="1.0.0", canary_eval_score=70.0)

    assert reg.prior_live_canary_score(name="research-summarize") == pytest.approx(
        70.0
    )

    # When v2 promotes, prior_live points at the most-recent LIVE.
    manifest_v2, blob_v2 = _make_manifest(signer, version="2.0.0")
    reg.register(manifest_v2, blob_v2)
    reg.promote(name="research-summarize", version="2.0.0", canary_eval_score=72.5)
    assert reg.prior_live_canary_score(name="research-summarize") == pytest.approx(
        72.5
    )
