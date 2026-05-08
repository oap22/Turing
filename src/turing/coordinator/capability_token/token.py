"""CapabilityToken — signed scope + the issuer/verifier services that handle them.

Per ADR 0003 v1: the coordinator is the only entity that signs a
``CapabilityToken`` and the only entity that verifies one (workers carry it
through). The token is a signed ``CapabilityScope`` plus the signer's public
key. ``cwd`` is **not** a scope field — the gate resolves the workspace from
``(task_id, subtask_id)``.

Command matching uses :py:meth:`re.fullmatch` (ADR 0003 §8.6) — anything else
is a regex-bypass footgun (``^cat `` would allow ``cat foo; rm -rf /``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from turing.coordinator.capability_token.scope import CapabilityScope
from turing.transport.signer import (
    MessageSigner,
    SignatureError,
    SignedMessage,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class TokenSignatureError(Exception):
    """Raised when a token's signature does not verify."""


class ScopeViolation(Exception):
    """Raised when a request falls outside its token's scope."""


@dataclass(frozen=True)
class CapabilityToken:
    scope: CapabilityScope
    signature: bytes
    signer_public_key: bytes

    def to_dict(self) -> dict:
        return {
            "scope": self.scope.to_dict(),
            "signature": self.signature.hex(),
            "signer_public_key": self.signer_public_key.hex(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> CapabilityToken:
        return cls(
            scope=CapabilityScope.from_dict(d["scope"]),
            signature=bytes.fromhex(d["signature"]),
            signer_public_key=bytes.fromhex(d["signer_public_key"]),
        )

    def fingerprint(self) -> str:
        """16-hex-char hash over the canonical token bytes (ADR 0003 §9)."""
        h = hashlib.sha256(self.scope.signing_bytes() + self.signature).hexdigest()
        return h[:16]


@dataclass(frozen=True)
class AuthorizedExecution:
    """Returned by ``CapabilityVerifier.authorize`` after all checks pass."""

    subtask_id: str
    task_id: str
    timeout_s: int


class CapabilityTokenIssuer:
    def __init__(self, *, signer: MessageSigner) -> None:
        self._signer = signer

    def issue(self, scope: CapabilityScope) -> CapabilityToken:
        signed = self._signer.sign(scope.signing_bytes())
        return CapabilityToken(
            scope=scope,
            signature=signed.signature,
            signer_public_key=signed.sender_public_key,
        )


class CapabilityVerifier:
    def __init__(self, *, trusted_issuers: Iterable[bytes]) -> None:
        self._trusted = [bytes(k) for k in trusted_issuers]
        # MessageSigner needs a private key to instantiate, but we only use
        # its verify() method which is keyless beyond the trusted set.
        self._verifier = MessageSigner.generate()

    def verify(self, token: CapabilityToken, *, now_ms: int) -> CapabilityToken:
        del now_ms  # signature alone — lifetime is checked in authorize()
        try:
            self._verifier.verify(
                SignedMessage(
                    payload=token.scope.signing_bytes(),
                    signature=token.signature,
                    sender_public_key=token.signer_public_key,
                ),
                trusted_public_keys=self._trusted,
            )
        except SignatureError as exc:
            raise TokenSignatureError(str(exc)) from exc
        return token

    def authorize(
        self,
        token: CapabilityToken,
        *,
        command: str,
        now_ms: int,
    ) -> AuthorizedExecution:
        """Signature + expiry + command-regex check (ADR 0003 §8 steps 3,5,6).

        Subtask-state binding (step 4) and workspace resolution (step 7)
        belong to the gate which holds the lifecycle store and tmpdirs; the
        verifier is the pure-policy slice.
        """
        self.verify(token, now_ms=now_ms)
        scope = token.scope
        if now_ms >= scope.expires_at_ms:
            raise ScopeViolation(f"token expired: now={now_ms} >= expires_at={scope.expires_at_ms}")
        if not re.fullmatch(scope.allowed_commands_regex, command):
            raise ScopeViolation(
                f"command {command!r} does not match allowed pattern "
                f"{scope.allowed_commands_regex!r}"
            )
        return AuthorizedExecution(
            subtask_id=scope.subtask_id,
            task_id=scope.task_id,
            timeout_s=scope.timeout_s,
        )
