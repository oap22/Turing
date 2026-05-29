"""Tests for the generalist-first adapter strategy (issue #264, ADR 0009 §3).

A single ai-ml-generalist adapter is promoted through the existing registry
gate and replicated to all four homogeneous workers via the existing
canary→fleet RolloutCoordinator. Per-specialty keying (evals/lessons/registry)
is preserved so a later split is config, not a rewrite.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from turing.coordinator.adapters import AdapterManifest, AdapterRegistry, AdapterState
from turing.coordinator.adapters.manifest import CURRENT_ADAPTER_MANIFEST_VERSION
from turing.coordinator.flywheel import (
    AI_ML_GENERALIST,
    GENERALIST_FLEET_SIZE,
    GeneralistAdapterRollout,
    GeneralistFleet,
    replicate_to_fleet,
    specialty_eval_dir,
)
from turing.coordinator.promotion.rollout import RolloutState
from turing.transport.signer import MessageSigner

BASE_MODEL = "qwen2.5:7b"
ADAPTER_NAME = f"dgx-1/{AI_ML_GENERALIST}"


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def notify(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


def _sign(signer: MessageSigner, *, version: str) -> tuple[AdapterManifest, bytes]:
    blob = b"adapter-" + version.encode()
    unsigned = AdapterManifest(
        name=ADAPTER_NAME,
        version=version,
        base_model=BASE_MODEL,
        sha256=hashlib.sha256(blob).hexdigest(),
        eval_score=0.74,
        signer_public_key=signer.public_key,
        signature=b"",
        schema_version=CURRENT_ADAPTER_MANIFEST_VERSION,
    )
    manifest = unsigned.with_signature(signer.sign(unsigned.signing_bytes()).signature)
    return manifest, blob


def _fleet() -> GeneralistFleet:
    return GeneralistFleet(workers=("jetson-1", "jetson-2", "jetson-3", "jetson-4"))


def _registry_with_staged(signer: MessageSigner, version: str) -> AdapterRegistry:
    registry = AdapterRegistry(worker_base_model=BASE_MODEL, trusted_issuers=[signer.public_key])
    manifest, blob = _sign(signer, version=version)
    registry.register(manifest, blob)  # → STAGED
    return registry


# ── fleet shape ──────────────────────────────────────────────────────────────


def test_phase0_fleet_is_single_specialty_across_four_workers() -> None:
    fleet = _fleet()
    assert fleet.size == GENERALIST_FLEET_SIZE
    assert fleet.specialty == AI_ML_GENERALIST


def test_fleet_rejects_duplicate_or_empty_workers() -> None:
    with pytest.raises(ValueError):
        GeneralistFleet(workers=())
    with pytest.raises(ValueError):
        GeneralistFleet(workers=("a", "a"))


# ── replication via the existing rollout path ────────────────────────────────


def test_promoted_adapter_replicates_to_all_four_workers() -> None:
    signer = MessageSigner.generate()
    registry = _registry_with_staged(signer, "v1")
    fleet = _fleet()
    rollout = GeneralistAdapterRollout(fleet=fleet, registry=registry, notifier=_Recorder())

    coordinator = rollout.replicate(
        name=ADAPTER_NAME, version="v1", baseline_score=0.70, canary_eval_score=0.74
    )

    # The existing registry gate moved it to LIVE.
    assert registry.state_of(name=ADAPTER_NAME, version="v1") is AdapterState.LIVE
    # Canary first, then the rest → replicated to all four.
    assert coordinator.canary_worker == "jetson-1"
    replicate_to_fleet(coordinator, fleet=fleet, version="v1", score=0.74)
    assert coordinator.state is RolloutState.COMPLETED
    assert coordinator.pending_workers() == ()


def test_regression_on_any_worker_halts_replication() -> None:
    signer = MessageSigner.generate()
    registry = _registry_with_staged(signer, "v1")
    fleet = _fleet()
    recorder = _Recorder()
    rollout = GeneralistAdapterRollout(fleet=fleet, registry=registry, notifier=recorder)

    coordinator = rollout.replicate(
        name=ADAPTER_NAME, version="v1", baseline_score=0.70, canary_eval_score=0.74
    )
    # A worker regressing below baseline − threshold halts the fleet rollout.
    from turing.coordinator.promotion.rollout import LiveEvalReport, RegressionHaltedError

    with pytest.raises(RegressionHaltedError):
        coordinator.report_live_eval(LiveEvalReport(worker_id="jetson-1", score=0.40, version="v1"))
    assert coordinator.state is RolloutState.HALTED
    assert recorder.events and recorder.events[0][0] == "regression_halted"


# ── per-specialty structure preserved (not collapsed) ─────────────────────────


def test_per_specialty_eval_dir_is_preserved_and_keyed() -> None:
    # The generalist still has its own per-specialty eval dir...
    assert specialty_eval_dir(AI_ML_GENERALIST) == Path("evals") / AI_ML_GENERALIST
    # ...and a future second specialty would key to a *distinct* dir, proving
    # the per-specialty machinery isn't collapsed away.
    assert specialty_eval_dir("nlp-summarization") != specialty_eval_dir(AI_ML_GENERALIST)


def test_registry_still_keyed_by_name_and_version() -> None:
    """A second specialty's adapter coexists, keyed independently in the same
    registry — the keying is preserved, the split is just config."""
    signer = MessageSigner.generate()
    registry = _registry_with_staged(signer, "v1")
    # Register another version under a *different* specialty-bearing name.
    other_name = "dgx-1/nlp-summarization"
    blob = b"adapter-other"
    unsigned = AdapterManifest(
        name=other_name,
        version="v1",
        base_model=BASE_MODEL,
        sha256=hashlib.sha256(blob).hexdigest(),
        eval_score=0.7,
        signer_public_key=signer.public_key,
        signature=b"",
        schema_version=CURRENT_ADAPTER_MANIFEST_VERSION,
    )
    manifest = unsigned.with_signature(signer.sign(unsigned.signing_bytes()).signature)
    registry.register(manifest, blob)

    assert registry.state_of(name=ADAPTER_NAME, version="v1") is AdapterState.STAGED
    assert registry.state_of(name=other_name, version="v1") is AdapterState.STAGED
