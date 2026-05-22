"""Discord DM fallback for the hardware-safety alerts subsystem.

A thin, defensive wrapper around the running discord.py bot. The alerts
dispatcher calls :meth:`DiscordAlertClient.dm_operator` on the ``alerting``
edge when the SPA is unreachable (PRD #228, slice 4).

Every failure mode — no bot, bot not yet ready, ``fetch_user`` raising, the
DM call raising (rate limit, closed DMs) — is caught and logged.
``dm_operator`` never raises, so a Discord hiccup can never perturb the
alert engine's state.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class DiscordAlertClient:
    """Best-effort one-line operator DM over the running discord.py bot.

    ``bot`` is the live ``discord.py`` bot (or ``None`` if Discord is not
    running). ``operator_id`` is the hardcoded operator user id, or ``None``
    when the fallback is disabled by config — in which case ``dm_operator``
    is a silent no-op.
    """

    def __init__(self, bot: Any, operator_id: int | None) -> None:
        self._bot = bot
        self._operator_id = operator_id

    async def dm_operator(self, content: str) -> None:
        """DM the configured operator. Never raises.

        Logs and drops on any failure so the alert engine's state is
        unaffected by Discord being down, rate-limited, or unconfigured.
        """
        if self._operator_id is None:
            return  # fallback disabled by config — nothing to do
        if self._bot is None:
            logger.warning("discord_alert_dropped", reason="bot unavailable")
            return
        try:
            ready = bool(self._bot.is_ready())
        except Exception:
            ready = False
        if not ready:
            logger.warning("discord_alert_dropped", reason="bot not ready")
            return
        try:
            user = await self._bot.fetch_user(self._operator_id)
            await user.send(content)
        except Exception:
            logger.warning("discord_alert_dm_failed", exc_info=True)
