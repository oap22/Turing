"""End-to-end: select canary → run eval → decide → promote or reject.

Composes CanarySelector + CanaryPassGate + AdapterRegistry into the
slice-4 decision flow, simulating the canary subtask result inline
rather than dispatching it. Acceptance criteria covered:

- Round-robin selection across a multi-worker specialty.
- Pass-threshold test: PROMOTE on within-ε; REJECT on below-ε.
- canary_eval_score recorded on PROMOTE.
- AdapterState.REJECTED on regression — permanent.
- First-ever promotion passes with no incumbent.

Worker-side handler dispatch and Discord notify are out of this slice's
scope; the gate's *decision* is what this test exercises.
"""

from __future__ import annotations

import hashlib

import pytest

from turing.coordinator.adapters import (
    AdapterManifest,
    AdapterRegistry,
    AdapterState,
)
from turing.coordinator.adapters.manifest import (
    CURRENT_ADAPTER_MANIFEST_VERSION,
)
from turing.coordinator.promotion import (
    CanaryPassGate,
    CanarySelector,
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


def _decide_and_apply(
    *,
    reg: AdapterRegistry,
    selector: CanarySelector,
    pass_gate: CanaryPassGate,
    name: str,
    version: str,
    fleet: tuple[str, ...],
    last_canary_worker_id: str | None,
    canary_score: float,
) -> tuple[str, bool]:
    """Run the slice-4 decision flow inline. Returns (canary_worker, promoted)."""
    canary_worker = selector.next(
        fleet=fleet, last_canary_worker_id=last_canary_worker_id
    )
    prior = reg.prior_live_canary_score(name=name)
    result = pass_gate.evaluate(
        canary_score=canary_score, prior_live_canary_score=prior
    )
    if result.passed:
        reg.promote(name=name, version=version, canary_eval_score=canary_score)
        return canary_worker, True
    reg.reject(name=name, version=version)
    return canary_worker, False


def test_first_ever_promotion_passes_with_no_incumbent() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    selector = CanarySelector()
    gate = CanaryPassGate(epsilon_pp=0.5)

    manifest, blob = _make_manifest(signer, version="1.0.0")
    reg.register(manifest, blob)

    canary, promoted = _decide_and_apply(
        reg=reg,
        selector=selector,
        pass_gate=gate,
        name="research-summarize",
        version="1.0.0",
        fleet=("jetson-a", "jetson-b"),
        last_canary_worker_id=None,
        canary_score=68.0,
    )
    assert canary == "jetson-a"
    assert promoted is True
    assert reg.state_of(name="research-summarize", version="1.0.0") is AdapterState.LIVE
    assert reg.canary_eval_score_of(
        name="research-summarize", version="1.0.0"
    ) == pytest.approx(68.0)


def test_within_epsilon_against_incumbent_promotes_and_rotates_canary() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    selector = CanarySelector()
    gate = CanaryPassGate(epsilon_pp=0.5)

    # Incumbent v1 promoted with canary score 70.0 on jetson-a.
    m1, b1 = _make_manifest(signer, version="1.0.0")
    reg.register(m1, b1)
    reg.promote(name="research-summarize", version="1.0.0", canary_eval_score=70.0)

    # v2 canary scores 69.7 — within ε=0.5pp; should promote.
    m2, b2 = _make_manifest(signer, version="2.0.0")
    reg.register(m2, b2)

    canary, promoted = _decide_and_apply(
        reg=reg,
        selector=selector,
        pass_gate=gate,
        name="research-summarize",
        version="2.0.0",
        fleet=("jetson-a", "jetson-b"),
        last_canary_worker_id="jetson-a",
        canary_score=69.7,
    )
    # Round-robin: last was jetson-a → next is jetson-b.
    assert canary == "jetson-b"
    assert promoted is True
    assert reg.state_of(name="research-summarize", version="2.0.0") is AdapterState.LIVE
    # New incumbent's score becomes the next canary's reference.
    assert reg.prior_live_canary_score(name="research-summarize") == pytest.approx(
        69.7
    )


def test_below_epsilon_against_incumbent_rejects_permanent() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    selector = CanarySelector()
    gate = CanaryPassGate(epsilon_pp=0.5)

    # Incumbent v1 at 70.0.
    m1, b1 = _make_manifest(signer, version="1.0.0")
    reg.register(m1, b1)
    reg.promote(name="research-summarize", version="1.0.0", canary_eval_score=70.0)

    # v2 canary scores 69.4 — below ε; reject.
    m2, b2 = _make_manifest(signer, version="2.0.0")
    reg.register(m2, b2)

    _canary, promoted = _decide_and_apply(
        reg=reg,
        selector=selector,
        pass_gate=gate,
        name="research-summarize",
        version="2.0.0",
        fleet=("jetson-a", "jetson-b"),
        last_canary_worker_id="jetson-a",
        canary_score=69.4,
    )
    assert promoted is False
    assert (
        reg.state_of(name="research-summarize", version="2.0.0")
        is AdapterState.REJECTED
    )
    # Incumbent unchanged — prior_live_canary_score still points at v1's 70.0.
    assert reg.prior_live_canary_score(name="research-summarize") == pytest.approx(
        70.0
    )
    # REJECTED is permanent: re-registering same version refused.
    with pytest.raises(ValueError, match="already registered"):
        reg.register(m2, b2)


def test_round_robin_wraps_across_three_canaries() -> None:
    signer = MessageSigner.generate()
    reg = _registry(signer)
    selector = CanarySelector()
    fleet = ("a", "b", "c")

    last: str | None = None
    chosen: list[str] = []
    for v in ("1.0.0", "2.0.0", "3.0.0", "4.0.0"):
        m, b = _make_manifest(signer, version=v)
        reg.register(m, b)
        canary = selector.next(fleet=fleet, last_canary_worker_id=last)
        chosen.append(canary)
        # Promote unconditionally for the round-robin demonstration.
        reg.promote(name="research-summarize", version=v, canary_eval_score=70.0)
        last = canary

    # First call (no prior) → 'a'; then b, c, wrap back to a.
    assert chosen == ["a", "b", "c", "a"]
