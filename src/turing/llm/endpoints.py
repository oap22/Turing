"""Validation helpers for operator-authorized Ollama endpoints.

Peer presence is signed, but a signature authenticates the publisher, not the
HTTP URL embedded in its payload.  Callers must therefore compare a validated
peer URL with an exact operator-configured allowlist before creating a client.
"""

from __future__ import annotations

from urllib.parse import urlsplit


class OllamaEndpointError(ValueError):
    """Raised when an Ollama endpoint is not a safe, absolute HTTP URL."""


def validate_ollama_endpoint(value: object, *, field: str = "ollama endpoint") -> str:
    """Validate and return one exact Ollama base URL.

    This intentionally does not ban private or LAN addresses.  Operators may
    authorize those addresses by placing the exact URL in the peer allowlist.
    Credentials, query/fragment tricks, paths, control characters, and invalid
    ports are rejected before an HTTP client is constructed.
    """
    if not isinstance(value, str) or not value:
        raise OllamaEndpointError(f"{field} must be a non-empty URL")
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        raise OllamaEndpointError(f"{field} contains whitespace or control characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise OllamaEndpointError(f"{field} is malformed: {exc}") from exc
    if parsed.scheme not in {"http", "https"} or hostname is None:
        raise OllamaEndpointError(f"{field} must use http or https with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise OllamaEndpointError(f"{field} must not contain URL credentials")
    if port is not None and not 1 <= port <= 65535:
        raise OllamaEndpointError(f"{field} has an invalid port")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise OllamaEndpointError(f"{field} must not contain a path, query, or fragment")
    return value


def validate_ollama_allowlist(values: object) -> list[str]:
    """Validate exact endpoint entries from operator configuration."""
    if not isinstance(values, list):
        raise OllamaEndpointError("ollama peer allowlist must be a list of URLs")
    validated: list[str] = []
    for index, value in enumerate(values):
        endpoint = validate_ollama_endpoint(value, field=f"ollama peer allowlist[{index}]")
        if endpoint not in validated:
            validated.append(endpoint)
    return validated
