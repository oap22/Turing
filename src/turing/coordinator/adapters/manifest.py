"""AdapterManifest — versioned, signed metadata for a LoRA adapter.

The manifest is the only thing workers trust. Object-store blobs are verified
against `sha256`; the manifest itself is verified against `signature`. The
schema is versioned so an old worker rejects a manifest format it cannot
fully parse instead of silently loading a partially-understood adapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

CURRENT_ADAPTER_MANIFEST_VERSION = 1


class ManifestVersionError(Exception):
    """Raised when an adapter manifest's schema_version doesn't match ours."""


@dataclass(frozen=True)
class AdapterManifest:
    name: str
    version: str
    base_model: str
    sha256: str
    eval_score: float
    signer_public_key: bytes
    signature: bytes
    schema_version: int = CURRENT_ADAPTER_MANIFEST_VERSION

    def signing_bytes(self) -> bytes:
        """Bytes the signature must cover — every field except signature itself."""
        d = {
            "name": self.name,
            "version": self.version,
            "base_model": self.base_model,
            "sha256": self.sha256,
            "eval_score": self.eval_score,
            "signer_public_key": self.signer_public_key.hex(),
            "schema_version": self.schema_version,
        }
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def with_signature(self, signature: bytes) -> AdapterManifest:
        return replace(self, signature=signature)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "base_model": self.base_model,
            "sha256": self.sha256,
            "eval_score": self.eval_score,
            "signer_public_key": self.signer_public_key.hex(),
            "signature": self.signature.hex(),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AdapterManifest:
        version = raw.get("schema_version")
        if version != CURRENT_ADAPTER_MANIFEST_VERSION:
            raise ManifestVersionError(
                f"adapter manifest schema_version {version!r} does not match "
                f"{CURRENT_ADAPTER_MANIFEST_VERSION}"
            )
        return cls(
            name=raw["name"],
            version=raw["version"],
            base_model=raw["base_model"],
            sha256=raw["sha256"],
            eval_score=raw["eval_score"],
            signer_public_key=bytes.fromhex(raw["signer_public_key"]),
            signature=bytes.fromhex(raw["signature"]),
            schema_version=version,
        )
