"""FakeBackend + real Solver + synthetic Problem → RoundRecord with lineage.

The existing loop integration test drives a fake solver that implements
``step`` directly. This one is the missing e2e: a scripted
:class:`~turing.research.backends.fake.FakeBackend` talks to the real
:class:`~turing.research.solver.Solver` through
:class:`~turing.research.backends.adapter.ProposalAdapter`, the runner
drives that solver through
:class:`~turing.research.loop.solver_bridge.SolverBridge`, and the
instrument writes a :class:`~turing.research.contracts.RoundRecord` whose
lineage fields are populated.

No network, no ntfy, no sandbox user. One synthetic problem, two rounds so
``parent_round_id`` is real rather than just round-0-has-none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from turing.research.backends import (
    FakeBackend,
    ProposalAdapter,
    ScriptedTurn,
    Usage,
    encode_proposal,
)
from turing.research.backends.errors import BackendError
from turing.research.contracts import (
    SCORE_SCALE_SPEEDUP,
    Attempt,
    AttemptState,
    Cap,
    EscalationReason,
    EscalationVerdict,
    Problem,
    ProblemType,
    Split,
    VerificationResult,
    Verifier,
)
from turing.research.loop.protocols import SolverTask
from turing.research.loop.runner import AttemptOutcome, PassCriterion, RoundConfig, RoundRunner
from turing.research.loop.solver_bridge import SolverBridge
from turing.research.loop.trajectory import TrajectoryStore
from turing.research.loop.workspace import CopyTreeWorkspaceProvider
from turing.research.problems.adapter import fingerprint_corpus
from turing.research.solver import (
    InMemoryCheckpointStore,
    Solver,
    WorkspaceManager,
)

from .conftest import FakeClock, ScriptedEscalationChannel

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.solver.protocols import ProposalBackend

SOLUTION_FILE = "solution.txt"


@dataclass(frozen=True)
class NumberVerifier(Verifier):
    """Reads a number the solver wrote. Frozen, like every verifier."""

    filename: str = SOLUTION_FILE

    async def verify(self, workspace: Path) -> VerificationResult:
        raw = ""
        try:
            raw = (workspace / self.filename).read_text(encoding="utf-8").strip()
            score = float(raw)
            passed = True
        except (OSError, ValueError):
            score, passed = 0.0, False
        return VerificationResult(
            problem_id=self.problem_id,
            verifier_id=self.verifier_id,
            score=score,
            passed_correctness=passed,
            score_scale=self.score_scale,
            raw_measurements={"parsed": score},
            detail=f"read {raw!r}",
        )


def _problem(template: Path) -> Problem:
    return Problem(
        id="synth-speed",
        problem_type=ProblemType.SPEEDUP,
        goal="raise the number in solution.txt",
        workspace_template=template,
        verifier=NumberVerifier(
            verifier_id="v-synth-speed",
            problem_id="synth-speed",
            description="score is the number in solution.txt",
            score_scale=SCORE_SCALE_SPEEDUP,
        ),
        split=Split.PRACTICE,
    )


def _solver(backend: ProposalBackend, workspace_root: Path) -> Solver:
    return Solver(
        backend=backend,
        store=InMemoryCheckpointStore(),
        workspaces=WorkspaceManager(workspace_root),
        escalations=_UnusedSolverChannel(),
    )


class _UnusedSolverChannel:
    """The runner owns escalation; the standalone solver channel is unused here."""

    async def raise_escalation(self, request: object) -> None:
        raise AssertionError("solver-side escalation must not fire on the runner path")

    async def await_decision(self, request_id: str) -> None:
        raise AssertionError("solver-side escalation must not fire on the runner path")


def _config(*, engine: object, **overrides: object) -> RoundConfig:
    values: dict[str, object] = {
        "round_index": 0,
        "run_id": "r00",
        "parent_round_id": None,
        "eval_set_hash": "",
        "engine": engine,
        "seed": 7,
        "default_cap": Cap(max_steps=3, max_tokens=100_000, max_wall_clock_seconds=600.0),
        "pass_criteria": {"synth-speed": PassCriterion(min_score=3.0)},
    }
    values.update(overrides)
    return RoundConfig(**values)  # type: ignore[arg-type]


async def test_fake_backend_real_solver_synthetic_problem_writes_lineage(
    tmp_path: Path,
) -> None:
    template = tmp_path / "template"
    template.mkdir()
    (template / SOLUTION_FILE).write_text("0\n", encoding="utf-8")
    problem = _problem(template)
    corpus = [problem]

    fake = FakeBackend.replying(
        encode_proposal(rationale="write 2.0", edits=((SOLUTION_FILE, "2.0"),)),
        encode_proposal(rationale="write 3.0", edits=((SOLUTION_FILE, "3.0"),)),
        encode_proposal(rationale="write 4.0", edits=((SOLUTION_FILE, "4.0"),)),
    )
    adapter = ProposalAdapter(fake)
    workspace_root = tmp_path / "workspaces"
    solver = _solver(adapter, workspace_root)
    clock = FakeClock()
    store = TrajectoryStore(tmp_path / "results", "e2e-seams", clock=clock)
    runner = RoundRunner(
        solver=SolverBridge(solver),
        workspaces=CopyTreeWorkspaceProvider(workspace_root),
        trajectory=store,
        escalations=ScriptedEscalationChannel(),
        clock=clock,
    )
    engine = fake.identity.to_engine_identity("scaffold-deadbeef")

    base = await runner.run_round(corpus, _config(engine=engine))

    assert base.record.round_index == 0
    assert base.record.parent_round_id is None
    assert base.record.eval_set_hash == fingerprint_corpus(corpus)
    assert base.record.engine.backend == "fake"
    assert base.record.engine.orchestrator_model == "fake-orchestrator"
    assert base.record.engine.scaffold_git_sha == "scaffold-deadbeef"
    cell = base.record.score_for(ProblemType.SPEEDUP, Split.PRACTICE)
    assert cell is not None
    assert cell.scores["synth-speed"] == 3.0
    assert cell.correctness_passes == 1
    assert base.attempts[0].attempt.state is AttemptState.PASSED
    assert len(fake.calls) == 2
    assert "Verifier" not in fake.calls[0].system

    second = await runner.run_round(
        corpus,
        _config(
            engine=engine,
            round_index=1,
            run_id="r01",
            parent_round_id="r00",
            pass_criteria={"synth-speed": PassCriterion(min_score=4.0)},
        ),
        parent=base.record,
    )

    assert second.record.round_index == 1
    assert second.record.parent_round_id == "r00"
    assert second.record.eval_set_hash == base.record.eval_set_hash
    base.record.assert_comparable_to(second.record)
    assert second.record.differs_in_engine_from(base.record) is False
    r1 = second.record.score_for(ProblemType.SPEEDUP, Split.PRACTICE)
    assert r1 is not None
    assert r1.scores["synth-speed"] == 4.0
    assert second.attempts[0].attempt.state is AttemptState.PASSED
    assert len(fake.calls) == 3

    document = json.loads(store.trajectory_path.read_text(encoding="utf-8"))
    assert [row["round"] for row in document["rounds"]] == [0, 1]
    assert document["rounds"][1]["parent_round"] == "r00"
    assert document["rounds"][1]["eval_set_hash"] == fingerprint_corpus(corpus)
    assert document["rounds"][1]["engine"]["backend"] == "fake"
    assert document["rounds"][1]["engine"]["scaffold_git_sha"] == "scaffold-deadbeef"
    assert "primary" in document["rounds"][1]
    assert isinstance(document["rounds"][1]["primary"], dict)
    assert (store.loop_dir / "round-00" / "round.json").exists()
    assert (store.loop_dir / "round-01" / "round.json").exists()


async def test_two_bridge_steps_on_one_live_attempt(tmp_path: Path) -> None:
    """The second successful step must not die restoring a spent ledger."""
    template = tmp_path / "template"
    template.mkdir()
    (template / SOLUTION_FILE).write_text("0\n", encoding="utf-8")
    problem = _problem(template)
    fake = FakeBackend.replying(
        encode_proposal(rationale="write 1", edits=((SOLUTION_FILE, "1.0"),)),
        encode_proposal(rationale="write 2", edits=((SOLUTION_FILE, "2.0"),)),
    )
    workspace_root = tmp_path / "workspaces"
    solver = _solver(ProposalAdapter(fake), workspace_root)
    workspace = workspace_root / "a1"
    workspace.mkdir(parents=True)
    (workspace / SOLUTION_FILE).write_text("0\n", encoding="utf-8")
    attempt = Attempt(
        attempt_id="a1",
        problem_id=problem.id,
        round_id="r00",
        seed=7,
        workspace_path=workspace,
        cap=Cap(max_steps=5, max_tokens=100_000, max_wall_clock_seconds=600.0),
        state=AttemptState.RUNNING,
        started_at_ms=1,
        updated_at_ms=1,
    )
    bridge = SolverBridge(solver)
    task = SolverTask.from_problem(problem)

    first = await bridge.step(task, attempt)
    assert first.tokens > 0
    assert first.resume_token is not None
    attempt = attempt.evolve(now_ms=2, resume_token=first.resume_token, step_index=1)
    second = await bridge.step(task, attempt)
    assert second.tokens > 0
    assert (workspace / SOLUTION_FILE).read_text(encoding="utf-8") == "2.0"
    assert fake.ledger.steps == 2


async def test_malformed_json_spend_survives_operator_continue(tmp_path: Path) -> None:
    """A successful generate then a parse error still charges tokens and a step."""
    template = tmp_path / "template"
    template.mkdir()
    (template / SOLUTION_FILE).write_text("0\n", encoding="utf-8")
    problem = _problem(template)
    failed_usage = Usage(input_tokens=10, output_tokens=5)
    ok_usage = Usage(input_tokens=8, output_tokens=4)
    fake = FakeBackend(
        [
            ScriptedTurn(text="not a proposal", usage=failed_usage),
            ScriptedTurn(
                text=encode_proposal(
                    rationale="write 2.0",
                    edits=((SOLUTION_FILE, "2.0"),),
                ),
                usage=ok_usage,
            ),
        ]
    )
    workspace_root = tmp_path / "workspaces"
    solver = _solver(ProposalAdapter(fake), workspace_root)
    clock = FakeClock()
    store = TrajectoryStore(tmp_path / "results", "e2e-spend", clock=clock)
    channel = ScriptedEscalationChannel([EscalationVerdict.CONTINUE])
    runner = RoundRunner(
        solver=SolverBridge(solver),
        workspaces=CopyTreeWorkspaceProvider(workspace_root),
        trajectory=store,
        escalations=channel,
        clock=clock,
    )
    engine = fake.identity.to_engine_identity("scaffold-deadbeef")
    outcome = await runner.run_attempt(
        problem,
        _config(
            engine=engine,
            pass_criteria={"synth-speed": PassCriterion(min_score=2.0)},
            default_cap=Cap(max_steps=5, max_tokens=100_000, max_wall_clock_seconds=600.0),
        ),
        output_dir=store.round_dir(0),
    )

    assert channel.requests
    assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
    assert channel.requests[0].consumed.tokens == 15
    assert channel.requests[0].consumed.steps == 1
    assert outcome.attempt.consumed.tokens == 27
    assert outcome.attempt.consumed.steps == 2
    assert outcome.attempt.state is AttemptState.PASSED


async def test_generate_failure_spend_survives_operator_continue(tmp_path: Path) -> None:
    """A generate() that fails after charging the ledger still costs tokens and a step."""
    template = tmp_path / "template"
    template.mkdir()
    (template / SOLUTION_FILE).write_text("0\n", encoding="utf-8")
    problem = _problem(template)
    ok_usage = Usage(input_tokens=8, output_tokens=4)
    fake = FakeBackend(
        [
            ScriptedTurn(error=BackendError("provider down")),
            ScriptedTurn(
                text=encode_proposal(
                    rationale="write 2.0",
                    edits=((SOLUTION_FILE, "2.0"),),
                ),
                usage=ok_usage,
            ),
        ]
    )
    workspace_root = tmp_path / "workspaces"
    solver = _solver(ProposalAdapter(fake), workspace_root)
    clock = FakeClock()
    store = TrajectoryStore(tmp_path / "results", "e2e-generate-fail", clock=clock)
    channel = ScriptedEscalationChannel([EscalationVerdict.CONTINUE])
    runner = RoundRunner(
        solver=SolverBridge(solver),
        workspaces=CopyTreeWorkspaceProvider(workspace_root),
        trajectory=store,
        escalations=channel,
        clock=clock,
    )
    engine = fake.identity.to_engine_identity("scaffold-deadbeef")
    outcome = await runner.run_attempt(
        problem,
        _config(
            engine=engine,
            pass_criteria={"synth-speed": PassCriterion(min_score=2.0)},
            default_cap=Cap(max_steps=5, max_tokens=100_000, max_wall_clock_seconds=600.0),
        ),
        output_dir=store.round_dir(0),
    )

    assert channel.requests
    assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
    failed_tokens = channel.requests[0].consumed.tokens
    assert failed_tokens > 0
    assert channel.requests[0].consumed.steps == 1
    assert outcome.attempt.consumed.tokens == failed_tokens + ok_usage.total_tokens
    assert outcome.attempt.consumed.steps == 2
    assert fake.ledger.tokens == outcome.attempt.consumed.tokens
    assert fake.ledger.steps == 2
    assert outcome.attempt.state is AttemptState.PASSED


class _ForeignSpendError(RuntimeError):
    """An SDK-shaped crash that already carries a misleading spend figure."""

    def __init__(self, tokens: int = 1) -> None:
        super().__init__("sdk blew up")
        self.usage = Usage(input_tokens=tokens)
        self.tokens = tokens


async def _continue_after_generate_error(
    tmp_path: Path,
    *,
    error: BaseException,
    run_id: str,
) -> tuple[AttemptOutcome, FakeBackend, ScriptedEscalationChannel, Usage]:
    template = tmp_path / "template"
    template.mkdir()
    (template / SOLUTION_FILE).write_text("0\n", encoding="utf-8")
    problem = _problem(template)
    ok_usage = Usage(input_tokens=8, output_tokens=4)
    fake = FakeBackend(
        [
            ScriptedTurn(error=error),
            ScriptedTurn(
                text=encode_proposal(
                    rationale="write 2.0",
                    edits=((SOLUTION_FILE, "2.0"),),
                ),
                usage=ok_usage,
            ),
        ]
    )
    workspace_root = tmp_path / "workspaces"
    solver = _solver(ProposalAdapter(fake), workspace_root)
    clock = FakeClock()
    store = TrajectoryStore(tmp_path / "results", run_id, clock=clock)
    channel = ScriptedEscalationChannel([EscalationVerdict.CONTINUE])
    runner = RoundRunner(
        solver=SolverBridge(solver),
        workspaces=CopyTreeWorkspaceProvider(workspace_root),
        trajectory=store,
        escalations=channel,
        clock=clock,
    )
    engine = fake.identity.to_engine_identity("scaffold-deadbeef")
    outcome = await runner.run_attempt(
        problem,
        _config(
            engine=engine,
            pass_criteria={"synth-speed": PassCriterion(min_score=2.0)},
            default_cap=Cap(max_steps=5, max_tokens=100_000, max_wall_clock_seconds=600.0),
        ),
        output_dir=store.round_dir(0),
    )
    return outcome, fake, channel, ok_usage


async def test_foreign_spend_on_generate_failure_does_not_override_ledger(
    tmp_path: Path,
) -> None:
    """The attempt must charge the ledger figure, not a foreign usage/tokens=1."""
    outcome, fake, channel, ok_usage = await _continue_after_generate_error(
        tmp_path,
        error=_ForeignSpendError(1),
        run_id="e2e-foreign-spend",
    )

    assert channel.requests
    assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
    failed_tokens = channel.requests[0].consumed.tokens
    assert failed_tokens > 1
    assert failed_tokens == fake.ledger.tokens - ok_usage.total_tokens
    assert channel.requests[0].consumed.steps == 1
    assert outcome.attempt.consumed.tokens == failed_tokens + ok_usage.total_tokens
    assert outcome.attempt.consumed.steps == 2
    assert fake.ledger.tokens == outcome.attempt.consumed.tokens
    assert fake.ledger.steps == 2
    assert outcome.attempt.state is AttemptState.PASSED


class _ReadOnlyUsageError(RuntimeError):
    """An SDK-shaped crash whose ``usage`` cannot be overwritten."""

    @property
    def usage(self) -> Usage:
        return Usage(input_tokens=0, output_tokens=1)


async def test_read_only_usage_on_generate_failure_does_not_override_ledger(
    tmp_path: Path,
) -> None:
    """attempt.consumed.tokens is the ledger charge, not the frozen usage of 1.

    CONTINUE must not drop it: the second step adds the successful call on
    top of that charge, not on top of 1.
    """
    outcome, fake, channel, ok_usage = await _continue_after_generate_error(
        tmp_path,
        error=_ReadOnlyUsageError("sdk blew up"),
        run_id="e2e-readonly-usage",
    )

    assert channel.requests
    assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
    failed_tokens = channel.requests[0].consumed.tokens
    assert failed_tokens > 1
    assert failed_tokens == fake.ledger.tokens - ok_usage.total_tokens
    assert channel.requests[0].consumed.steps == 1
    assert outcome.attempt.consumed.tokens == failed_tokens + ok_usage.total_tokens
    assert outcome.attempt.consumed.steps == 2
    assert fake.ledger.tokens == outcome.attempt.consumed.tokens
    assert fake.ledger.steps == 2
    assert outcome.attempt.state is AttemptState.PASSED


async def test_explicit_zero_usage_falls_back_on_generate_failure(tmp_path: Path) -> None:
    """BackendError(usage=Usage()) must charge the estimate, not zero tokens."""
    outcome, fake, channel, ok_usage = await _continue_after_generate_error(
        tmp_path,
        error=BackendError("provider down", usage=Usage()),
        run_id="e2e-zero-usage",
    )

    assert channel.requests
    assert channel.requests[0].reason is EscalationReason.HARNESS_FAILURE
    failed_tokens = channel.requests[0].consumed.tokens
    assert failed_tokens > 0
    assert channel.requests[0].consumed.steps == 1
    assert outcome.attempt.consumed.tokens == failed_tokens + ok_usage.total_tokens
    assert outcome.attempt.consumed.steps == 2
    assert fake.ledger.tokens == outcome.attempt.consumed.tokens
    assert fake.ledger.steps == 2
    assert outcome.attempt.state is AttemptState.PASSED
