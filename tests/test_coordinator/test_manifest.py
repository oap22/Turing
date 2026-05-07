"""Tests for CapabilityManifest — typed, versioned worker self-description.

Workers advertise a manifest at registration and refresh on heartbeat. The
schema is versioned so a worker running a stale build is rejected with a clear
error rather than silently dispatched against.
"""

from __future__ import annotations

import pytest

from turing.coordinator.registry.manifest import (
    CURRENT_MANIFEST_VERSION,
    CapabilityManifest,
    ManifestVersionError,
)


def _manifest(**overrides: object) -> CapabilityManifest:
    base = dict(
        worker_id="worker-1",
        specialties=("research-summarize",),
        base_model="qwen2.5:14b",
        adapters=(),
        tools=("vault_query", "web_fetch"),
        hardware="mbp-m3",
        max_concurrent=2,
        eval_score=0.71,
        public_key=b"\x01" * 32,
        schema_version=CURRENT_MANIFEST_VERSION,
    )
    base.update(overrides)
    return CapabilityManifest(**base)  # type: ignore[arg-type]


def test_manifest_round_trips_through_dict() -> None:
    manifest = _manifest()
    restored = CapabilityManifest.from_dict(manifest.to_dict())
    assert restored == manifest


def test_manifest_rejects_mismatched_schema_version() -> None:
    raw = _manifest().to_dict()
    raw["schema_version"] = CURRENT_MANIFEST_VERSION + 1

    with pytest.raises(ManifestVersionError):
        CapabilityManifest.from_dict(raw)


def test_manifest_requires_at_least_one_specialty() -> None:
    with pytest.raises(ValueError, match="specialty"):
        _manifest(specialties=())


def test_manifest_max_concurrent_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        _manifest(max_concurrent=0)
