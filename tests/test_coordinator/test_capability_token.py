"""Tests for the coordinator-issued capability token primitive (ADR 0003 v1)."""

from __future__ import annotations

import pytest

from turing.coordinator.capability_token import (
    CapabilityScope,
    CapabilityToken,
    CapabilityTokenIssuer,
    CapabilityVerifier,
    ScopeViolationError,
    TokenSignatureError,
)
from turing.transport.signer import MessageSigner


def _scope(
    *,
    subtask_id: str = "st_a",
    task_id: str = "tsk_x",
    allowed_commands_regex: str = r"echo .*",
    timeout_s: int = 30,
    expires_at_ms: int = 10_000_000,
    issued_at_ms: int = 1_000,
) -> CapabilityScope:
    return CapabilityScope(
        subtask_id=subtask_id,
        task_id=task_id,
        allowed_commands_regex=allowed_commands_regex,
        timeout_s=timeout_s,
        expires_at_ms=expires_at_ms,
        issued_at_ms=issued_at_ms,
    )


class TestSignVerify:
    def test_signed_token_round_trips(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(_scope())
        assert isinstance(token, CapabilityToken)
        assert token.signer_public_key == coord.public_key
        verified = CapabilityVerifier(trusted_issuers=[coord.public_key]).verify(token, now_ms=1)
        assert verified.scope.subtask_id == "st_a"
        assert verified.scope.task_id == "tsk_x"

    def test_unknown_signer_rejected(self) -> None:
        coord = MessageSigner.generate()
        rogue = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=rogue).issue(_scope())
        with pytest.raises(TokenSignatureError):
            CapabilityVerifier(trusted_issuers=[coord.public_key]).verify(token, now_ms=1)

    def test_tampered_scope_rejected(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(_scope(allowed_commands_regex=r"ls .*"))
        forged = CapabilityToken(
            scope=_scope(allowed_commands_regex=r".*"),
            signature=token.signature,
            signer_public_key=token.signer_public_key,
        )
        with pytest.raises(TokenSignatureError):
            CapabilityVerifier(trusted_issuers=[coord.public_key]).verify(forged, now_ms=1)

    def test_token_round_trips_through_dict(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(_scope())
        rebuilt = CapabilityToken.from_dict(token.to_dict())
        assert rebuilt == token
        assert rebuilt.fingerprint() == token.fingerprint()


class TestAuthorize:
    def _verifier(self, trusted: bytes) -> CapabilityVerifier:
        return CapabilityVerifier(trusted_issuers=[trusted])

    def test_allowed_command_passes(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(_scope(allowed_commands_regex=r"echo .*"))
        decision = self._verifier(coord.public_key).authorize(token, command="echo hi", now_ms=1)
        assert decision.subtask_id == "st_a"
        assert decision.task_id == "tsk_x"
        assert decision.timeout_s == 30

    def test_disallowed_command_rejected(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(_scope(allowed_commands_regex=r"echo"))
        with pytest.raises(ScopeViolationError, match="command"):
            self._verifier(coord.public_key).authorize(token, command="rm -rf /", now_ms=1)

    def test_fullmatch_rejects_partial_match(self) -> None:
        # `cat .*` must NOT permit `cat foo; rm -rf /` (would pass with re.match).
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(_scope(allowed_commands_regex=r"cat .*"))
        verifier = self._verifier(coord.public_key)
        verifier.authorize(token, command="cat foo", now_ms=1)
        with pytest.raises(ScopeViolationError, match="command"):
            verifier.authorize(token, command="cat foo\n; rm -rf /", now_ms=1)

    def test_expired_token_rejected(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(_scope(expires_at_ms=1000))
        with pytest.raises(ScopeViolationError, match="expired"):
            self._verifier(coord.public_key).authorize(token, command="echo hi", now_ms=2000)

    def test_v1_deny_by_default_regex_rejects_everything(self) -> None:
        # The orchestrator's v1 default (deny-by-default) is r"(?!x)x".
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(_scope(allowed_commands_regex=r"(?!x)x"))
        verifier = self._verifier(coord.public_key)
        for cmd in ["echo hi", "ls", "true", "x", "", "anything"]:
            with pytest.raises(ScopeViolationError):
                verifier.authorize(token, command=cmd, now_ms=1)
