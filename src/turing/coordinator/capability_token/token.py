"""CapabilityToken — signed scope + the issuer/verifier services that handle them.

The coordinator is the only entity that can sign a CapabilityToken; workers
verify it before executing a shell command. The token is point-in-time —
``expires_at_ms`` puts a hard upper bound on its useful life, and the
``timeout_s`` field caps any single execution within that window.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from turing.coordinator.capability_token.scope import CapabilityScope
from turing.transport.signer import (
    MessageSigner,
    SignatureError,
    SignedMessage,
)


class TokenSignatureError(Exception):
    """Raised when a token's signature does not verify."""


class ScopeViolation(Exception):
    """Raised when a request falls outside its token's scope."""


@dataclass(frozen=True)
class CapabilityToken:
    scope: CapabilityScope
    signature: bytes
    signer_public_key: bytes


@dataclass(frozen=True)
class AuthorizedExecution:
    """Returned by ``CapabilityVerifier.authorize`` after all checks pass."""

    subtask_id: str
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
        cwd: str,
        now_ms: int,
    ) -> AuthorizedExecution:
        self.verify(token, now_ms=now_ms)
        scope = token.scope
        if now_ms >= scope.expires_at_ms:
            raise ScopeViolation(
                f"token expired: now={now_ms} >= expires_at={scope.expires_at_ms}"
            )
        if cwd != scope.cwd:
            raise ScopeViolation(
                f"cwd mismatch: requested {cwd!r}, scope allows {scope.cwd!r}"
            )
        if not re.match(scope.allowed_commands_regex, command):
            raise ScopeViolation(
                f"command {command!r} does not match allowed pattern "
                f"{scope.allowed_commands_regex!r}"
            )
        return AuthorizedExecution(
            subtask_id=scope.subtask_id, timeout_s=scope.timeout_s
        )
