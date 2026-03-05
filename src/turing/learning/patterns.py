"""Pattern extraction from conversations using LLM analysis."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import structlog

from turing.llm.base import Message, Role

if TYPE_CHECKING:
    from turing.llm.router import LLMRouter
    from turing.memory.store import MemoryStore

logger = structlog.get_logger("turing.learning.patterns")


class PatternExtractor:
    """Extracts facts, preferences, and patterns from conversations using LLM."""

    def __init__(self, llm_router: LLMRouter, memory_store: MemoryStore) -> None:
        self._llm_router = llm_router
        self._memory_store = memory_store

    async def extract(self, conversation_id: str) -> dict[str, Any]:
        """Analyze recent conversation and extract structured knowledge.

        Extracts:
        - Facts (subject-predicate-object triples)
        - User preferences (key-value pairs)
        - Patterns (recurring requests, preferred tools)

        Returns a dictionary with the extracted data and stores facts
        and preferences in the memory store.
        """
        # Get recent messages from the conversation
        messages = await self._memory_store.get_messages(conversation_id, limit=20)

        if not messages:
            logger.debug("pattern_extract_no_messages", conversation_id=conversation_id)
            return {"facts": [], "preferences": [], "patterns": []}

        # Build conversation text for analysis
        conversation_text = "\n".join(
            f"{msg.get('role', 'unknown')}: {msg.get('content', '')}"
            for msg in messages
            if msg.get("content")
        )

        if len(conversation_text) < 50:
            return {"facts": [], "preferences": [], "patterns": []}

        extraction_prompt = (
            "Analyze the following conversation and extract structured knowledge.\n\n"
            "Return a JSON object with three keys:\n"
            '1. "facts": array of {subject, predicate, object} triples '
            "(e.g. {\"subject\": \"Python\", \"predicate\": \"is\", \"object\": \"a programming language\"})\n"
            '2. "preferences": array of {user_id, key, value} for user preferences '
            "(e.g. {\"user_id\": \"123\", \"key\": \"language\", \"value\": \"Python\"})\n"
            '3. "patterns": array of strings describing recurring patterns\n\n'
            "Only extract clear, factual information. Do not speculate.\n"
            "If nothing useful can be extracted, return empty arrays.\n\n"
            f"Conversation:\n{conversation_text}\n\n"
            "Respond ONLY with the JSON object."
        )

        try:
            response = await self._llm_router.route(
                messages=[Message(role=Role.USER, content=extraction_prompt)],
                system="You are a knowledge extraction system. Respond only with valid JSON.",
                max_tokens=1024,
                temperature=0.2,
            )

            extracted = self._parse_response(response.content)

            # Store extracted facts
            facts_stored = 0
            for fact in extracted.get("facts", []):
                subject = fact.get("subject", "")
                predicate = fact.get("predicate", "")
                obj = fact.get("object", "")
                if subject and predicate and obj:
                    await self._memory_store.add_fact(
                        subject=subject,
                        predicate=predicate,
                        obj=obj,
                        confidence=0.8,
                        source=f"conversation:{conversation_id}",
                    )
                    facts_stored += 1

            # Store extracted preferences
            prefs_stored = 0
            for pref in extracted.get("preferences", []):
                user_id = pref.get("user_id", "")
                key = pref.get("key", "")
                value = pref.get("value", "")
                if user_id and key and value:
                    await self._memory_store.set_user_preference(
                        user_id=user_id,
                        key=key,
                        value=value,
                    )
                    prefs_stored += 1

            logger.info(
                "patterns_extracted",
                conversation_id=conversation_id,
                facts=facts_stored,
                preferences=prefs_stored,
                patterns=len(extracted.get("patterns", [])),
            )

            return extracted

        except Exception as exc:
            logger.error(
                "pattern_extraction_error",
                conversation_id=conversation_id,
                error=str(exc),
            )
            return {"facts": [], "preferences": [], "patterns": []}

    def _parse_response(self, response_text: str) -> dict[str, Any]:
        """Parse the LLM response into a structured dictionary."""
        text = response_text.strip()

        # Remove markdown code fences if present
        code_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if code_match:
            text = code_match.group(1).strip()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return {
                    "facts": parsed.get("facts", []),
                    "preferences": parsed.get("preferences", []),
                    "patterns": parsed.get("patterns", []),
                }
        except json.JSONDecodeError:
            logger.warning("pattern_parse_failed", response=response_text[:200])

        return {"facts": [], "preferences": [], "patterns": []}
