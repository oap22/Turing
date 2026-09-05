"""Invariant tests for the research contracts.

These are not coverage tests. Each block pins one property from
``research/briefs/2026-08-12-autonomous-research-agent.md`` that the rest of
the system is allowed to assume, and that an unattended solver has an incentive
to erode:

* a verifier cannot be mutated;
* an escalation reply carries no advice channel;
* practice and held-out results stay distinguishable, and held-out never
  reaches the loop-2 self-edit summary;
* a cap only ever grows, and only from an operator decision;
* an interrupted attempt resumes rather than restarts;
* round records carry lineage and expose no cross-type average.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any

import pytest

from turing.research.contracts import (
    CORE_METRICS_FIELDS,
    HARNESS_FAILURE_KEY,
    RESERVED_METRICS_FIELDS,
    RESERVED_METRICS_KEYS,
    SCORE_SCALE_LEADERBOARD_PERCENTILE,
    SCORE_SCALE_SPEEDUP,
    TERMINAL_ATTEMPT_STATES,
    Attempt,
    AttemptState,
    Cap,
    CapConsumption,
    CapDimension,
    CapExtension,
    CapExtensionError,
    ContractViolationError,
    EngineIdentity,
    EscalationDecision,
    EscalationProtocolError,
    EscalationReason,
    EscalationRequest,
    EscalationVerdict,
    EvalSetMismatchError,
    FrozenVerifierError,
    Problem,
    ProblemType,
    RoundCost,
    RoundDelta,
    RoundRecord,
    Split,
    TypeScore,
    VerificationResult,
    Verifier,
    round_cells,
)

# --------------------------------------------------------------------------- #
# Fixtures / doubles
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StubVerifier(Verifier):
    """A well-behaved verifier: frozen, async, reports a continuous score."""

    score: float = 2.0
    passed: bool = True

    async def verify(self, workspace: Path) -> VerificationResult:
        return VerificationResult(
            problem_id=self.problem_id,
            verifier_id=self.verifier_id,
            score=self.score,
            passed_correctness=self.passed,
            score_scale=self.score_scale,
            raw_measurements={"baseline_seconds": 21.9, "candidate_seconds": 21.9 / self.score},
            detail="stub",
        )


def make_verifier(problem_id: str = "speedup-claim-scorer", **kwargs: Any) -> StubVerifier:
    kwargs.setdefault("score_scale", "speedup_ratio")
    return StubVerifier(
        verifier_id=f"{problem_id}-v1",
        problem_id=problem_id,
        description="stub verifier",
        **kwargs,
    )


def make_problem(
    problem_id: str = "speedup-claim-scorer",
    *,
    split: Split = Split.PRACTICE,
    problem_type: ProblemType = ProblemType.SPEEDUP,
) -> Problem:
    return Problem(
        id=problem_id,
        problem_type=problem_type,
        goal="Make the claim-preservation scorer faster without changing its output.",
        workspace_template=Path("/tmp/templates") / problem_id,
        verifier=make_verifier(problem_id),
        split=split,
    )


def make_cap(**kwargs: Any) -> Cap:
    defaults: dict[str, Any] = {
        "max_steps": 100,
        "max_tokens": 500_000,
        "max_wall_clock_seconds": 3600.0,
    }
    defaults.update(kwargs)
    return Cap(**defaults)


def make_attempt(**kwargs: Any) -> Attempt:
    defaults: dict[str, Any] = {
        "attempt_id": "a-1",
        "problem_id": "speedup-claim-scorer",
        "round_id": "round-00",
        "seed": 7,
        "workspace_path": Path("/tmp/turing-workspace/speedup-claim-scorer"),
        "cap": make_cap(),
    }
    defaults.update(kwargs)
    return Attempt(**defaults)


def make_engine() -> EngineIdentity:
    return EngineIdentity(
        backend="claude",
        orchestrator_model="claude-opus-5",
        substep_model="claude-haiku-tier",
        scaffold_git_sha="0" * 40,
    )


def make_round(
    *,
    round_index: int = 0,
    parent_round_id: str | None = None,
    eval_set_hash: str = "corpus-v1",
    type_scores: tuple[TypeScore, ...] | None = None,
    deltas: tuple[RoundDelta, ...] = (),
    escalation_count: int = 0,
) -> RoundRecord:
    if type_scores is None:
        type_scores = (
            TypeScore(
                problem_type=ProblemType.SPEEDUP,
                split=Split.PRACTICE,
                scores={"speedup-a": 2.0, "speedup-b": 4.0},
                correctness_passes=2,
            ),
            TypeScore(
                problem_type=ProblemType.SPEEDUP,
                split=Split.HELD_OUT,
                scores={"speedup-c": 9.5},
                correctness_passes=1,
            ),
        )
    return RoundRecord(
        round_index=round_index,
        run_id=f"2026-08-12-loop-r{round_index:02d}",
        parent_round_id=parent_round_id,
        eval_set_hash=eval_set_hash,
        engine=make_engine(),
        type_scores=type_scores,
        deltas=deltas,
        cost=RoundCost(wall_clock_seconds=7200.0, tokens=1_200_000, attempts=11),
        escalation_count=escalation_count,
        created_at_ms=1_760_000_000_000,
    )


# --------------------------------------------------------------------------- #
# Invariant 1 — the verifier cannot be mutated
# --------------------------------------------------------------------------- #


class TestVerifierIsFrozen:
    def test_field_assignment_is_rejected(self) -> None:
        verifier = make_verifier()
        with pytest.raises(FrozenInstanceError):
            verifier.score = 99.0  # type: ignore[misc]

    def test_field_deletion_is_rejected(self) -> None:
        verifier = make_verifier()
        with pytest.raises(FrozenInstanceError):
            del verifier.score  # type: ignore[misc]

    def test_new_attribute_cannot_be_attached(self) -> None:
        verifier = make_verifier()
        with pytest.raises(FrozenInstanceError):
            verifier.relaxed_threshold = 0.0  # type: ignore[attr-defined]

    def test_non_frozen_dataclass_subclass_is_impossible(self) -> None:
        """Python itself refuses this; the base being frozen is what buys it."""
        with pytest.raises(TypeError, match="non-frozen dataclass"):

            @dataclass  # deliberately not frozen
            class Mutable(Verifier):
                async def verify(self, workspace: Path) -> VerificationResult:  # pragma: no cover
                    raise NotImplementedError

    def test_plain_subclass_still_cannot_assign(self) -> None:
        """A non-dataclass subclass inherits the frozen __setattr__."""

        class Plain(Verifier):
            async def verify(self, workspace: Path) -> VerificationResult:  # pragma: no cover
                raise NotImplementedError

        plain = Plain("v", "p", "d", "s")
        with pytest.raises(FrozenInstanceError):
            plain.problem_id = "other"  # type: ignore[misc]

    def test_subclass_reinstating_setattr_is_refused_at_class_creation(self) -> None:
        with pytest.raises(FrozenVerifierError, match="__setattr__"):

            class Sneaky(Verifier):
                def __setattr__(self, name: str, value: Any) -> None:  # pragma: no cover
                    object.__setattr__(self, name, value)

                async def verify(self, workspace: Path) -> VerificationResult:  # pragma: no cover
                    raise NotImplementedError

    def test_subclass_reinstating_delattr_is_refused_at_class_creation(self) -> None:
        with pytest.raises(FrozenVerifierError, match="__delattr__"):

            class Sneaky(Verifier):
                def __delattr__(self, name: str) -> None:  # pragma: no cover
                    object.__delattr__(self, name)

                async def verify(self, workspace: Path) -> VerificationResult:  # pragma: no cover
                    raise NotImplementedError

    def test_slotted_frozen_subclass_is_allowed(self) -> None:
        """dataclass(slots=True) re-fires __init_subclass__; the guard must not trip."""

        @dataclass(frozen=True, slots=True)
        class Slotted(Verifier):
            async def verify(self, workspace: Path) -> VerificationResult:  # pragma: no cover
                raise NotImplementedError

        instance = Slotted("v", "p", "d", "s")
        with pytest.raises(FrozenInstanceError):
            instance.problem_id = "other"  # type: ignore[misc]

    def test_problem_rejects_a_writable_verifier(self) -> None:
        """The behavioural probe catches a route the class checks did not."""

        class Escaped(Verifier):
            async def verify(self, workspace: Path) -> VerificationResult:  # pragma: no cover
                raise NotImplementedError

        # Bypass __init_subclass__ entirely by patching after class creation —
        # exactly the shape a determined self-edit would take.
        Escaped.__setattr__ = object.__setattr__  # type: ignore[method-assign,assignment]

        escaped = Escaped("v", "leaky", "d", "s")
        with pytest.raises(FrozenVerifierError, match="accepted attribute assignment"):
            Problem(
                id="leaky",
                problem_type=ProblemType.SPEEDUP,
                goal="g",
                workspace_template=Path("/tmp/t"),
                verifier=escaped,
                split=Split.PRACTICE,
            )

    def test_problem_rejects_a_verifier_for_a_different_problem(self) -> None:
        with pytest.raises(ContractViolationError, match="grades problem"):
            Problem(
                id="problem-a",
                problem_type=ProblemType.KAGGLE,
                goal="g",
                workspace_template=Path("/tmp/t"),
                verifier=make_verifier("problem-b"),
                split=Split.PRACTICE,
            )

    def test_problem_itself_is_frozen(self) -> None:
        problem = make_problem()
        with pytest.raises(FrozenInstanceError):
            problem.verifier = make_verifier()  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# VerificationResult
# --------------------------------------------------------------------------- #


class TestVerificationResult:
    async def test_stub_verifier_round_trip(self) -> None:
        verifier = make_verifier()
        result = await verifier.verify(Path("/tmp/ws"))
        assert result.problem_id == verifier.problem_id
        assert result.verifier_id == verifier.verifier_id
        assert result.score == pytest.approx(2.0)
        assert result.passed_correctness is True

    def test_fast_but_wrong_is_distinguishable_from_slow_but_right(self) -> None:
        fast_wrong = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=25.5,
            passed_correctness=False,
            score_scale="speedup_ratio",
        )
        slow_right = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=1.02,
            passed_correctness=True,
            score_scale="speedup_ratio",
        )
        assert fast_wrong.score > slow_right.score
        assert not fast_wrong.passed_correctness
        assert slow_right.passed_correctness

    def test_raw_measurements_are_read_only(self) -> None:
        source = {"baseline_seconds": 24.5}
        result = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=2.0,
            passed_correctness=True,
            score_scale="speedup_ratio",
            raw_measurements=source,
        )
        with pytest.raises(TypeError):
            result.raw_measurements["baseline_seconds"] = 0.0  # type: ignore[index]
        # And the caller's dict is copied, not aliased.
        source["baseline_seconds"] = 0.0
        assert result.raw_measurements["baseline_seconds"] == pytest.approx(24.5)

    def test_harness_failure_is_not_a_grade(self) -> None:
        broken = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=0.0,
            passed_correctness=False,
            score_scale="speedup_ratio",
            raw_measurements={HARNESS_FAILURE_KEY: 1.0},
        )
        graded = VerificationResult(
            problem_id="p",
            verifier_id="v",
            score=0.0,
            passed_correctness=False,
            score_scale="speedup_ratio",
        )
        assert broken.harness_failed is True
        assert graded.harness_failed is False

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_scores_are_rejected(self, bad: float) -> None:
        with pytest.raises(ContractViolationError, match="finite"):
            VerificationResult(
                problem_id="p",
                verifier_id="v",
                score=bad,
                passed_correctness=True,
                score_scale="speedup_ratio",
            )


# --------------------------------------------------------------------------- #
# score_scale — free-form, but it still has to survive as a metrics key
# --------------------------------------------------------------------------- #


class TestScoreScaleIsRefusedWhereItIsDeclared:
    """The runner names the emitted metrics key after ``score_scale``.

    A scale that cannot be a metrics key — one of the axis/meta fields the
    desktop excludes, one of the core per-step fields every line already
    carries, an empty string, or the agent's ``diag_`` prefix — used to raise
    from ``MetricsLine.to_json`` at the attempt's *first verification*: after
    the workspace was copied, after the solver ran, one problem at a time,
    forever, because the scale is a property of the problem and every re-drive
    reproduces it.

    All of those are refused here instead, where the scale is declared, so a
    corpus carrying one cannot be built and no compute is spent on it. The
    names matter: ``progress`` is a natural scale for a problem graded on
    fraction-of-target, ``step`` for one graded on step count, ``tokens_used``
    for one graded on token efficiency. Nothing about them looks wrong when an
    operator writes them down, which is exactly why the refusal has to be
    loud and early.
    """

    @pytest.mark.parametrize("scale", sorted(RESERVED_METRICS_KEYS))
    def test_every_reserved_key_is_refused_as_a_verifier_scale(self, scale: str) -> None:
        with pytest.raises(ContractViolationError) as caught:
            make_verifier(score_scale=scale)
        message = str(caught.value)
        assert repr(scale) in message
        assert ("axis/meta" in message) is (scale in RESERVED_METRICS_FIELDS)
        assert ("core field" in message) is (scale in CORE_METRICS_FIELDS)

    @pytest.mark.parametrize("scale", sorted(RESERVED_METRICS_KEYS))
    def test_every_reserved_key_is_refused_on_a_verification_result(self, scale: str) -> None:
        with pytest.raises(ContractViolationError, match="score_scale"):
            VerificationResult(
                problem_id="p",
                verifier_id="v",
                score=1.0,
                passed_correctness=True,
                score_scale=scale,
            )

    def test_an_empty_scale_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="empty score_scale"):
            make_verifier(score_scale="")

    def test_a_diagnostics_prefixed_scale_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="diag_"):
            make_verifier(score_scale="diag_speedup")

    def test_a_non_string_scale_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="plain str"):
            make_verifier(score_scale=1.0)

    @pytest.mark.parametrize(
        "scale",
        [
            SCORE_SCALE_SPEEDUP,
            SCORE_SCALE_LEADERBOARD_PERCENTILE,
            "val_loss",
            "auc",
            "progress_toward_target",
            "steps_to_solution",
        ],
    )
    def test_the_conventional_and_ordinary_scales_are_still_accepted(self, scale: str) -> None:
        """The rule must not have quietly become a whitelist.

        ``score_scale`` is still free-form: anything that can be a metrics key
        is still a legal scale, including names that merely *contain* a
        reserved one.
        """
        verifier = make_verifier(score_scale=scale)
        assert verifier.score_scale == scale

    def test_a_scale_cannot_be_smuggled_in_by_replacing_it_later(self) -> None:
        """``dataclasses.replace`` re-runs ``__post_init__``.

        This is the route the loop's own tests used to build a colliding
        problem, and it is the route a corpus builder would take.
        """
        verifier = make_verifier()
        with pytest.raises(ContractViolationError, match="progress"):
            dataclasses.replace(verifier, score_scale="progress")


# --------------------------------------------------------------------------- #
# score_floor — a problem's own declared floor (RES-15)
# --------------------------------------------------------------------------- #


class TestScoreFloorIsDeclaredOnTheVerifier:
    """A problem on a scale ``DEFAULT_SCORE_FLOORS`` doesn't know can still
    declare its own floor, alongside the scale, on the verifier.
    """

    def test_undeclared_floor_defaults_to_none(self) -> None:
        verifier = make_verifier()
        assert verifier.score_floor is None

    def test_a_declared_floor_is_kept(self) -> None:
        verifier = make_verifier(score_scale="val_loss", score_floor=1e9)
        assert verifier.score_floor == pytest.approx(1e9)

    def test_a_declared_floor_of_zero_is_kept_not_treated_as_falsy(self) -> None:
        verifier = make_verifier(score_scale="val_loss", score_floor=0.0)
        assert verifier.score_floor == 0.0

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_floor_is_refused(self, bad: float) -> None:
        with pytest.raises(ContractViolationError, match="not finite"):
            make_verifier(score_scale="val_loss", score_floor=bad)

    def test_a_non_numeric_floor_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="not a plain number"):
            make_verifier(score_scale="val_loss", score_floor="worst")  # type: ignore[arg-type]

    def test_a_bool_floor_is_refused(self) -> None:
        """``bool`` is a subclass of ``int`` in Python; refused explicitly."""
        with pytest.raises(ContractViolationError, match="not a plain number"):
            make_verifier(score_scale="val_loss", score_floor=True)  # type: ignore[arg-type]

    def test_a_scale_cannot_smuggle_a_bad_floor_in_by_replacing_it_later(self) -> None:
        verifier = make_verifier(score_scale="val_loss", score_floor=1.0)
        with pytest.raises(ContractViolationError, match="not finite"):
            dataclasses.replace(verifier, score_floor=float("nan"))


# --------------------------------------------------------------------------- #
# problem.id — a path component, validated as one (RES-16)
# --------------------------------------------------------------------------- #


class TestProblemIdIsAPathComponent:
    """``problem.id`` is written straight into the results tree.

    ``output_dir/attempts/<id>/`` holds the metrics trio and the plots;
    ``attempts/<id>.json`` and ``checkpoints/<id>.json`` are files named after
    it. Nothing validated it, so an id could escape the results root, collide
    with a directory the runner mints itself, or land somewhere the desktop's
    walk will not look — and every one of those failures surfaces far from the
    corpus definition that caused it, or (worse) not at all.
    """

    @pytest.mark.parametrize(
        "problem_id",
        [
            "speedup-01-claim-scorer",
            "cuda/matmul-speedup",
            "kaggle/tabular/titanic",
            "a",
            "prior-1-regression",  # only an exact ``prior-<digits>`` is the namespace
            "not-prior-1/leaf",  # ``prior-N`` anywhere but the final segment is fine
            "x" * 128,
        ],
    )
    def test_legal_ids_are_accepted_and_round_trip_unchanged(self, problem_id: str) -> None:
        problem = Problem(
            id=problem_id,
            problem_type=ProblemType.SPEEDUP,
            goal="go faster",
            workspace_template=Path("/tmp/templates/x"),
            verifier=make_verifier(problem_id),
            split=Split.PRACTICE,
        )
        assert problem.id == problem_id

    @pytest.mark.parametrize(
        ("problem_id", "expected"),
        [
            ("", "non-empty"),
            ("../escape", "'..' segment"),
            ("cuda/../../etc", "'..' segment"),
            ("/absolute/id", "absolute path"),
            ("trailing/", "empty path segment"),
            ("double//segment", "empty path segment"),
            ("cuda/ /matmul", "whitespace-only"),
            ("\\t/matmul", "backslash"),  # a literal backslash, not a tab
            ("C:/corpus", "colon"),
            ("\\\\server\\share", "backslash"),
            (".hidden", "'.'-prefixed segment"),
            ("cuda/.hidden/matmul", "'.'-prefixed segment"),
            ("./relative", "'.'-prefixed segment"),
            ("cuda/prior-1", "rotation namespace"),
            ("prior-12", "rotation namespace"),
            ("a/b/c/d/e", "path segments"),
            ("x" * 129, "characters"),
        ],
    )
    def test_each_unsafe_shape_is_refused_with_its_own_reason(
        self, problem_id: str, expected: str
    ) -> None:
        with pytest.raises(ContractViolationError) as caught:
            make_problem(problem_id)
        assert expected in str(caught.value)

    @pytest.mark.parametrize("bad", ["nul\x00byte", "bell\x07", "line\nbreak", "del\x7f"])
    def test_control_characters_are_refused(self, bad: str) -> None:
        with pytest.raises(ContractViolationError, match="control character"):
            make_problem(bad)

    def test_a_non_string_id_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="plain str"):
            Problem(
                id=7,  # type: ignore[arg-type]
                problem_type=ProblemType.SPEEDUP,
                goal="go faster",
                workspace_template=Path("/tmp/templates/x"),
                verifier=make_verifier(),
                split=Split.PRACTICE,
            )

    def test_the_refusal_names_the_consumer_that_motivates_the_rule(self) -> None:
        """An operator who trips one of these must learn *why*.

        Each of these rules exists because of one specific downstream reader,
        and a message that only said "invalid id" would send the operator
        looking for a naming convention that does not exist.
        """
        with pytest.raises(ContractViolationError, match=re.escape("fsroots.rs")):
            make_problem(".hidden")
        with pytest.raises(ContractViolationError, match="_viewer_runs"):
            make_problem("prior-3")
        with pytest.raises(ContractViolationError, match="MAX_DEPTH"):
            make_problem("a/b/c/d/e")
        with pytest.raises(ContractViolationError, match="results root"):
            make_problem("../etc")


# --------------------------------------------------------------------------- #
# Invariant — practice and held-out stay distinguishable
# --------------------------------------------------------------------------- #


class TestSplit:
    def test_problem_reports_its_split(self) -> None:
        practice = make_problem("p-a", split=Split.PRACTICE)
        held_out = make_problem("p-b", split=Split.HELD_OUT)
        assert practice.is_practice and not practice.is_held_out
        assert held_out.is_held_out and not held_out.is_practice

    def test_splits_are_distinct_values(self) -> None:
        assert Split.PRACTICE is not Split.HELD_OUT
        assert {s.value for s in Split} == {"practice", "held_out"}

    def test_self_edit_summary_never_sees_held_out(self) -> None:
        record = make_round()
        visible = record.self_edit_visible_scores()
        assert visible
        assert all(ts.split is Split.PRACTICE for ts in visible)
        assert record.score_for(ProblemType.SPEEDUP, Split.HELD_OUT) is not None
        assert all(ts.split is not Split.HELD_OUT for ts in visible)

    def test_held_out_is_still_scored_and_reported(self) -> None:
        """Withheld from learning, not from the report."""
        record = make_round()
        held = record.score_for(ProblemType.SPEEDUP, Split.HELD_OUT)
        assert held is not None
        assert held.mean_score == pytest.approx(9.5)
        assert (ProblemType.SPEEDUP, Split.HELD_OUT) in record.primary_scores()


# --------------------------------------------------------------------------- #
# Invariant — the operator decides, never advises
# --------------------------------------------------------------------------- #


class TestEscalation:
    def test_verdict_vocabulary_is_exactly_three(self) -> None:
        assert {v.value for v in EscalationVerdict} == {"continue", "abandon", "extend_cap"}

    def test_decision_has_no_free_text_field(self) -> None:
        """Free-form guidance would make the operator the improvement mechanism."""
        field_names = {f.name for f in dataclasses.fields(EscalationDecision)}
        assert field_names == {"request_id", "verdict", "decided_at_ms", "cap_extension"}
        annotations = {f.name: f.type for f in dataclasses.fields(EscalationDecision)}
        # request_id is an identifier, not a channel; nothing else is a string.
        assert annotations["verdict"] == "EscalationVerdict"
        assert annotations["cap_extension"] == "CapExtension | None"

    def test_advice_cannot_be_smuggled_in_at_runtime(self) -> None:
        """``slots=True`` closes the "just attach a field" route.

        The exception *type* varies by CPython version — 3.11's frozen
        ``__setattr__`` closure misfires on slotted dataclasses and surfaces
        ``TypeError`` where 3.12+ gives ``AttributeError`` — so the assertion
        is on the outcome that matters: the write does not land.
        """
        decision = EscalationDecision(
            request_id="e-1",
            verdict=EscalationVerdict.CONTINUE,
            decided_at_ms=1,
        )
        with pytest.raises((AttributeError, TypeError)):
            decision.advice = "try gradient boosting on that one"  # type: ignore[attr-defined]
        assert not hasattr(decision, "advice")

    def test_extend_cap_requires_a_numeric_extension(self) -> None:
        with pytest.raises(EscalationProtocolError, match="requires a cap_extension"):
            EscalationDecision(
                request_id="e-1",
                verdict=EscalationVerdict.EXTEND_CAP,
                decided_at_ms=1,
            )

    @pytest.mark.parametrize("verdict", [EscalationVerdict.CONTINUE, EscalationVerdict.ABANDON])
    def test_non_extend_verdicts_cannot_carry_budget(self, verdict: EscalationVerdict) -> None:
        with pytest.raises(EscalationProtocolError, match="must not carry"):
            EscalationDecision(
                request_id="e-1",
                verdict=verdict,
                decided_at_ms=1,
                cap_extension=CapExtension(extra_steps=10),
            )

    def test_continue_leaves_the_cap_alone(self) -> None:
        cap = make_cap()
        decision = EscalationDecision("e-1", EscalationVerdict.CONTINUE, 1)
        assert decision.apply_to(cap) == cap

    def test_extend_cap_grows_the_cap_and_records_the_extension(self) -> None:
        cap = make_cap()
        decision = EscalationDecision(
            "e-1",
            EscalationVerdict.EXTEND_CAP,
            1,
            cap_extension=CapExtension(extra_steps=50, extra_wall_clock_seconds=600.0),
        )
        extended = decision.apply_to(cap)
        assert extended.max_steps == 150
        assert extended.max_wall_clock_seconds == pytest.approx(4200.0)
        assert extended.max_tokens == cap.max_tokens
        assert extended.extension_count == 1

    def test_abandon_has_no_cap_answer(self) -> None:
        decision = EscalationDecision("e-1", EscalationVerdict.ABANDON, 1)
        with pytest.raises(EscalationProtocolError, match="ends the attempt"):
            decision.apply_to(make_cap())

    def test_request_carries_context_outward(self) -> None:
        """Rich request, three-valued reply — the asymmetry is the design."""
        request = EscalationRequest(
            request_id="e-1",
            problem_id="speedup-claim-scorer",
            attempt_id="a-1",
            round_id="round-00",
            reason=EscalationReason.CAP_EXHAUSTED,
            summary="Vectorised the embed loop; stuck at 3.1x against a 9.5x ceiling.",
            cap=make_cap(),
            consumed=CapConsumption(steps=100, tokens=400_000, wall_clock_seconds=3000.0),
            created_at_ms=1,
        )
        assert request.reason is EscalationReason.CAP_EXHAUSTED
        assert request.best_result is None
        with pytest.raises(FrozenInstanceError):
            request.summary = "edited"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #


class TestCap:
    def test_every_dimension_must_be_positive(self) -> None:
        for kwargs in (
            {"max_steps": 0},
            {"max_tokens": 0},
            {"max_wall_clock_seconds": 0.0},
            {"max_steps": -1},
        ):
            with pytest.raises(ContractViolationError, match="positive"):
                make_cap(**kwargs)

    def test_reports_which_dimension_tripped(self) -> None:
        cap = make_cap()
        assert cap.exceeded(CapConsumption(steps=100)) == (CapDimension.STEPS,)
        assert cap.exceeded(CapConsumption(tokens=500_000)) == (CapDimension.TOKENS,)
        assert cap.exceeded(CapConsumption(wall_clock_seconds=3600.0)) == (CapDimension.WALL_CLOCK,)
        assert cap.exceeded(CapConsumption(steps=1, tokens=1, wall_clock_seconds=1.0)) == ()

    def test_is_exhausted_matches_exceeded(self) -> None:
        cap = make_cap()
        assert not cap.is_exhausted(CapConsumption(steps=99))
        assert cap.is_exhausted(CapConsumption(steps=101))

    def test_remaining_is_clamped_at_zero(self) -> None:
        cap = make_cap()
        remaining = cap.remaining(CapConsumption(steps=140, tokens=1, wall_clock_seconds=0.0))
        assert remaining.steps == 0
        assert remaining.tokens == 499_999

    def test_extension_may_only_add(self) -> None:
        with pytest.raises(CapExtensionError, match="only add budget"):
            CapExtension(extra_steps=-1)

    def test_empty_extension_is_refused(self) -> None:
        with pytest.raises(CapExtensionError, match="indistinguishable from"):
            CapExtension()

    def test_extension_count_accumulates(self) -> None:
        cap = make_cap().extend(CapExtension(extra_steps=10)).extend(CapExtension(extra_tokens=1))
        assert cap.extension_count == 2
        assert cap.max_steps == 110

    def test_cap_is_frozen(self) -> None:
        cap = make_cap()
        with pytest.raises(FrozenInstanceError):
            cap.max_steps = 10_000  # type: ignore[misc]

    def test_consumption_adds(self) -> None:
        total = CapConsumption(steps=1, tokens=10, wall_clock_seconds=1.5) + CapConsumption(
            steps=2, tokens=20, wall_clock_seconds=0.5
        )
        assert total == CapConsumption(steps=3, tokens=30, wall_clock_seconds=2.0)

    def test_negative_consumption_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="negative"):
            CapConsumption(steps=-1)


# --------------------------------------------------------------------------- #
# Attempts — checkpoint and resume
# --------------------------------------------------------------------------- #


class TestAttempt:
    def test_evolve_produces_a_new_checkpoint(self) -> None:
        attempt = make_attempt()
        nxt = attempt.evolve(now_ms=1_000, state=AttemptState.RUNNING)
        assert nxt is not attempt
        assert nxt.checkpoint_seq == attempt.checkpoint_seq + 1
        assert nxt.updated_at_ms == 1_000
        assert attempt.state is AttemptState.PENDING

    def test_consumption_survives_a_pause_and_resume(self) -> None:
        """An interruption must cost the remainder of an attempt, not the attempt."""
        attempt = make_attempt().evolve(now_ms=1, state=AttemptState.RUNNING)
        attempt = attempt.record_consumption(
            CapConsumption(steps=12, tokens=90_000, wall_clock_seconds=400.0), now_ms=2
        )
        paused = attempt.pause(now_ms=3, resume_token="conv-abc")
        assert paused.state is AttemptState.PAUSED
        assert paused.resume_token == "conv-abc"

        resumed = paused.resume(now_ms=4)
        assert resumed.state is AttemptState.RUNNING
        assert resumed.consumed == CapConsumption(steps=12, tokens=90_000, wall_clock_seconds=400.0)
        assert resumed.resume_token == "conv-abc"
        assert resumed.remaining.steps == 88

    def test_pause_keeps_an_existing_resume_token_when_none_supplied(self) -> None:
        attempt = make_attempt(resume_token="conv-abc").pause(now_ms=1)
        assert attempt.resume_token == "conv-abc"

    def test_pause_then_resume_from_escalated_cannot_abandon_without_a_fresh_id(self) -> None:
        """The CONTINUE hole: returning to RUNNING by pause/resume kept the id."""
        escalated = make_attempt(state=AttemptState.ESCALATED, escalation_id="esc-a-1")
        resumed = escalated.pause(now_ms=1).resume(now_ms=2)
        assert resumed.state is AttemptState.RUNNING
        assert resumed.escalation_id is None
        with pytest.raises(ContractViolationError, match="ABANDONED requires an escalation_id"):
            resumed.evolve(now_ms=3, state=AttemptState.ABANDONED)

    def test_evolve_to_running_clears_a_leftover_escalation_id(self) -> None:
        escalated = make_attempt(state=AttemptState.ESCALATED, escalation_id="esc-a-1")
        running = escalated.evolve(now_ms=1, state=AttemptState.RUNNING)
        assert running.escalation_id is None
        with pytest.raises(ContractViolationError, match="ABANDONED requires an escalation_id"):
            running.evolve(now_ms=2, state=AttemptState.ABANDONED)

    def test_constructor_clears_a_leftover_escalation_id_on_running(self) -> None:
        attempt = make_attempt(state=AttemptState.RUNNING, escalation_id="esc-a-1")
        assert attempt.escalation_id is None
        with pytest.raises(ContractViolationError, match="ABANDONED requires an escalation_id"):
            attempt.evolve(now_ms=1, state=AttemptState.ABANDONED)

    def test_terminal_attempts_cannot_pause_or_resume(self) -> None:
        for state in TERMINAL_ATTEMPT_STATES:
            kwargs: dict[str, Any] = {"state": state}
            if state is AttemptState.ABANDONED:
                kwargs["escalation_id"] = "esc-terminal"
            attempt = make_attempt(**kwargs)
            assert attempt.is_terminal
            assert not attempt.is_resumable
            with pytest.raises(ContractViolationError, match="cannot pause"):
                attempt.pause(now_ms=1)
            with pytest.raises(ContractViolationError, match="cannot resume"):
                attempt.resume(now_ms=1)

    def test_escalated_is_not_terminal_but_is_not_resumable(self) -> None:
        """The operator's decision, not the solver, moves an escalation onward."""
        attempt = make_attempt(state=AttemptState.ESCALATED)
        assert not attempt.is_terminal
        assert not attempt.is_resumable

    def test_verifying_is_resumable(self) -> None:
        """A crash during verify must not lose the rest of the attempt."""
        attempt = make_attempt(state=AttemptState.VERIFYING)
        assert not attempt.is_terminal
        assert attempt.is_resumable
        assert attempt.resume(now_ms=1).state is AttemptState.RUNNING

    def test_no_state_means_the_agent_gave_up_on_its_own(self) -> None:
        """The enum has no self-quit synonym. It does not check a verdict."""
        assert AttemptState.ABANDONED in TERMINAL_ATTEMPT_STATES
        assert not any(
            s.value in {"quit", "gave_up", "hopeless", "self_abandoned"} for s in AttemptState
        )

    def test_cap_exhaustion_is_reported_per_dimension(self) -> None:
        attempt = make_attempt().record_consumption(
            CapConsumption(steps=100, tokens=500_000), now_ms=1
        )
        assert attempt.cap_exhausted
        assert attempt.exceeded_cap_dimensions == (CapDimension.STEPS, CapDimension.TOKENS)

    def test_cap_extension_flows_through_the_attempt(self) -> None:
        attempt = make_attempt()
        extended = attempt.apply_cap_extension(CapExtension(extra_steps=25), now_ms=9)
        assert extended.cap.max_steps == 125
        assert extended.cap.extension_count == 1
        assert attempt.cap.max_steps == 100

    def test_attempt_is_frozen(self) -> None:
        attempt = make_attempt()
        with pytest.raises(FrozenInstanceError):
            attempt.state = AttemptState.PASSED  # type: ignore[misc]

    def test_failed_within_cap_is_a_first_class_outcome(self) -> None:
        assert AttemptState.FAILED_WITHIN_CAP in TERMINAL_ATTEMPT_STATES

    def test_evolve_refuses_a_wholesale_cap_swap(self) -> None:
        """Cap growth is apply_cap_extension; evolve cannot reset extension_count."""
        attempt = make_attempt()
        with pytest.raises(ContractViolationError, match="apply_cap_extension"):
            attempt.evolve(
                now_ms=1,
                cap=Cap(max_steps=1, max_tokens=10_000, max_wall_clock_seconds=1.0),
            )

    def test_evolve_refuses_zeroing_consumption(self) -> None:
        """Consumption is additive; evolve cannot write the meter back to empty."""
        attempt = make_attempt().record_consumption(
            CapConsumption(steps=12, tokens=90_000, wall_clock_seconds=400.0), now_ms=1
        )
        with pytest.raises(ContractViolationError, match="record_consumption"):
            attempt.evolve(now_ms=2, consumed=CapConsumption())

    def test_evolve_refuses_abandoned_without_an_escalation_id(self) -> None:
        """ABANDONED is not a bare field write; it needs a non-empty escalation_id."""
        attempt = make_attempt()
        with pytest.raises(ContractViolationError, match="ABANDONED requires an escalation_id"):
            attempt.evolve(now_ms=1, state=AttemptState.ABANDONED)

    def test_evolve_allows_abandoned_when_an_escalation_id_is_already_bound(self) -> None:
        """A leftover id is enough; this is not an operator-verdict check."""
        attempt = make_attempt().evolve(
            now_ms=1, state=AttemptState.ESCALATED, escalation_id="esc-a-1"
        )
        abandoned = attempt.evolve(now_ms=2, state=AttemptState.ABANDONED)
        assert abandoned.state is AttemptState.ABANDONED
        assert abandoned.escalation_id == "esc-a-1"

    def test_constructor_refuses_abandoned_without_an_escalation_id(self) -> None:
        """The evolve guard is not enough; construction is the same rule."""
        with pytest.raises(ContractViolationError, match="ABANDONED requires an escalation_id"):
            make_attempt(state=AttemptState.ABANDONED)

    def test_constructor_refuses_abandoned_with_an_empty_escalation_id(self) -> None:
        with pytest.raises(ContractViolationError, match="ABANDONED requires an escalation_id"):
            make_attempt(state=AttemptState.ABANDONED, escalation_id="")

    def test_constructor_allows_abandoned_when_an_escalation_id_is_bound(self) -> None:
        attempt = make_attempt(state=AttemptState.ABANDONED, escalation_id="esc-a-1")
        assert attempt.state is AttemptState.ABANDONED
        assert attempt.escalation_id == "esc-a-1"

    def test_record_consumption_still_adds(self) -> None:
        attempt = make_attempt().record_consumption(CapConsumption(steps=1, tokens=10), now_ms=1)
        assert attempt.consumed == CapConsumption(steps=1, tokens=10)

    def test_apply_cap_extension_still_grows(self) -> None:
        attempt = make_attempt().apply_cap_extension(CapExtension(extra_steps=5), now_ms=1)
        assert attempt.cap.max_steps == 105
        assert attempt.cap.extension_count == 1


