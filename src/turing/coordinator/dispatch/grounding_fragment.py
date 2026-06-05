"""Grounding fragment — carries a worker's reasoning trajectory home (#261).

ADR 0009 §2 requires the episode to record the **reasoning trajectory**, not
just the final answer — that captured reasoning is the SFT target. But the
episode is written **coordinator-side** (the episode store lives there), while
the reasoning is produced **worker-side** by the grounded research loop. The
worker therefore packs its reasoning into ``TaskResult.fragment`` (the same
free-form channel worker-proposed follow-up questions ride on), and the
dispatcher unpacks it when recording the episode.

This module lives in the neutral ``dispatch`` package because both the worker
(pack) and the coordinator (unpack) already depend on it for the envelopes —
keeping the fragment key + (de)serialisation in one place avoids a worker ↔
coordinator import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from turing.coordinator.dispatch.envelopes import TaskResult

GROUNDING_FRAGMENT_KEY = "grounding"
_REASONING = "reasoning"
_CONFIDENCE = "confidence"
_SOURCE_COUNT = "source_count"


def grounding_to_fragment(
    *,
    reasoning: Iterable[str],
    confidence: float = 0.0,
    source_count: int = 0,
) -> dict[str, object]:
    """Build the grounding payload a worker merges into ``TaskResult.fragment``.

    Only the reasoning trajectory is load-bearing (it becomes the episode's
    ``trajectory``); ``confidence`` / ``source_count`` ride along for telemetry.
    """
    return {
        GROUNDING_FRAGMENT_KEY: {
            _REASONING: [str(step) for step in reasoning],
            _CONFIDENCE: float(confidence),
            _SOURCE_COUNT: int(source_count),
        }
    }


def reasoning_from_result(result: TaskResult) -> tuple[str, ...]:
    """Extract the reasoning trajectory a worker attached, if any.

    Tolerant of a missing fragment / key / malformed shape — a worker that ran
    the generic loop (no grounding) simply yields an empty trajectory, which is
    exactly the pre-#261 behaviour, so the dispatcher stays backward-compatible.
    """
    fragment = result.fragment or {}
    block = fragment.get(GROUNDING_FRAGMENT_KEY)
    if not isinstance(block, dict):
        return ()
    raw = block.get(_REASONING, [])
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(step) for step in raw if str(step).strip())


def merge_fragments(*fragments: dict[str, object] | None) -> dict[str, object] | None:
    """Shallow-merge several fragment dicts (e.g. grounding + proposals).

    A worker that both grounds an answer *and* proposes follow-ups attaches both
    payloads to one ``TaskResult.fragment``; this merges them without either
    clobbering the other. Returns ``None`` when nothing was supplied so the
    envelope's ``fragment`` stays ``None`` in the no-extra-data case.
    """
    merged: dict[str, object] = {}
    for frag in fragments:
        if frag:
            merged.update(frag)
    return merged or None
