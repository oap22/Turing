"""LLM-judge scorer for claim recall.

The judge labels which of a hand-authored ``expected_claims`` list appear in
the worker's output. It never invents new claims — that would be circular
(an LLM grading another LLM's invention) and would poison the gate. The
system prompt below enforces this structurally; a malformed response falls
to 0.0 so a parse failure fails the case loudly instead of skipping it.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from turing.llm.base import Message, Role

_SYSTEM_PROMPT = (
    "You are an evaluation judge. Given a worker output and a list of "
    "expected claims, return a JSON object with two keys: 'recalled' "
    "(claims from the input list that appear in the output, possibly "
    "paraphrased) and 'missing' (claims from the input list that do not). "
    "Do not invent new claims. Do not generate any claim text not present "
    "in the provided expected_claims list. Output only the JSON object — "
    "no preamble, no commentary."
)


class _LLMRouter(Protocol):
    async def complete(
        self, messages: list[Message], system: str = "", **_: Any
    ) -> Any: ...


async def claim_recall(
    *,
    output: str,
    expected_claims: list[str],
    llm: _LLMRouter,
) -> float:
    """Fraction of ``expected_claims`` the judge marks recalled, in [0, 1]."""
    if not expected_claims:
        return 1.0

    user_prompt = (
        f"expected_claims: {json.dumps(expected_claims)}\n\n"
        f"worker_output: {output}"
    )
    response = await llm.complete(
        messages=[Message(role=Role.USER, content=user_prompt)],
        system=_SYSTEM_PROMPT,
    )
    try:
        parsed = json.loads(getattr(response, "content", ""))
        recalled = parsed.get("recalled", [])
        if not isinstance(recalled, list):
            return 0.0
    except (json.JSONDecodeError, TypeError):
        return 0.0

    # Count distinct expected claims the judge says were recalled.
    expected_set = set(expected_claims)
    hits = sum(1 for claim in recalled if claim in expected_set)
    return hits / len(expected_claims)
