"""ntfy push fallback for the hardware-safety alerts subsystem.

A thin, defensive async HTTP client over the operator's ntfy topic, self-hosted
on the Surface coordinator (ADR-0010 §2). The alerts dispatcher calls
:meth:`NtfyAlertClient.ntfy_push` on the ``alerting`` edge when the SPA is
unreachable (PRD #228, slice B) — replacing the retired Discord-DM fallback.

The push is a plain ``POST {base_url}/{topic}`` with the one-line alert summary
as the request body, matching ntfy's publish-as-POST contract
(``scripts/coordinator/server.yml`` serves plaintext HTTP on ``:8090`` behind
Tailscale, so the topic name is the only access control).

Every failure mode — no topic, no base URL, a transport error, a non-2xx
status — is caught and logged. ``ntfy_push`` never raises, so an ntfy hiccup
can never perturb the alert engine's state.
"""

from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Match the project's network timeout posture (see ``tools/network.py``).
_TIMEOUT_SECONDS = 10.0


class NtfyAlertClient:
    """Best-effort one-line operator push to the coordinator's ntfy topic.

    ``base_url`` is the ntfy server URL (e.g. ``http://surface.<tailnet>.ts.net:8090``)
    and ``topic`` is the per-operator topic. Either being ``None`` disables the
    fallback — in which case :meth:`ntfy_push` is a silent no-op, mirroring the
    config gate the retired Discord client used.
    """

    def __init__(self, base_url: str | None, topic: str | None) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._topic = topic

    async def ntfy_push(self, content: str) -> None:
        """POST the alert text to the operator topic. Never raises.

        Logs and drops on any failure so the alert engine's state is
        unaffected by ntfy being down, unreachable, or unconfigured.
        """
        if self._topic is None or self._base_url is None:
            return  # fallback disabled by config — nothing to do
        url = f"{self._base_url}/{self._topic}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.post(url, content=content.encode("utf-8"))
                response.raise_for_status()
        except Exception:
            logger.warning("ntfy_alert_push_failed", exc_info=True)
