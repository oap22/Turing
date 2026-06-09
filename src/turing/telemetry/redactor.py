"""Pure-function payload redactor for telemetry events.

Strips PII and credentials, then truncates the result to a head+tail slice
inside a configurable byte budget.
"""

from __future__ import annotations

import re

DEFAULT_MAX_BYTES = 2048

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_BEARER_RE = re.compile(r"[Bb]earer\s+[A-Za-z0-9\-_.~+/=]{12,}")
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")
_GENERIC_KEY_RE = re.compile(
    r"\b(?:sk|pk|api|key)[-_][A-Za-z0-9\-_]{16,}\b",
    re.IGNORECASE,
)
# NATS nkey seeds: base32, start with 'S' (seed) then a role letter, ~58 chars.
_NKEY_SEED_RE = re.compile(r"\bS[A-Z2-7]{2}[A-Z2-7]{50,}\b")
# Opaque secrets assigned to a credential-named key, e.g. ``token=<value>``,
# ``password: <value>``, ``gateway_token=<value>``. Catches tokens that have no
# distinguishing prefix (like the gateway bearer token).
_ASSIGNED_SECRET_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:token|password|passwd|secret|api[-_]?key|nkey|seed)"
    r"\s*[=:]\s*)(['\"]?)([^\s'\"]{8,})",
)


def redact(text: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Return ``text`` with secrets scrubbed and truncated to ``max_bytes``.

    Replacement order matters: API keys and bearer tokens are stripped before
    the email regex so an embedded ``user@domain`` inside a key cannot leak
    via partial matches.
    """
    if not text:
        return text

    cleaned = _ANTHROPIC_KEY_RE.sub("[REDACTED:api-key]", text)
    cleaned = _BEARER_RE.sub("[REDACTED:bearer]", cleaned)
    cleaned = _GENERIC_KEY_RE.sub("[REDACTED:api-key]", cleaned)
    cleaned = _ASSIGNED_SECRET_RE.sub(r"\1\2[REDACTED:secret]", cleaned)
    cleaned = _NKEY_SEED_RE.sub("[REDACTED:nkey-seed]", cleaned)
    cleaned = _EMAIL_RE.sub("[REDACTED:email]", cleaned)

    encoded = cleaned.encode("utf-8")
    if len(encoded) <= max_bytes:
        return cleaned

    head_budget = max_bytes // 2
    tail_budget = max_bytes - head_budget
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    tail = encoded[-tail_budget:].decode("utf-8", errors="ignore")
    dropped = len(encoded) - head_budget - tail_budget
    return f"{head}[...TRUNCATED {dropped} bytes...]{tail}"
