"""Regex-based redaction + head/tail truncation for telemetry payloads.

Per PRD #38 US 11 & 12: secrets must be scrubbed and prompt slices bounded
before they ever reach the ring buffer.
"""

from __future__ import annotations

import re

REDACTION_MARKER = "[REDACTED]"
TRUNCATION_MARKER = "…[truncated]…"

DEFAULT_MAX_BYTES = 2048

# Order matters: more-specific patterns run before generic ones.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Anthropic API keys
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    # OpenAI keys (legacy + project)
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    # Bearer tokens
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    # GitHub tokens
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    # Email addresses
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    # Generic password=/secret=/token= assignments (quoted or bare)
    re.compile(
        r"""(?ix)
        \b(?:password|passwd|secret|token|api[_-]?key)
        \s*[:=]\s*
        (?:"[^"]+"|'[^']+'|[^\s,;]+)
        """
    ),
)


def redact(text: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Scrub known-secret patterns then truncate to ~max_bytes head+tail."""
    scrubbed = text
    for pattern in _PATTERNS:
        scrubbed = pattern.sub(REDACTION_MARKER, scrubbed)
    return _truncate(scrubbed, max_bytes)


def _truncate(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    half = max_bytes // 2
    head = encoded[:half].decode("utf-8", errors="ignore")
    tail = encoded[-half:].decode("utf-8", errors="ignore")
    return f"{head}{TRUNCATION_MARKER}{tail}"
