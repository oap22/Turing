"""Tests for the coordinator-issued capability token (#12)."""

from __future__ import annotations

import pytest

from turing.coordinator.capability_token import (
    CapabilityScope,
    CapabilityToken,
    CapabilityTokenIssuer,
    CapabilityVerifier,
    ScopeViolation,
    TokenSignatureError,
)
from turing.transport.signer import MessageSigner


# ── helpers ──────────────────────────────────────────────────────────


def _scope(
    *,
    subtask_id: str = "t-1.s-0",
    allowed_commands_regex: str = r"^(echo|ls)\b",
    cwd: str = "/tmp/work",
    timeout_s: int = 30,
    expires_at_ms: int = 10_000_000,
) -> CapabilityScope:
    return CapabilityScope(
        subtask_id=subtask_id,
        allowed_commands_regex=allowed_commands_regex,
        cwd=cwd,
        timeout_s=timeout_s,
        expires_at_ms=expires_at_ms,
    )


# ── sign/verify ──────────────────────────────────────────────────────


class TestSignVerify:
    def test_signed_token_round_trips(self) -> None:
        coord = MessageSigner.generate()
        issuer = CapabilityTokenIssuer(signer=coord)
        token = issuer.issue(_scope())
        assert isinstance(token, CapabilityToken)
        assert token.signer_public_key == coord.public_key

        verifier = CapabilityVerifier(trusted_issuers=[coord.public_key])
        verified = verifier.verify(token, now_ms=1)
        assert verified.scope.subtask_id == "t-1.s-0"

    def test_unknown_signer_rejected(self) -> None:
        coord = MessageSigner.generate()
        rogue = MessageSigner.generate()
        issuer = CapabilityTokenIssuer(signer=rogue)
        token = issuer.issue(_scope())

        verifier = CapabilityVerifier(trusted_issuers=[coord.public_key])
        with pytest.raises(TokenSignatureError):
            verifier.verify(token, now_ms=1)

    def test_tampered_scope_rejected(self) -> None:
        coord = MessageSigner.generate()
        issuer = CapabilityTokenIssuer(signer=coord)
        token = issuer.issue(_scope(allowed_commands_regex=r"^ls\b"))
        # Forge a wider regex post-sign
        forged = CapabilityToken(
            scope=_scope(allowed_commands_regex=r"^.*$"),
            signature=token.signature,
            signer_public_key=token.signer_public_key,
        )
        verifier = CapabilityVerifier(trusted_issuers=[coord.public_key])
        with pytest.raises(TokenSignatureError):
            verifier.verify(forged, now_ms=1)


# ── scope enforcement ────────────────────────────────────────────────


class TestScopeEnforcement:
    def _verifier(self, trusted: bytes) -> CapabilityVerifier:
        return CapabilityVerifier(trusted_issuers=[trusted])

    def test_allowed_command_passes(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(
            _scope(allowed_commands_regex=r"^(echo|ls)\b")
        )
        verifier = self._verifier(coord.public_key)
        # echo / ls both pass
        verifier.authorize(
            token,
            command="echo hi",
            cwd="/tmp/work",
            now_ms=1,
        )
        verifier.authorize(
            token,
            command="ls -la",
            cwd="/tmp/work",
            now_ms=1,
        )

    def test_disallowed_command_rejected(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(
            _scope(allowed_commands_regex=r"^echo\b")
        )
        verifier = self._verifier(coord.public_key)
        with pytest.raises(ScopeViolation, match="command"):
            verifier.authorize(token, command="rm -rf /", cwd="/tmp/work", now_ms=1)

    def test_wrong_cwd_rejected(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(_scope(cwd="/tmp/work"))
        verifier = self._verifier(coord.public_key)
        with pytest.raises(ScopeViolation, match="cwd"):
            verifier.authorize(
                token, command="echo hi", cwd="/etc", now_ms=1
            )

    def test_expired_token_rejected(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(
            _scope(expires_at_ms=1000)
        )
        verifier = self._verifier(coord.public_key)
        with pytest.raises(ScopeViolation, match="expired"):
            verifier.authorize(
                token, command="echo hi", cwd="/tmp/work", now_ms=2000
            )

    def test_timeout_budget_exposed_for_caller(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(_scope(timeout_s=15))
        verifier = self._verifier(coord.public_key)
        decision = verifier.authorize(
            token, command="echo hi", cwd="/tmp/work", now_ms=1
        )
        assert decision.timeout_s == 15

    def test_authorize_records_subtask_id_for_audit(self) -> None:
        coord = MessageSigner.generate()
        token = CapabilityTokenIssuer(signer=coord).issue(
            _scope(subtask_id="t-99.s-3")
        )
        verifier = self._verifier(coord.public_key)
        decision = verifier.authorize(
            token, command="echo hi", cwd="/tmp/work", now_ms=1
        )
        assert decision.subtask_id == "t-99.s-3"
