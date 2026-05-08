"""Tests for telemetry.redactor — covers PRD #38 user stories 11 and 12."""

from __future__ import annotations

from turing.telemetry.redactor import REDACTION_MARKER, TRUNCATION_MARKER, redact


class TestRedactsSecrets:
    def test_email_redacted(self):
        out = redact("contact alice@example.com for details")
        assert "alice@example.com" not in out
        assert REDACTION_MARKER in out

    def test_bearer_token_redacted(self):
        out = redact("Authorization: Bearer abc123XYZ.token-value_99")
        assert "abc123XYZ.token-value_99" not in out
        assert REDACTION_MARKER in out

    def test_anthropic_api_key_redacted(self):
        out = redact("ANTHROPIC_API_KEY=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA")
        assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA" not in out
        assert REDACTION_MARKER in out

    def test_openai_api_key_redacted(self):
        out = redact("key=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF")
        assert "sk-proj-abcdefghij" not in out
        assert REDACTION_MARKER in out

    def test_generic_secret_assignment_redacted(self):
        out = redact('password="hunter2-very-secret"')
        assert "hunter2-very-secret" not in out

    def test_non_secret_text_left_alone(self):
        text = "the quick brown fox jumps over the lazy dog"
        assert redact(text) == text

    def test_redactor_is_idempotent(self):
        once = redact("alice@example.com")
        twice = redact(once)
        assert once == twice


class TestTruncation:
    def test_short_input_unchanged(self):
        text = "hello world"
        assert redact(text, max_bytes=2048) == text

    def test_overlong_input_truncated_with_marker(self):
        text = "A" * 5000
        out = redact(text, max_bytes=2048)
        assert TRUNCATION_MARKER in out
        assert len(out.encode("utf-8")) <= 2048 + len(TRUNCATION_MARKER.encode("utf-8"))

    def test_truncation_keeps_head_and_tail(self):
        head = "HEAD_MARKER_AAA"
        tail = "TAIL_MARKER_ZZZ"
        text = head + ("x" * 5000) + tail
        out = redact(text, max_bytes=200)
        assert head in out
        assert tail in out
        assert TRUNCATION_MARKER in out

    def test_redaction_runs_before_truncation(self):
        # An email at the start should still be scrubbed even if the body is huge.
        text = "alice@example.com " + ("x" * 10_000)
        out = redact(text, max_bytes=512)
        assert "alice@example.com" not in out
