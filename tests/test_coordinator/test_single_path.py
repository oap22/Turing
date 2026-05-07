"""Tests for the single-specialty Discord path (#7).

The Discord cog itself is a thin wrapper over ``SinglePathRouter`` —
this test file pins the router's behaviour: classify → schedule → run
→ episode write, with auditable classifier decisions and live status
callbacks for the cog's edit-message loop.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from turing.coordinator.lifecycle.episode_store import Episode, EpisodeStore
from turing.coordinator.lifecycle.lifecycle import SubtaskState
from turing.coordinator.registry import CapabilityRegistry
from turing.coordinator.registry.manifest import CapabilityManifest
from turing.coordinator.scheduler.scheduler import Scheduler
from turing.coordinator.single_path import (
    KeywordSpecialtyClassifier,
    LLMSpecialtyClassifier,
    NoMatchingWorkerError,
    SinglePathRouter,
    SpecialtyChoice,
)
from turing.telemetry.bus import Telemetry, TelemetryEvent


def _manifest(specialty: str, score: float = 0.8) -> CapabilityManifest:
    return CapabilityManifest(
        worker_id=f"w-{specialty}",
        specialties=(specialty,),
        base_model="qwen2.5-7b",
        adapters=(),
        tools=(),
        hardware="cpu",
        max_concurrent=4,
        eval_score=score,
        public_key=b"\x00" * 32,
    )


def _registry(*manifests: CapabilityManifest) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for m in manifests:
        reg.register(m)
    return reg


# ── KeywordSpecialtyClassifier ─────────────────────────────────────────


class TestKeywordClassifier:
    def test_picks_research_for_research_keywords(self) -> None:
        clf = KeywordSpecialtyClassifier(
            mapping={
                "research-summarize": ("research", "summari", "paper"),
                "code-debug": ("debug", "stack trace", "exception"),
            },
            default="research-summarize",
        )
        choice = clf.classify("Please summarise this paper for me")
        assert choice.specialty == "research-summarize"

    def test_picks_code_for_debug_keywords(self) -> None:
        clf = KeywordSpecialtyClassifier(
            mapping={
                "research-summarize": ("research", "summari", "paper"),
                "code-debug": ("debug", "stack trace", "exception"),
            },
            default="research-summarize",
        )
        choice = clf.classify("debug this stack trace")
        assert choice.specialty == "code-debug"

    def test_falls_back_to_default(self) -> None:
        clf = KeywordSpecialtyClassifier(
            mapping={"x": ("foo",)}, default="research-summarize"
        )
        choice = clf.classify("nothing matches")
        assert choice.specialty == "research-summarize"
        assert choice.reason  # explanation present

    def test_choice_includes_reason(self) -> None:
        clf = KeywordSpecialtyClassifier(
            mapping={"code-debug": ("debug",)}, default="x"
        )
        choice = clf.classify("debug me")
        assert "debug" in choice.reason.lower()


# ── LLMSpecialtyClassifier ─────────────────────────────────────────────


class TestLLMClassifier:
    @pytest.mark.asyncio
    async def test_uses_llm_response(self) -> None:
        from turing.llm.base import LLMResponse

        llm = AsyncMock()
        llm.complete = AsyncMock(
            return_value=LLMResponse(content="research-summarize", model="claude")
        )
        clf = LLMSpecialtyClassifier(
            llm=llm, allowed=("research-summarize", "code-debug"), default="code-debug"
        )
        choice = await clf.classify("summarise this paper")
        assert choice.specialty == "research-summarize"

    @pytest.mark.asyncio
    async def test_falls_back_to_default_on_invalid_response(self) -> None:
        from turing.llm.base import LLMResponse

        llm = AsyncMock()
        llm.complete = AsyncMock(
            return_value=LLMResponse(content="not-a-real-specialty", model="claude")
        )
        clf = LLMSpecialtyClassifier(
            llm=llm, allowed=("research-summarize",), default="research-summarize"
        )
        choice = await clf.classify("anything")
        assert choice.specialty == "research-summarize"


# ── SinglePathRouter ───────────────────────────────────────────────────


class _StubClassifier:
    def __init__(self, specialty: str) -> None:
        self._specialty = specialty
        self.calls: list[str] = []

    def classify(self, message: str) -> SpecialtyChoice:
        self.calls.append(message)
        return SpecialtyChoice(specialty=self._specialty, reason="stub")


@pytest.fixture
def telemetry(monkeypatch: pytest.MonkeyPatch) -> Telemetry:
    fresh = Telemetry()
    monkeypatch.setattr("turing.telemetry.bus._SINGLETON", fresh)
    return fresh


class TestSinglePathRouter:
    @pytest.mark.asyncio
    async def test_happy_path_returns_worker_output(
        self, telemetry: Telemetry
    ) -> None:
        registry = _registry(_manifest("research-summarize"))
        scheduler = Scheduler()
        store = EpisodeStore()

        async def dispatch(*, manifest: CapabilityManifest, message: str) -> str:
            return f"[{manifest.worker_id}] {message[::-1]}"

        router = SinglePathRouter(
            classifier=_StubClassifier("research-summarize"),
            registry=registry,
            scheduler=scheduler,
            episode_store=store,
            dispatch=dispatch,
        )
        result = await router.handle(message="hello world", task_id="t-1")
        assert "[w-research-summarize]" in result
        assert "dlrow olleh" in result

    @pytest.mark.asyncio
    async def test_writes_episode_with_correct_specialty(
        self, telemetry: Telemetry
    ) -> None:
        registry = _registry(_manifest("research-summarize"))
        scheduler = Scheduler()
        store = EpisodeStore()

        async def dispatch(*, manifest: CapabilityManifest, message: str) -> str:
            return "result"

        router = SinglePathRouter(
            classifier=_StubClassifier("research-summarize"),
            registry=registry,
            scheduler=scheduler,
            episode_store=store,
            dispatch=dispatch,
        )
        await router.handle(message="hi", task_id="t-7")

        episodes = store.query(specialty="research-summarize")
        assert len(episodes) == 1
        ep = episodes[0]
        assert ep.task_id == "t-7"
        assert ep.outcome is SubtaskState.COMPLETED
        assert ep.success is True

    @pytest.mark.asyncio
    async def test_classifier_decision_emitted_as_telemetry(
        self, telemetry: Telemetry
    ) -> None:
        captured: list[TelemetryEvent] = []
        telemetry.add_sink(captured.append)

        registry = _registry(_manifest("research-summarize"))
        store = EpisodeStore()

        async def dispatch(*, manifest, message):  # type: ignore[no-untyped-def]
            return "ok"

        router = SinglePathRouter(
            classifier=_StubClassifier("research-summarize"),
            registry=registry,
            scheduler=Scheduler(),
            episode_store=store,
            dispatch=dispatch,
        )
        await router.handle(message="hi", task_id="t-1")

        names = [e.name for e in captured]
        assert "single_path.classify" in names
        choice_event = next(e for e in captured if e.name == "single_path.classify")
        assert choice_event.payload["specialty"] == "research-summarize"
        assert choice_event.payload["reason"] == "stub"

    @pytest.mark.asyncio
    async def test_status_reporter_called_at_each_stage(
        self, telemetry: Telemetry
    ) -> None:
        registry = _registry(_manifest("research-summarize"))
        store = EpisodeStore()
        statuses: list[str] = []

        async def report(stage: str) -> None:
            statuses.append(stage)

        async def dispatch(*, manifest, message):  # type: ignore[no-untyped-def]
            return "ok"

        router = SinglePathRouter(
            classifier=_StubClassifier("research-summarize"),
            registry=registry,
            scheduler=Scheduler(),
            episode_store=store,
            dispatch=dispatch,
        )
        await router.handle(message="hi", task_id="t-1", report=report)

        # The cog edits its message at each transition; we keep the status
        # vocabulary stable so the cog's emoji map is small.
        assert statuses == ["classifying", "dispatched", "running", "completed"]

    @pytest.mark.asyncio
    async def test_no_matching_worker_raises(self, telemetry: Telemetry) -> None:
        registry = _registry()  # empty
        store = EpisodeStore()

        async def dispatch(*, manifest, message):  # type: ignore[no-untyped-def]
            return "ok"

        router = SinglePathRouter(
            classifier=_StubClassifier("research-summarize"),
            registry=registry,
            scheduler=Scheduler(),
            episode_store=store,
            dispatch=dispatch,
        )
        with pytest.raises(NoMatchingWorkerError):
            await router.handle(message="hi", task_id="t-1")

    @pytest.mark.asyncio
    async def test_dispatch_failure_records_failed_episode(
        self, telemetry: Telemetry
    ) -> None:
        registry = _registry(_manifest("research-summarize"))
        store = EpisodeStore()

        async def dispatch(*, manifest, message):  # type: ignore[no-untyped-def]
            raise RuntimeError("worker exploded")

        router = SinglePathRouter(
            classifier=_StubClassifier("research-summarize"),
            registry=registry,
            scheduler=Scheduler(),
            episode_store=store,
            dispatch=dispatch,
        )
        with pytest.raises(RuntimeError, match="worker exploded"):
            await router.handle(message="hi", task_id="t-9")

        episodes = store.query(specialty="research-summarize")
        assert len(episodes) == 1
        assert episodes[0].outcome is SubtaskState.FAILED
        assert episodes[0].success is False
