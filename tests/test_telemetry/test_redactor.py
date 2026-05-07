"""Tests for the telemetry payload redactor."""

from __future__ import annotations

from turing.telemetry.redactor import redact


class TestEmailRedaction:
    def test_strips_simple_email(self) -> None:
        out = redact("contact me at alice@example.com please")
        assert "alice@example.com" not in out
        assert "[REDACTED:email]" in out

    def test_strips_multiple_emails(self) -> None:
        out = redact("a@x.io and b@y.io")
        assert "a@x.io" not in out
        assert "b@y.io" not in out
        assert out.count("[REDACTED:email]") == 2

    def test_preserves_surrounding_text(self) -> None:
        out = redact("user user@host.com asks")
        assert out.startswith("user ")
        assert out.endswith(" asks")


class TestBearerTokenRedaction:
    def test_strips_bearer_authorization(self) -> None:
        out = redact("Authorization: Bearer abc123XYZ_token-value-789")
        assert "abc123XYZ_token-value-789" not in out
        assert "[REDACTED:bearer]" in out

    def test_strips_lowercase_bearer(self) -> None:
        out = redact("auth: bearer aLongOpaqueTokenValue123")
        assert "aLongOpaqueTokenValue123" not in out
        assert "[REDACTED:bearer]" in out


class TestApiKeyRedaction:
    def test_strips_anthropic_key(self) -> None:
        key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890"
        out = redact(f"export KEY={key}")
        assert key not in out
        assert "[REDACTED:api-key]" in out

    def test_strips_generic_sk_key(self) -> None:
        key = "sk-proj-1234567890abcdefghij"
        out = redact(f"key is {key} value")
        assert key not in out
        assert "[REDACTED:api-key]" in out


class TestTruncation:
    def test_short_payload_passes_through(self) -> None:
        out = redact("hello world", max_bytes=2048)
        assert out == "hello world"

    def test_long_payload_truncated_with_marker(self) -> None:
        long_text = "a" * 5000
        out = redact(long_text, max_bytes=200)
        assert len(out.encode("utf-8")) < 5000
        assert "TRUNCATED" in out

    def test_truncation_keeps_head_and_tail(self) -> None:
        text = "HEAD" + ("x" * 5000) + "TAIL"
        out = redact(text, max_bytes=100)
        assert out.startswith("HEAD")
        assert out.endswith("TAIL")
        assert "TRUNCATED" in out

    def test_empty_string_unchanged(self) -> None:
        assert redact("") == ""


class TestCombined:
    def test_redact_then_truncate(self) -> None:
        # An email should be replaced before truncation considers byte budget
        text = "x" * 100 + " bob@example.org " + "y" * 100
        out = redact(text, max_bytes=300)
        assert "bob@example.org" not in out
        assert "[REDACTED:email]" in out
