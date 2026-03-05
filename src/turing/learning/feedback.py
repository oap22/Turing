"""Feedback collection from Discord reactions and explicit corrections."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from turing.memory.store import MemoryStore

logger = structlog.get_logger("turing.learning.feedback")


class FeedbackCollector:
    """Tracks success/failure via Discord reactions and explicit feedback.

    Records positive and negative signals for responses, as well as
    explicit corrections from users.  This data is stored in the audit
    log for later analysis and model improvement.
    """

    def __init__(self, memory_store: MemoryStore) -> None:
        self._memory_store = memory_store

    async def record_feedback(
        self,
        message_id: str,
        user_id: str,
        positive: bool,
    ) -> None:
        """Record feedback for a specific response.

        Args:
            message_id: The ID of the message being rated.
            user_id: The user providing the feedback.
            positive: True for positive feedback, False for negative.
        """
        feedback_type = "positive" if positive else "negative"

        await self._memory_store.log_audit(
            action="feedback",
            user_id=user_id,
            tool_name="",
            arguments={"message_id": message_id, "feedback": feedback_type},
            result=feedback_type,
            risk_level="low",
            approved=True,
        )

        logger.info(
            "feedback_recorded",
            message_id=message_id,
            user_id=user_id,
            positive=positive,
        )

    async def record_correction(
        self,
        user_id: str,
        original: str,
        correction: str,
    ) -> None:
        """Record a user correction of a previous response.

        Args:
            user_id: The user providing the correction.
            original: The original (incorrect) response.
            correction: The corrected information.
        """
        await self._memory_store.log_audit(
            action="correction",
            user_id=user_id,
            tool_name="",
            arguments={
                "original": original[:500],
                "correction": correction[:500],
            },
            result="correction_recorded",
            risk_level="low",
            approved=True,
        )

        # Also store as a fact for future reference
        await self._memory_store.add_fact(
            subject="correction",
            predicate="replaces",
            obj=f"{original[:200]} -> {correction[:200]}",
            confidence=0.9,
            source=f"user_correction:{user_id}",
        )

        logger.info(
            "correction_recorded",
            user_id=user_id,
            original_preview=original[:100],
            correction_preview=correction[:100],
        )
