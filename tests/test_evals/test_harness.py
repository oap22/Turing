"""Tests for the ``research-summarize`` eval harness.

Drives the scoring loop (:func:`run`) across its branches — plain scorer,
embedder- and judge-aware scorers, unknown ``scoring_fn``, worker exceptions,
and ``NotImplementedError`` — plus case loading, the worker builders (mocked at
their import boundaries), and the ``main`` CLI dispatch.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turing.evals.research_summarize import harness
from turing.evals.research_summarize.schema import EvalCase, Expected, SourceDoc
from turing.evals.research_summarize.scoring import ScoreResult


def _case(case_id: str, category: str, scoring_fn: str) -> EvalCase:
    return EvalCase(
        id=case_id,
        category=category,  # type: ignore[arg-type]
        prompt="summarise this",
        source_docs=[SourceDoc(id="s1", text="body")],
        expected=Expected(must_contain_claims=["alpha"]),
        scoring_fn=scoring_fn,
    )


class TestRun:
    def test_plain_scorer_aggregates_by_category(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            harness, "SCORERS", {"plain": lambda summary, case: ScoreResult(score=1.0)}
        )
        cases = [_case("c1", "claim_preservation", "plain")]
        report = harness.run(lambda case: "summary", cases)

        assert report["by_category"]["claim_preservation"] == 1.0
        # claim_preservation weight is 0.45 in the aggregate.
        assert report["aggregate"] == pytest.approx(0.45)
        assert report["per_case"][0] == {
            "id": "c1",
            "category": "claim_preservation",
            "score": 1.0,
        }

    def test_unknown_scoring_fn_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(harness, "SCORERS", {})
        with pytest.raises(ValueError, match="Unknown scoring_fn"):
            harness.run(lambda case: "s", [_case("c1", "claim_preservation", "missing")])

    def test_embedder_is_passed_to_claim_preservation_scorer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def scorer(summary: str, case: EvalCase, *, embedder: Any) -> ScoreResult:
            seen["embedder"] = embedder
            return ScoreResult(score=0.8)

        monkeypatch.setattr(harness, "SCORERS", {"score_claim_preservation_v1": scorer})
        sentinel = object()
        report = harness.run(
            lambda case: "s",
            [_case("c1", "claim_preservation", "score_claim_preservation_v1")],
            embedder=sentinel,  # type: ignore[arg-type]
        )
        assert seen["embedder"] is sentinel
        assert report["by_category"]["claim_preservation"] == 0.8

    def test_judge_is_passed_to_citation_correctness_scorer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def scorer(summary: str, case: EvalCase, *, judge: Any) -> ScoreResult:
            seen["judge"] = judge
            return ScoreResult(score=0.5)

        monkeypatch.setattr(harness, "SCORERS", {"score_citation_correctness_v1": scorer})
        sentinel = object()
        harness.run(
            lambda case: "s",
            [_case("c1", "citation_correctness", "score_citation_correctness_v1")],
            judge=sentinel,  # type: ignore[arg-type]
        )
        assert seen["judge"] is sentinel

    def test_worker_exception_scores_zero_without_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            harness, "SCORERS", {"plain": lambda summary, case: ScoreResult(score=1.0)}
        )

        def boom(case: EvalCase) -> str:
            raise RuntimeError("worker died")

        report = harness.run(boom, [_case("c1", "claim_preservation", "plain")])
        assert report["by_category"]["claim_preservation"] == 0.0

    def test_not_implemented_scorer_scores_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def scorer(summary: str, case: EvalCase) -> ScoreResult:
            raise NotImplementedError("todo")

        monkeypatch.setattr(harness, "SCORERS", {"plain": scorer})
        report = harness.run(lambda case: "s", [_case("c1", "voice_match", "plain")])
        assert report["by_category"]["voice_match"] == 0.0


class TestLoadCases:
    def test_skips_blank_and_comment_lines(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        case = _case("c1", "claim_preservation", "plain")
        lines = ["// a comment", "", "   ", case.model_dump_json()]
        (tmp_path / "cases.jsonl").write_text("\n".join(lines))
        monkeypatch.setattr(harness, "CASES_DIR", tmp_path)

        loaded = harness.load_cases()
        assert [c.id for c in loaded] == ["c1"]


def test_fixture_worker_echoes_first_claim() -> None:
    case = _case("c1", "claim_preservation", "plain")
    assert harness.fixture_worker(case) == "alpha"


def test_fixture_worker_handles_no_claims() -> None:
    case = _case("c1", "claim_preservation", "plain")
    case.expected.must_contain_claims = []
    assert harness.fixture_worker(case) == "(empty)"


class TestBuildDirectWorker:
    def test_raises_without_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
            harness._build_direct_worker("claude-sonnet-4-5", 60.0)

    def test_builds_provider_when_key_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        sentinel = object()
        with (
            patch("turing.llm.cloud.ClaudeProvider") as provider_cls,
            patch(
                "turing.evals.research_summarize.workers.direct_worker",
                return_value=sentinel,
            ) as direct,
        ):
            result = harness._build_direct_worker("claude-sonnet-4-5", 30.0)

        provider_cls.assert_called_once()
        direct.assert_called_once()
        assert result is sentinel


class TestBuildCoordinatorWorker:
    def test_connects_and_returns_worker(self) -> None:
        sentinel = object()
        with (
            patch(
                "turing.transport.nats_bus.NatsBus.connect",
                new=AsyncMock(return_value=MagicMock()),
            ) as connect,
            patch(
                "turing.transport.signer.MessageSigner.generate",
                return_value=SimpleNamespace(public_key=b"pk"),
            ),
            patch("turing.transport.signed_transport.SignedTransport"),
            patch("turing.coordinator.dispatch.SubtaskDispatchClient"),
            patch(
                "turing.evals.research_summarize.workers.coordinator_worker",
                return_value=sentinel,
            ) as coord,
        ):
            result = harness._build_coordinator_worker(
                nats_url="nats://127.0.0.1:4222",
                worker_id="w1",
                deadline_offset_s=120.0,
            )

        connect.assert_awaited_once()
        coord.assert_called_once()
        assert result is sentinel


class TestMain:
    def test_main_fixture_worker_prints_report(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["prog", "--worker", "fixture"])
        monkeypatch.setattr(harness, "load_cases", lambda: [])

        harness.main()

        report = json.loads(capsys.readouterr().out)
        assert report["aggregate"] == 0.0

    def test_main_direct_branch_uses_builder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["prog", "--worker", "direct"])
        monkeypatch.setattr(harness, "load_cases", lambda: [])
        called: dict[str, Any] = {}

        def fake_builder(model: str, timeout_s: float) -> Any:
            called["model"] = model
            return harness.fixture_worker

        monkeypatch.setattr(harness, "_build_direct_worker", fake_builder)
        harness.main()
        assert "model" in called

    def test_main_coordinator_branch_uses_builder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["prog", "--worker", "coordinator"])
        monkeypatch.setattr(harness, "load_cases", lambda: [])
        called: dict[str, Any] = {}

        def fake_builder(**kwargs: Any) -> Any:
            called.update(kwargs)
            return harness.fixture_worker

        monkeypatch.setattr(harness, "_build_coordinator_worker", fake_builder)
        harness.main()
        assert "nats_url" in called