# --------------------------------------------------------------------------- #
# Round records — the four driving-function numbers plus lineage
# --------------------------------------------------------------------------- #


class TestTypeScore:
    def test_mean_is_within_a_cell(self) -> None:
        cell = TypeScore(
            problem_type=ProblemType.SPEEDUP,
            split=Split.PRACTICE,
            scores={"a": 2.0, "b": 4.0},
            correctness_passes=1,
        )
        assert cell.n == 2
        assert cell.mean_score == pytest.approx(3.0)
        assert cell.correctness_pass_rate == pytest.approx(0.5)

    def test_empty_cell_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="measures nothing"):
            TypeScore(ProblemType.KAGGLE, Split.PRACTICE, {}, 0)

    def test_impossible_pass_count_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="out of range"):
            TypeScore(ProblemType.KAGGLE, Split.PRACTICE, {"a": 1.0}, 2)

    def test_scores_are_read_only(self) -> None:
        cell = TypeScore(ProblemType.KAGGLE, Split.PRACTICE, {"a": 1.0}, 1)
        with pytest.raises(TypeError):
            cell.scores["a"] = 9.0  # type: ignore[index]


class TestRoundRecord:
    def test_no_aggregate_score_across_types(self) -> None:
        """Averaging a speedup ratio against a leaderboard percentile is meaningless."""
        names = {f.name for f in dataclasses.fields(RoundRecord)}
        forbidden = {"primary", "primary_score", "score", "mean_score", "overall", "solve_rate"}
        assert not (names & forbidden)
        assert not any(hasattr(RoundRecord, n) for n in forbidden)

    def test_primary_scores_are_reported_per_cell(self) -> None:
        record = make_round(
            type_scores=(
                TypeScore(ProblemType.SPEEDUP, Split.PRACTICE, {"a": 2.0}, 1),
                TypeScore(ProblemType.KAGGLE, Split.PRACTICE, {"k": 0.61}, 1),
            )
        )
        cells = record.primary_scores()
        assert cells[(ProblemType.SPEEDUP, Split.PRACTICE)] == pytest.approx(2.0)
        assert cells[(ProblemType.KAGGLE, Split.PRACTICE)] == pytest.approx(0.61)
        assert len(cells) == 2

    def test_round_zero_has_no_parent(self) -> None:
        with pytest.raises(ContractViolationError, match="baseline"):
            make_round(round_index=0, parent_round_id="round--1")

    def test_later_rounds_must_record_a_parent(self) -> None:
        with pytest.raises(ContractViolationError, match="lineage"):
            make_round(round_index=2, parent_round_id=None)

    def test_eval_set_hash_is_required(self) -> None:
        with pytest.raises(ContractViolationError, match="eval_set_hash"):
            make_round(eval_set_hash="")

    def test_rounds_on_different_eval_sets_cannot_be_compared(self) -> None:
        a = make_round(eval_set_hash="corpus-v1")
        b = make_round(round_index=1, parent_round_id=a.run_id, eval_set_hash="corpus-v2")
        with pytest.raises(EvalSetMismatchError, match="trajectory restarts"):
            b.assert_comparable_to(a)

    def test_rounds_on_the_same_eval_set_compare_fine(self) -> None:
        a = make_round()
        b = make_round(round_index=1, parent_round_id=a.run_id)
        b.assert_comparable_to(a)
        assert not b.differs_in_engine_from(a)

    def test_duplicate_cells_are_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="duplicate cell"):
            make_round(
                type_scores=(
                    TypeScore(ProblemType.SPEEDUP, Split.PRACTICE, {"a": 1.0}, 1),
                    TypeScore(ProblemType.SPEEDUP, Split.PRACTICE, {"b": 2.0}, 1),
                )
            )

    def test_escalation_count_is_the_human_gate_load(self) -> None:
        record = make_round(escalation_count=3)
        assert record.escalation_count == 3
        with pytest.raises(ContractViolationError, match="negative"):
            make_round(escalation_count=-1)

    def test_cost_carries_no_dollar_field(self) -> None:
        """Dollar metering was retired; the cap is the runaway brake now."""
        names = {f.name for f in dataclasses.fields(RoundCost)}
        assert names == {"wall_clock_seconds", "tokens", "attempts"}

    def test_engine_identity_is_recorded_for_lineage(self) -> None:
        record = make_round()
        assert record.engine.backend == "claude"
        assert record.engine.scaffold_git_sha
        other = dataclasses.replace(record.engine, substep_model=None)
        assert record.engine != other

    def test_gates_are_read_only(self) -> None:
        record = dataclasses.replace(make_round(), gates={"harness_integrity": True})
        assert record.gates["harness_integrity"] is True
        with pytest.raises(TypeError):
            record.gates["harness_integrity"] = False  # type: ignore[index]

    def test_round_cells_lists_cells_in_first_seen_order(self) -> None:
        a = make_round()
        b = make_round(round_index=1, parent_round_id=a.run_id)
        assert round_cells([a, b]) == (
            (ProblemType.SPEEDUP, Split.PRACTICE),
            (ProblemType.SPEEDUP, Split.HELD_OUT),
        )


