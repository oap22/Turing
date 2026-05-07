"""SinglePathRouter — classify → schedule → run → record.

This is the planner-bypass spine: a Discord message in the bound channel
goes through one classifier, one scheduler pick, one worker dispatch, and
lands one Episode. No DAG, no planner, no retry-to-different-worker.

The classifier's decision is emitted as a telemetry event so its choices
are auditable in the trace pane (slice 7) without dragging in extra
log-parsing infrastructure.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional, Protocol

from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.registry import CapabilityRegistry
from turing.coordinator.registry.manifest import CapabilityManifest
from turing.coordinator.scheduler.scheduler import Scheduler, Subtask
from turing.coordinator.single_path.classifier import (
    SpecialtyChoice,
    SpecialtyClassifier,
)
from turing.telemetry import get_telemetry
from turing.telemetry.bus import TelemetryEvent, now_ms

DispatchFn = Callable[..., Awaitable[str]]
ReportFn = Callable[[str], Awaitable[None]]


class NoMatchingWorkerError(RuntimeError):
    """Raised when the classifier picks a specialty no live worker advertises."""


class _AsyncClassifier(Protocol):
    async def classify(self, message: str) -> SpecialtyChoice: ...


def _classify_sync_or_async(
    classifier: SpecialtyClassifier | _AsyncClassifier, message: str
) -> SpecialtyChoice:
    """Bridge sync + async classifiers — the keyword path is sync, the LLM async."""
    import inspect

    result = classifier.classify(message)
    if inspect.isawaitable(result):
        # Caller is responsible for awaiting; we return the awaitable as-is
        # via a coroutine wrapper below. The router uses an async helper.
        raise TypeError("call _classify_async for awaitable classifiers")
    return result


class SinglePathRouter:
    def __init__(
        self,
        *,
        classifier: Any,
        registry: CapabilityRegistry,
        scheduler: Scheduler,
        episode_store: EpisodeStore,
        dispatch: DispatchFn,
    ) -> None:
        self._classifier = classifier
        self._registry = registry
        self._scheduler = scheduler
        self._store = episode_store
        self._dispatch = dispatch

    async def handle(
        self,
        *,
        message: str,
        task_id: str,
        report: Optional[ReportFn] = None,
    ) -> str:
        await _maybe_report(report, "classifying")
        choice = await _classify_async(self._classifier, message)
        self._emit_classifier_event(message=message, choice=choice)

        subtask = Subtask(
            subtask_id=f"{task_id}.s-0",
            specialty_required=choice.specialty,
        )
        manifest = self._scheduler.pick(subtask, self._registry)
        if manifest is None:
            raise NoMatchingWorkerError(
                f"no worker available for specialty {choice.specialty!r}"
            )
        await _maybe_report(report, "dispatched")

        await _maybe_report(report, "running")
        t0 = time.monotonic()
        try:
            result = await self._dispatch(manifest=manifest, message=message)
        except Exception:
            self._record_failed_episode(
                task_id=task_id,
                subtask=subtask,
                manifest=manifest,
                message=message,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
            raise

        latency_ms = int((time.monotonic() - t0) * 1000)
        self._record_success_episode(
            task_id=task_id,
            subtask=subtask,
            manifest=manifest,
            message=message,
            output_text=result,
            latency_ms=latency_ms,
        )
        await _maybe_report(report, "completed")
        return result

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _emit_classifier_event(
        *, message: str, choice: SpecialtyChoice
    ) -> None:
        tel = get_telemetry()
        tel.emit(
            TelemetryEvent(
                name="single_path.classify",
                stream="single_path.classify",
                seq=tel.next_seq("single_path.classify"),
                timestamp_ms=now_ms(),
                payload={
                    "specialty": choice.specialty,
                    "reason": choice.reason,
                    # Truncated message preview keeps the audit line useful
                    # without dragging the full prompt into telemetry.
                    "message_preview": message[:120],
                },
            )
        )

    def _record_success_episode(
        self,
        *,
        task_id: str,
        subtask: Subtask,
        manifest: CapabilityManifest,
        message: str,
        output_text: str,
        latency_ms: int,
    ) -> None:
        self._store.record(
            Episode(
                task_id=task_id,
                subtask_id=subtask.subtask_id,
                worker_id=manifest.worker_id,
                specialty=subtask.specialty_required,
                model_version=manifest.base_model,
                adapter_version=",".join(manifest.adapters) or "base",
                input_text=message,
                trajectory=("single_path.dispatch",),
                output_text=output_text,
                success=True,
                latency_ms=latency_ms,
                tokens_used=0,
                outcome=SubtaskState.COMPLETED,
                critic_score=0.0,
                recorded_at_ms=now_ms(),
            )
        )

    def _record_failed_episode(
        self,
        *,
        task_id: str,
        subtask: Subtask,
        manifest: CapabilityManifest,
        message: str,
        latency_ms: int,
    ) -> None:
        self._store.record(
            Episode(
                task_id=task_id,
                subtask_id=subtask.subtask_id,
                worker_id=manifest.worker_id,
                specialty=subtask.specialty_required,
                model_version=manifest.base_model,
                adapter_version=",".join(manifest.adapters) or "base",
                input_text=message,
                trajectory=("single_path.dispatch",),
                output_text="",
                success=False,
                latency_ms=latency_ms,
                tokens_used=0,
                outcome=SubtaskState.FAILED,
                critic_score=0.0,
                recorded_at_ms=now_ms(),
            )
        )


async def _classify_async(classifier: Any, message: str) -> SpecialtyChoice:
    import inspect

    result = classifier.classify(message)
    if inspect.isawaitable(result):
        return await result
    return result


async def _maybe_report(report: Optional[ReportFn], stage: str) -> None:
    if report is not None:
        await report(stage)
