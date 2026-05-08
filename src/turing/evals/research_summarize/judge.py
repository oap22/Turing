"""Judge protocol + Haiku-backed implementation + file cache.

The citation scorer asks a judge "does this source actually support this
claim?" and gets back one of supports / unrelated / contradicts. Tests
inject `FakeJudge`; production runs wrap a `ClaudeProvider` configured
for Haiku.

Verdicts are cached on disk keyed by hash(claim + source_text) so eval
re-runs don't re-bill Haiku.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from pathlib import Path

JudgeVerdict = Literal["supports", "unrelated", "contradicts"]
_VALID_VERDICTS: frozenset[JudgeVerdict] = frozenset({"supports", "unrelated", "contradicts"})


class Judge(Protocol):
    def judge(self, *, claim: str, source_id: str, source_text: str) -> JudgeVerdict: ...


def _cache_key(claim: str, source_text: str) -> str:
    h = hashlib.sha256()
    h.update(claim.strip().encode("utf-8"))
    h.update(b"\x00")
    h.update(source_text.strip().encode("utf-8"))
    return h.hexdigest()


class CachedJudge:
    """Wraps another Judge and persists verdicts to a JSON file."""

    def __init__(self, *, inner: Judge, cache_path: Path) -> None:
        self._inner = inner
        self._path = cache_path
        self._cache: dict[str, JudgeVerdict] = {}
        if cache_path.exists():
            try:
                raw = json.loads(cache_path.read_text())
                self._cache = {k: v for k, v in raw.items() if v in _VALID_VERDICTS}
            except (OSError, json.JSONDecodeError):
                # Corrupted cache: start fresh, don't poison the run.
                self._cache = {}

    def judge(self, *, claim: str, source_id: str, source_text: str) -> JudgeVerdict:
        key = _cache_key(claim, source_text)
        if key in self._cache:
            return self._cache[key]
        verdict = self._inner.judge(claim=claim, source_id=source_id, source_text=source_text)
        self._cache[key] = verdict
        self._flush()
        return verdict

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._cache, sort_keys=True, indent=2))


# ── Haiku-backed implementation (constructed by the harness CLI) ──────────


_PROMPT = (
    "You are grading whether a SOURCE supports a CLAIM. Reply with exactly "
    "one word: 'supports', 'unrelated', or 'contradicts'.\n\n"
    "- supports: the source clearly states or directly entails the claim\n"
    "- contradicts: the source asserts something incompatible with the claim\n"
    "- unrelated: neither — the source does not address the claim either way\n\n"
    "CLAIM: {claim}\n\nSOURCE:\n{source}\n\nVerdict:"
)


class HaikuJudge:
    """Calls a `ClaudeProvider` configured for Haiku and parses the verdict."""

    def __init__(self, provider, model: str = "claude-haiku-4-5"):
        self._provider = provider
        self._model = model

    def judge(self, *, claim: str, source_id: str, source_text: str) -> JudgeVerdict:
        import asyncio

        from turing.llm.base import Message, Role

        prompt = _PROMPT.format(claim=claim.strip(), source=source_text.strip())

        async def _call() -> str:
            resp = await self._provider.complete(
                messages=[Message(role=Role.USER, content=prompt)],
                system="",
                max_tokens=8,
                temperature=0.0,
            )
            return resp.content.strip().lower()

        loop = asyncio.new_event_loop()
        try:
            text = loop.run_until_complete(_call())
        finally:
            loop.close()

        for v in _VALID_VERDICTS:
            if text.startswith(v):
                return v  # type: ignore[return-value]
        # Conservative default: if the judge returns garbage, treat as
        # unrelated rather than crashing the eval run.
        return "unrelated"
