"""AdapterRegistry — verify and lifecycle-manage signed LoRA adapters.

A worker that loads an unverified adapter is a poisoning vector, so this
module is paranoid by default. `verify` runs four independent checks:

1. The manifest's `schema_version` matches what we understand (caught at
   `AdapterManifest.from_dict` — kept as a separate path so a stale worker
   refuses unknown formats up front rather than mid-load).
2. The signature on the manifest verifies against `trusted_issuers`.
3. The blob's SHA256 matches the manifest's claim.
4. The manifest's `base_model` matches the worker's loaded base.

Once verified, an adapter sits in `STAGED`. A separate `promote` step moves
it to `LIVE` after the K-worker live-eval check (Slice 22 / issue #24) signs
off — the registry doesn't enforce that gate itself, but the state split
gives the gate something to write through.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from turing.transport.signer import (
    MessageSigner,
    SignatureError,
    SignedMessage,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from turing.coordinator.adapters.manifest import AdapterManifest


class HashMismatchError(Exception):
    """Raised when an adapter blob's SHA256 doesn't match the manifest claim."""


class BaseModelMismatchError(Exception):
    """Raised when an adapter targets a base_model the worker doesn't run."""


class UnknownAdapterError(Exception):
    """Raised when a state transition references an unregistered adapter."""


class AdapterState(Enum):
    STAGED = "STAGED"
    LIVE = "LIVE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class _AdapterKey:
    name: str
    version: str


class AdapterRegistry:
    def __init__(
        self,
        *,
        worker_base_model: str,
        trusted_issuers: Iterable[bytes],
    ) -> None:
        self._worker_base_model = worker_base_model
        self._trusted = [bytes(k) for k in trusted_issuers]
        # `MessageSigner` doesn't need its own key for verify-only use.
        self._verifier = MessageSigner.generate()
        self._states: dict[_AdapterKey, AdapterState] = {}
        self._canary_scores: dict[_AdapterKey, float] = {}
        self._latest_live_by_name: dict[str, _AdapterKey] = {}

    def verify(self, manifest: AdapterManifest, blob: bytes) -> None:
        """Raise if the manifest+blob pair shouldn't be loaded."""
        signed = SignedMessage(
            payload=manifest.signing_bytes(),
            signature=manifest.signature,
            sender_public_key=manifest.signer_public_key,
        )
        self._verifier.verify(signed, trusted_public_keys=self._trusted)

        digest = hashlib.sha256(blob).hexdigest()
        if digest != manifest.sha256:
            raise HashMismatchError(
                f"blob sha256 {digest!r} does not match manifest {manifest.sha256!r}"
            )

        if manifest.base_model != self._worker_base_model:
            raise BaseModelMismatchError(
                f"adapter base_model {manifest.base_model!r} does not match "
                f"worker base {self._worker_base_model!r}"
            )

    def register(self, manifest: AdapterManifest, blob: bytes) -> None:
        self.verify(manifest, blob)
        key = _AdapterKey(manifest.name, manifest.version)
        if key in self._states:
            raise ValueError(f"adapter {manifest.name}@{manifest.version} already registered")
        self._states[key] = AdapterState.STAGED

    def promote(
        self,
        *,
        name: str,
        version: str,
        canary_eval_score: float | None = None,
    ) -> None:
        key = _AdapterKey(name, version)
        if key not in self._states:
            raise UnknownAdapterError(f"adapter {name}@{version} is not registered")
        if self._states[key] is AdapterState.REJECTED:
            raise ValueError(f"adapter {name}@{version} is REJECTED and cannot be promoted")
        self._states[key] = AdapterState.LIVE
        if canary_eval_score is not None:
            self._canary_scores[key] = canary_eval_score
        # Track the most-recently promoted LIVE per name; the next canary's
        # pass check looks up this incumbent's recorded canary_eval_score.
        self._latest_live_by_name[name] = key

    def reject(self, *, name: str, version: str) -> None:
        """Mark an adapter REJECTED — permanent terminal state per ADR 0007."""
        key = _AdapterKey(name, version)
        if key not in self._states:
            raise UnknownAdapterError(f"adapter {name}@{version} is not registered")
        self._states[key] = AdapterState.REJECTED

    def canary_eval_score_of(self, *, name: str, version: str) -> float | None:
        """Return the recorded canary_eval_score for a LIVE adapter, or None."""
        key = _AdapterKey(name, version)
        if key not in self._states:
            raise UnknownAdapterError(f"adapter {name}@{version} is not registered")
        return self._canary_scores.get(key)

    def prior_live_canary_score(self, *, name: str) -> float | None:
        """Return the canary_eval_score of the most-recent LIVE adapter for
        this name, or None if no incumbent has been promoted yet."""
        key = self._latest_live_by_name.get(name)
        if key is None:
            return None
        return self._canary_scores.get(key)

    def state_of(self, *, name: str, version: str) -> AdapterState:
        key = _AdapterKey(name, version)
        if key not in self._states:
            raise UnknownAdapterError(f"adapter {name}@{version} is not registered")
        return self._states[key]


# Re-export for convenience so callers don't have to dual-import.
SignatureError = SignatureError
