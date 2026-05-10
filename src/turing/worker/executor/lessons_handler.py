"""Synthesis-worker handler for `SubtaskKind.EXTRACT_LESSONS` (issue #121).

The dispatcher serializes a `{"specialty", "episodes": [...]}` payload into
`SubtaskDispatch.prompt`; the handler invokes a worker-side LLM extractor
callable and returns the list of `LessonCandidate`-shaped dicts on the
result envelope.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from turing.learning.lessons.nightly import LessonCandidate

if TYPE_CHECKING:
    from turing.coordinator.dispatch import SubtaskDispatch


ExtractorFn = Callable[[str, Sequence[dict[str, Any]]], Awaitable[list[LessonCandidate]]]


def make_extract_lessons_handler(extract_fn: ExtractorFn):
    """Build a kind handler returning JSON-encoded LessonCandidate list."""

    async def handle(envelope: SubtaskDispatch) -> dict[str, object]:
        body = json.loads(envelope.prompt)
        specialty = body["specialty"]
        episodes = body.get("episodes", [])
        candidates = await extract_fn(specialty, episodes)
        return {
            "status": "COMPLETED",
            "output": json.dumps(
                [
                    {
                        "task_id": c.task_id,
                        "text": c.text,
                        "embedding": list(c.embedding),
                    }
                    for c in candidates
                ]
            ),
        }

    return handle


def parse_candidates(output: str) -> list[LessonCandidate]:
    raw = json.loads(output)
    return [
        LessonCandidate(
            task_id=str(item["task_id"]),
            text=str(item["text"]),
            embedding=tuple(float(x) for x in item["embedding"]),
        )
        for item in raw
    ]