class TestRoundDelta:
    def test_saturation_is_gain_below_the_noise_floor(self) -> None:
        below = RoundDelta(
            ProblemType.SPEEDUP, Split.PRACTICE, marginal_gain=0.004, noise_floor=0.011
        )
        above = RoundDelta(
            ProblemType.SPEEDUP, Split.PRACTICE, marginal_gain=0.05, noise_floor=0.011
        )
        assert not below.beats_noise_floor
        assert above.beats_noise_floor
        assert above.gain_in_noise_units == pytest.approx(0.05 / 0.011)

    def test_undefined_noise_units_when_floor_is_zero(self) -> None:
        delta = RoundDelta(ProblemType.KAGGLE, Split.PRACTICE, marginal_gain=0.1, noise_floor=0.0)
        assert delta.gain_in_noise_units is None

    def test_cost_per_unit_gain_is_optional(self) -> None:
        """None rather than a sentinel: a made-up number reads as a measurement."""
        delta = RoundDelta(
            ProblemType.KAGGLE, Split.PRACTICE, marginal_gain=-0.02, noise_floor=0.01
        )
        assert delta.cost_per_unit_gain is None

    def test_negative_noise_floor_is_refused(self) -> None:
        with pytest.raises(ContractViolationError, match="noise floor"):
            RoundDelta(ProblemType.KAGGLE, Split.PRACTICE, marginal_gain=0.0, noise_floor=-1.0)


# --------------------------------------------------------------------------- #
# Package surface
# --------------------------------------------------------------------------- #


def test_contracts_are_re_exported_from_the_package() -> None:
    import turing.research as research
    from turing.research import contracts

    for name in contracts.__all__:
        assert hasattr(research, name), f"{name} missing from turing.research"
    assert set(research.__all__) == set(contracts.__all__)


def test_sibling_subpackages_import_cleanly() -> None:
    """The parallel modules must be importable before they have content."""
    import importlib

    for name in ("problems", "solver", "backends", "loop"):
        importlib.import_module(f"turing.research.{name}")
