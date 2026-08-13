"""Checkpoint codec and stores.

The store is the only thing standing between a closed subscription window and
a lost attempt, so the properties tested here are the ones resume relies on:
values survive a round trip intact, a journal entry and the budget it charged
land together or not at all, and a stale process cannot rewind a good
checkpoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from turing.research.contracts import (
    Attempt,
    AttemptState,
    Cap,
    CapConsumption,
    ContractViolationError,
    EscalationReason,
    EscalationRequest,
    VerificationResult,
)
from turing.research.solver import (
    CheckpointError,
    FileEdit,
    InMemoryCheckpointStore,
    IterationPhase,
    IterationRecord,
    Proposal,
    SqliteCheckpointStore,
    attempt_from_json,
    attempt_to_json,
    escalation_from_json,
    escalation_to_json,
    iteration_from_json,
    iteration_to_json,
)
from turing.research.solver.models import CommandOutcome


def _result() -> VerificationResult:
    return VerificationResult(
        problem_id="speedup-1",
        verifier_id="v-speedup-1",
        score=2.5,
        passed_correctness=True,
        score_scale="speedup_ratio",
        raw_measurements={"baseline_s": 24.5, "candidate_s": 9.8},
        detail="two runs",
    )


def _attempt(**changes: object) -> Attempt:
    base = Attempt(
        attempt_id="a1",
        problem_id="speedup-1",
        round_id="r0",
        seed=7,
        workspace_path=Path("/tmp/turing-workspace/speedup-1/a1"),
        cap=Cap(max_steps=10, max_tokens=1000, max_wall_clock_seconds=60.0),
        consumed=CapConsumption(steps=2, tokens=300, wall_clock_seconds=1.5),
        state=AttemptState.RUNNING,
        step_index=2,
        checkpoint_seq=5,
        resume_token="conv-abc",
        started_at_ms=1,
        updated_at_ms=2,
        result=_result(),
        escalation_id=None,
    )
    return base if not changes else base.evolve(now_ms=3, **changes)  # type: ignore[arg-type]


def _record(phase: IterationPhase = IterationPhase.VERIFIED) -> IterationRecord:
    return IterationRecord(
        attempt_id="a1",
        iteration_index=3,
        phase=phase,
        proposal=Proposal(
            proposal_id="p3",
            rationale="vectorise the inner loop",
            edits=(FileEdit(relative_path="src/fast.py", content="x = 1\n"),),
            commands=("pytest -q",),
            tokens=1234,
        ),
        started_at_ms=10,
        updated_at_ms=20,
        applied_paths=("src/fast.py",),
        command_outcomes=(
            CommandOutcome(
                command="pytest -q", exit_code=0, stdout_tail="52 passed", duration_seconds=0.07
            ),
        ),
        result=_result() if phase is IterationPhase.VERIFIED else None,
        wall_clock_seconds=3.25,
    )


class TestCodec:
    def test_attempt_round_trips_every_field(self) -> None:
        original = _attempt()
        assert attempt_from_json(attempt_to_json(original)) == original

    def test_abandoned_without_an_escalation_id_cannot_be_rebuilt(self) -> None:
        """attempt_from_json is Attempt(...); the constructor invariant applies."""
        payload = attempt_to_json(_attempt())
        payload["state"] = AttemptState.ABANDONED.value
        payload["escalation_id"] = None
        with pytest.raises(ContractViolationError, match="ABANDONED requires an escalation_id"):
            attempt_from_json(payload)

    def test_abandoned_round_trips_when_an_escalation_id_is_bound(self) -> None:
        original = _attempt(state=AttemptState.ABANDONED, escalation_id="esc-a1-0")
        assert attempt_from_json(attempt_to_json(original)) == original

    def test_raw_measurements_survive_the_round_trip(self) -> None:
        restored = attempt_from_json(attempt_to_json(_attempt()))
        assert restored.result is not None
        assert dict(restored.result.raw_measurements) == {
            "baseline_s": 24.5,
            "candidate_s": 9.8,
        }

    def test_iteration_round_trips_including_the_proposal(self) -> None:
        original = _record()
        assert iteration_from_json(iteration_to_json(original)) == original

    def test_a_proposal_survives_well_enough_to_be_replayed(self) -> None:
        """Resume re-applies from the journal, so the digest must be stable."""
        original = _record(IterationPhase.PROPOSED)
        restored = iteration_from_json(iteration_to_json(original))
        assert restored.proposal.digest() == original.proposal.digest()

    def test_escalation_round_trips(self) -> None:
        request = EscalationRequest(
            request_id="esc-a1-4",
            problem_id="speedup-1",
            attempt_id="a1",
            round_id="r0",
            reason=EscalationReason.CAP_EXHAUSTED,
            summary="cap exhausted on steps",
            cap=Cap(max_steps=10, max_tokens=1000, max_wall_clock_seconds=60.0),
            consumed=CapConsumption(steps=10, tokens=900, wall_clock_seconds=12.0),
            created_at_ms=99,
            best_result=_result(),
        )
        assert escalation_from_json(escalation_to_json(request)) == request


class TestStoreBehaviour:
    @pytest.fixture(params=["memory", "sqlite"])
    async def store(self, request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
        if request.param == "memory":
            yield InMemoryCheckpointStore()
            return
        sqlite = SqliteCheckpointStore(tmp_path / "checkpoints.db")
        await sqlite.open()
        yield sqlite
        await sqlite.close()

    async def test_save_then_load(self, store) -> None:  # type: ignore[no-untyped-def]
        await store.save_attempt(_attempt())
        assert await store.load_attempt("a1") == _attempt()

    async def test_unknown_attempt_loads_as_none(self, store) -> None:  # type: ignore[no-untyped-def]
        assert await store.load_attempt("nope") is None

    async def test_a_stale_checkpoint_cannot_rewind_a_newer_one(self, store) -> None:  # type: ignore[no-untyped-def]
        """A rewind looks like data loss hours later, so it fails now."""
        await store.save_attempt(_attempt())
        stale = _attempt().evolve(now_ms=1, checkpoint_seq=2)

        with pytest.raises(CheckpointError):
            await store.save_attempt(stale)
        loaded = await store.load_attempt("a1")
        assert loaded is not None
        assert loaded.checkpoint_seq == 5

    async def test_commit_iteration_writes_journal_and_attempt_together(self, store) -> None:  # type: ignore[no-untyped-def]
        attempt = _attempt()
        await store.commit_iteration(_record(), attempt)

        assert await store.load_attempt("a1") == attempt
        assert await store.latest_iteration("a1") == _record()

    async def test_commit_refuses_a_record_from_another_attempt(self, store) -> None:  # type: ignore[no-untyped-def]
        mismatched = IterationRecord(
            attempt_id="other",
            iteration_index=0,
            phase=IterationPhase.PROPOSED,
            proposal=Proposal(proposal_id="p0"),
            started_at_ms=1,
            updated_at_ms=1,
        )
        with pytest.raises(CheckpointError):
            await store.commit_iteration(mismatched, _attempt())

    async def test_iterations_come_back_in_index_order(self, store) -> None:  # type: ignore[no-untyped-def]
        attempt = _attempt()
        for index in (2, 0, 1):
            await store.commit_iteration(
                IterationRecord(
                    attempt_id="a1",
                    iteration_index=index,
                    phase=IterationPhase.VERIFIED,
                    proposal=Proposal(proposal_id=f"p{index}"),
                    started_at_ms=1,
                    updated_at_ms=1,
                ),
                attempt,
            )

        assert [r.iteration_index for r in await store.iterations("a1")] == [0, 1, 2]
        latest = await store.latest_iteration("a1")
        assert latest is not None
        assert latest.iteration_index == 2

    async def test_a_phase_advance_overwrites_the_same_iteration(self, store) -> None:  # type: ignore[no-untyped-def]
        attempt = _attempt()
        proposed = _record(IterationPhase.PROPOSED)
        await store.commit_iteration(proposed, attempt)
        await store.commit_iteration(proposed.advance(IterationPhase.APPLIED, now_ms=30), attempt)

        records = await store.iterations("a1")
        assert len(records) == 1
        assert records[0].phase is IterationPhase.APPLIED

    async def test_escalations_round_trip_through_the_store(self, store) -> None:  # type: ignore[no-untyped-def]
        request = EscalationRequest(
            request_id="esc-a1-4",
            problem_id="speedup-1",
            attempt_id="a1",
            round_id="r0",
            reason=EscalationReason.NO_VIABLE_APPROACH,
            summary="stuck",
            cap=Cap(max_steps=10, max_tokens=1000, max_wall_clock_seconds=60.0),
            consumed=CapConsumption(),
            created_at_ms=99,
        )
        await store.save_escalation(request)

        assert await store.load_escalation("esc-a1-4") == request
        assert await store.load_escalation("missing") is None


class TestDurability:
    async def test_checkpoints_survive_closing_and_reopening_the_database(
        self, tmp_path: Path
    ) -> None:
        """The actual requirement: survive the process, not just the object."""
        db = tmp_path / "nested" / "checkpoints.db"
        async with SqliteCheckpointStore(db) as store:
            await store.commit_iteration(_record(), _attempt())

        async with SqliteCheckpointStore(db) as reopened:
            assert await reopened.load_attempt("a1") == _attempt()
            assert await reopened.latest_iteration("a1") == _record()

    async def test_using_a_closed_store_is_loud(self, tmp_path: Path) -> None:
        store = SqliteCheckpointStore(tmp_path / "checkpoints.db")
        with pytest.raises(CheckpointError):
            await store.load_attempt("a1")
