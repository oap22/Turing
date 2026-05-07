"""Bearer-token verification helper for the gateway.

The browser SPA sets a cookie; the CLI sends an ``Authorization: Bearer ...``
header. We accept either to keep the surface flexible without making the
auth check more complicated than a constant-time string compare.
"""

from __future__ import annotations

import hmac

COOKIE_NAME = "turing_gateway_token"


class GatewayAuth:
    def __init__(self, *, token: str) -> None:
        self._token = token

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def check(
        self,
        *,
        authorization_header: str | None,
        cookie_token: str | None,
    ) -> bool:
        """Constant-time check that one of the supplied credentials matches."""
        if not self._token:
            # No token configured ⇒ deny everything; this is safer than allow-all.
            return False
        provided = self._extract_bearer(authorization_header) or cookie_token or ""
        return hmac.compare_digest(provided, self._token)

    @staticmethod
    def _extract_bearer(header: str | None) -> str | None:
        if not header:
            return None
        prefix = "Bearer "
        if not header.startswith(prefix):
            return None
        return header[len(prefix) :].strip()
