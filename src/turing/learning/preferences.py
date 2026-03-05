"""User preference tracking and injection into system prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from turing.memory.store import MemoryStore

logger = structlog.get_logger("turing.learning.preferences")


class PreferenceTracker:
    """Tracks and injects per-user preferences.

    Reads stored preferences from the memory store and formats them
    for injection into the agent's system prompt.
    """

    def __init__(self, memory_store: MemoryStore) -> None:
        self._memory_store = memory_store

    async def get_user_context(self, user_id: str) -> str:
        """Get a formatted preference string for system prompt injection.

        Returns a human-readable summary of the user's preferences,
        or an empty string if no preferences are stored.
        """
        preferences = await self._memory_store.get_user_preferences(user_id)

        if not preferences:
            return ""

        lines = ["User preferences:"]
        for key, value in preferences.items():
            lines.append(f"  - {key}: {value}")

        return "\n".join(lines)

    async def update_preference(
        self,
        user_id: str,
        key: str,
        value: str,
        user_name: str = "",
    ) -> None:
        """Update a user preference in the memory store."""
        await self._memory_store.set_user_preference(
            user_id=user_id,
            key=key,
            value=value,
            user_name=user_name,
        )
        logger.info(
            "preference_updated",
            user_id=user_id,
            key=key,
            value=value,
        )

    async def delete_preference(self, user_id: str, key: str) -> None:
        """Remove a user preference from the memory store."""
        await self._memory_store.delete_user_preference(user_id, key)
        logger.info("preference_deleted", user_id=user_id, key=key)

    async def get_all_preferences(self, user_id: str) -> dict[str, str]:
        """Return all stored preferences for a user."""
        return await self._memory_store.get_user_preferences(user_id)
