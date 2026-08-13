"""Durable checkpoints: the codec, and two stores that use it.

The requirement this module exists for: the operator's Claude subscription
closes at unpredictable points, and an interruption must cost the remainder of
an iteration rather than the attempt. That reduces to two properties.

**Exactly-once budget.** :meth:`SqliteCheckpointStore.commit_iteration` writes
the journal entry and the attempt that charged it in a single transaction. The
attempt's ``consumed`` is therefore durable if and only if the phase that spent
it is durable, so a crash can neither lose spend nor charge it twice.

**Monotonic checkpoints.** ``save_attempt`` refuses a write whose
``checkpoint_seq`` is lower than what is already stored. Two processes racing
on one attempt is a bug, but a *stale* process quietly rewinding a good
checkpoint is a bug that looks like data loss hours later, so it fails loudly.

The JSON codec is public because the journal is also the audit trail: the
round orchestrator and, later, the loop-2 self-edit summary read these rows.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import structlog

from turing.research.contracts import (
    Attempt,
    AttemptState,
    Cap,
    CapConsumption,
    EscalationReason,
    EscalationRequest,
    VerificationResult,
)
from turing.research.solver.errors import CheckpointError
from turing.research.solver.models import (
    CommandOutcome,
    FileEdit,
    IterationPhase,
    IterationRecord,
    Proposal,
)

if TYPE_CHECKING:
    from types import TracebackType

__all__ = [
    "InMemoryCheckpointStore",
    "SqliteCheckpointStore",
    "attempt_from_json",
    "attempt_to_json",
    "escalation_from_json",
    "escalation_to_json",
    "iteration_from_json",
    "iteration_to_json",
]

logger = structlog.get_logger("turing.research.solver.checkpoints")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS research_attempts (
    attempt_id     TEXT PRIMARY KEY,
    problem_id     TEXT NOT NULL,
    round_id       TEXT NOT NULL,
    state          TEXT NOT NULL,
    checkpoint_seq INTEGER NOT NULL,
    updated_at_ms  INTEGER,
    payload        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_attempts_round
    ON research_attempts(round_id);

CREATE TABLE IF NOT EXISTS research_iterations (
    attempt_id      TEXT NOT NULL,
    iteration_index INTEGER NOT NULL,
    phase           TEXT NOT NULL,
    updated_at_ms   INTEGER NOT NULL,
    payload         TEXT NOT NULL,
    PRIMARY KEY (attempt_id, iteration_index)
);

CREATE TABLE IF NOT EXISTS research_escalations (
    request_id  TEXT PRIMARY KEY,
    attempt_id  TEXT NOT NULL,
    problem_id  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    payload     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_escalations_attempt
    ON research_escalations(attempt_id);
"""


# --------------------------------------------------------------------------- #
# Codec
# --------------------------------------------------------------------------- #


def _result_to_json(result: VerificationResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "problem_id": result.problem_id,
        "verifier_id": result.verifier_id,
        "score": result.score,
        "passed_correctness": result.passed_correctness,
        "score_scale": result.score_scale,
        "raw_measurements": dict(result.raw_measurements),
        "detail": result.detail,
    }


def _result_from_json(payload: dict[str, Any] | None) -> VerificationResult | None:
    if payload is None:
        return None
    return VerificationResult(
        problem_id=payload["problem_id"],
        verifier_id=payload["verifier_id"],
        score=payload["score"],
        passed_correctness=payload["passed_correctness"],
        score_scale=payload["score_scale"],
        raw_measurements=payload.get("raw_measurements", {}),
        detail=payload.get("detail", ""),
    )


def _cap_to_json(cap: Cap) -> dict[str, Any]:
    return {
        "max_steps": cap.max_steps,
        "max_tokens": cap.max_tokens,
        "max_wall_clock_seconds": cap.max_wall_clock_seconds,
        "extension_count": cap.extension_count,
    }


def _cap_from_json(payload: dict[str, Any]) -> Cap:
    return Cap(
        max_steps=payload["max_steps"],
        max_tokens=payload["max_tokens"],
        max_wall_clock_seconds=payload["max_wall_clock_seconds"],
        extension_count=payload.get("extension_count", 0),
    )


def _consumption_to_json(consumed: CapConsumption) -> dict[str, Any]:
    return {
        "steps": consumed.steps,
        "tokens": consumed.tokens,
        "wall_clock_seconds": consumed.wall_clock_seconds,
    }


def _consumption_from_json(payload: dict[str, Any]) -> CapConsumption:
    return CapConsumption(
        steps=payload["steps"],
        tokens=payload["tokens"],
        wall_clock_seconds=payload["wall_clock_seconds"],
    )


def attempt_to_json(attempt: Attempt) -> dict[str, Any]:
    """Serialise an attempt checkpoint."""
    return {
        "attempt_id": attempt.attempt_id,
        "problem_id": attempt.problem_id,
        "round_id": attempt.round_id,
        "seed": attempt.seed,
        "workspace_path": str(attempt.workspace_path),
        "cap": _cap_to_json(attempt.cap),
        "consumed": _consumption_to_json(attempt.consumed),
        "state": attempt.state.value,
        "step_index": attempt.step_index,
        "checkpoint_seq": attempt.checkpoint_seq,
        "resume_token": attempt.resume_token,
        "started_at_ms": attempt.started_at_ms,
        "updated_at_ms": attempt.updated_at_ms,
        "result": _result_to_json(attempt.result),
        "escalation_id": attempt.escalation_id,
    }


def attempt_from_json(payload: dict[str, Any]) -> Attempt:
    """Rebuild an attempt checkpoint. The whole of resume is this call.

    Construction goes through :class:`~turing.research.contracts.Attempt`, so
    ``ABANDONED`` still requires a non-empty ``escalation_id``.
    """
    return Attempt(
        attempt_id=payload["attempt_id"],
        problem_id=payload["problem_id"],
        round_id=payload["round_id"],
        seed=payload["seed"],
        workspace_path=Path(payload["workspace_path"]),
        cap=_cap_from_json(payload["cap"]),
        consumed=_consumption_from_json(payload["consumed"]),
        state=AttemptState(payload["state"]),
        step_index=payload["step_index"],
        checkpoint_seq=payload["checkpoint_seq"],
        resume_token=payload.get("resume_token"),
        started_at_ms=payload.get("started_at_ms"),
        updated_at_ms=payload.get("updated_at_ms"),
        result=_result_from_json(payload.get("result")),
        escalation_id=payload.get("escalation_id"),
    )


def iteration_to_json(record: IterationRecord) -> dict[str, Any]:
    """Serialise a journal entry, proposal included.

    The proposal is stored in full, not by reference. Resume must be able to
    finish applying a change without asking the model for it again — that is
    what stops an interruption from spending the same tokens twice.
    """
    return {
        "attempt_id": record.attempt_id,
        "iteration_index": record.iteration_index,
        "phase": record.phase.value,
        "proposal": {
            "proposal_id": record.proposal.proposal_id,
            "rationale": record.proposal.rationale,
            "edits": [
                {"relative_path": e.relative_path, "content": e.content}
                for e in record.proposal.edits
            ],
            "commands": list(record.proposal.commands),
            "tokens": record.proposal.tokens,
            "no_viable_approach": record.proposal.no_viable_approach,
        },
        "started_at_ms": record.started_at_ms,
        "updated_at_ms": record.updated_at_ms,
        "applied_paths": list(record.applied_paths),
        "command_outcomes": [
            {
                "command": c.command,
                "exit_code": c.exit_code,
                "stdout_tail": c.stdout_tail,
                "stderr_tail": c.stderr_tail,
                "duration_seconds": c.duration_seconds,
            }
            for c in record.command_outcomes
        ],
        "result": _result_to_json(record.result),
        "wall_clock_seconds": record.wall_clock_seconds,
    }


def iteration_from_json(payload: dict[str, Any]) -> IterationRecord:
    """Rebuild a journal entry."""
    proposal_payload = payload["proposal"]
    proposal = Proposal(
        proposal_id=proposal_payload["proposal_id"],
        rationale=proposal_payload.get("rationale", ""),
        edits=tuple(
            FileEdit(relative_path=e["relative_path"], content=e["content"])
            for e in proposal_payload.get("edits", [])
        ),
        commands=tuple(proposal_payload.get("commands", [])),
        tokens=proposal_payload.get("tokens", 0),
        no_viable_approach=proposal_payload.get("no_viable_approach", False),
    )
    return IterationRecord(
        attempt_id=payload["attempt_id"],
        iteration_index=payload["iteration_index"],
        phase=IterationPhase(payload["phase"]),
        proposal=proposal,
        started_at_ms=payload["started_at_ms"],
        updated_at_ms=payload["updated_at_ms"],
        applied_paths=tuple(payload.get("applied_paths", [])),
        command_outcomes=tuple(
            CommandOutcome(
                command=c["command"],
                exit_code=c["exit_code"],
                stdout_tail=c.get("stdout_tail", ""),
                stderr_tail=c.get("stderr_tail", ""),
                duration_seconds=c.get("duration_seconds", 0.0),
            )
            for c in payload.get("command_outcomes", [])
        ),
        result=_result_from_json(payload.get("result")),
        wall_clock_seconds=payload.get("wall_clock_seconds", 0.0),
    )


def escalation_to_json(request: EscalationRequest) -> dict[str, Any]:
    """Serialise an escalation request.

    Note what round-trips: reason, summary, cap, consumption, best result.
    Information flows outward freely — the operator needs enough context to
    choose. Nothing flows back through this record.
    """
    return {
        "request_id": request.request_id,
        "problem_id": request.problem_id,
        "attempt_id": request.attempt_id,
        "round_id": request.round_id,
        "reason": request.reason.value,
        "summary": request.summary,
        "cap": _cap_to_json(request.cap),
        "consumed": _consumption_to_json(request.consumed),
        "created_at_ms": request.created_at_ms,
        "best_result": _result_to_json(request.best_result),
    }


def escalation_from_json(payload: dict[str, Any]) -> EscalationRequest:
    """Rebuild an escalation request."""
    return EscalationRequest(
        request_id=payload["request_id"],
        problem_id=payload["problem_id"],
        attempt_id=payload["attempt_id"],
        round_id=payload["round_id"],
        reason=EscalationReason(payload["reason"]),
        summary=payload["summary"],
        cap=_cap_from_json(payload["cap"]),
        consumed=_consumption_from_json(payload["consumed"]),
        created_at_ms=payload["created_at_ms"],
        best_result=_result_from_json(payload.get("best_result")),
    )


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #


class InMemoryCheckpointStore:
    """A store that keeps checkpoints in a dict. For tests and the bench cycle.

    Round-trips every value through the JSON codec rather than holding the
    objects, so a codec regression fails the solver's own tests instead of
    waiting to be discovered by the SQLite store in an overnight run.

    It is *not* a substitute for the SQLite store in resume tests that claim to
    prove durability: surviving a crash means surviving process exit, and
    nothing in this class does.
    """

    def __init__(self) -> None:
        self._attempts: dict[str, dict[str, Any]] = {}
        self._iterations: dict[str, dict[int, dict[str, Any]]] = {}
        self._escalations: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def save_attempt(self, attempt: Attempt) -> None:
        async with self._lock:
            self._save_attempt_locked(attempt)

    def _save_attempt_locked(self, attempt: Attempt) -> None:
        existing = self._attempts.get(attempt.attempt_id)
        if existing is not None and existing["checkpoint_seq"] > attempt.checkpoint_seq:
            raise CheckpointError(
                f"refusing to rewind attempt {attempt.attempt_id!r} from checkpoint "
                f"{existing['checkpoint_seq']} to {attempt.checkpoint_seq}"
            )
        self._attempts[attempt.attempt_id] = json.loads(json.dumps(attempt_to_json(attempt)))

    async def load_attempt(self, attempt_id: str) -> Attempt | None:
        async with self._lock:
            payload = self._attempts.get(attempt_id)
            return None if payload is None else attempt_from_json(payload)

    async def commit_iteration(self, record: IterationRecord, attempt: Attempt) -> None:
        if record.attempt_id != attempt.attempt_id:
            raise CheckpointError("iteration record and attempt disagree on attempt_id")
        async with self._lock:
            self._save_attempt_locked(attempt)
            self._iterations.setdefault(record.attempt_id, {})[record.iteration_index] = json.loads(
                json.dumps(iteration_to_json(record))
            )

    async def latest_iteration(self, attempt_id: str) -> IterationRecord | None:
        async with self._lock:
            by_index = self._iterations.get(attempt_id)
            if not by_index:
                return None
            return iteration_from_json(by_index[max(by_index)])

    async def iterations(self, attempt_id: str) -> tuple[IterationRecord, ...]:
        async with self._lock:
            by_index = self._iterations.get(attempt_id, {})
            return tuple(iteration_from_json(by_index[i]) for i in sorted(by_index))

    async def save_escalation(self, request: EscalationRequest) -> None:
        async with self._lock:
            self._escalations[request.request_id] = json.loads(
                json.dumps(escalation_to_json(request))
            )

    async def load_escalation(self, request_id: str) -> EscalationRequest | None:
        async with self._lock:
            payload = self._escalations.get(request_id)
            return None if payload is None else escalation_from_json(payload)


class SqliteCheckpointStore:
    """The real store: aiosqlite, one connection, one transaction per commit.

    Use it as an async context manager, or call :meth:`open` and :meth:`close`.
    Point it at a file for anything that must survive the process; ``:memory:``
    is fine for tests that do not claim durability.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def db_path(self) -> str:
        return self._db_path

    async def open(self) -> SqliteCheckpointStore:
        if self._db is not None:
            return self
        if self._db_path != ":memory:":
            Path(self._db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> SqliteCheckpointStore:
        return await self.open()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise CheckpointError("checkpoint store is not open; call open() first")
        return self._db

    async def save_attempt(self, attempt: Attempt) -> None:
        db = self._connection()
        async with self._lock:
            await self._write_attempt(db, attempt)
            await db.commit()

    @staticmethod
    async def _write_attempt(db: aiosqlite.Connection, attempt: Attempt) -> None:
        cursor = await db.execute(
            "SELECT checkpoint_seq FROM research_attempts WHERE attempt_id = ?",
            (attempt.attempt_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is not None and row[0] > attempt.checkpoint_seq:
            raise CheckpointError(
                f"refusing to rewind attempt {attempt.attempt_id!r} from checkpoint "
                f"{row[0]} to {attempt.checkpoint_seq}"
            )
        await db.execute(
            """
            INSERT INTO research_attempts
                (attempt_id, problem_id, round_id, state, checkpoint_seq, updated_at_ms, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id) DO UPDATE SET
                state = excluded.state,
                checkpoint_seq = excluded.checkpoint_seq,
                updated_at_ms = excluded.updated_at_ms,
                payload = excluded.payload
            """,
            (
                attempt.attempt_id,
                attempt.problem_id,
                attempt.round_id,
                attempt.state.value,
                attempt.checkpoint_seq,
                attempt.updated_at_ms,
                json.dumps(attempt_to_json(attempt)),
            ),
        )

    async def load_attempt(self, attempt_id: str) -> Attempt | None:
        db = self._connection()
        async with self._lock:
            cursor = await db.execute(
                "SELECT payload FROM research_attempts WHERE attempt_id = ?",
                (attempt_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return None if row is None else attempt_from_json(json.loads(row[0]))

    async def commit_iteration(self, record: IterationRecord, attempt: Attempt) -> None:
        """Write journal entry and attempt together, or write neither.

        The atomicity here is the exactly-once budget guarantee. Splitting it
        into two commits would create a window in which tokens are charged but
        the proposal that bought them is lost — the resumed run would call the
        backend again and pay twice.
        """
        if record.attempt_id != attempt.attempt_id:
            raise CheckpointError("iteration record and attempt disagree on attempt_id")
        db = self._connection()
        async with self._lock:
            try:
                await self._write_attempt(db, attempt)
                await db.execute(
                    """
                    INSERT INTO research_iterations
                        (attempt_id, iteration_index, phase, updated_at_ms, payload)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(attempt_id, iteration_index) DO UPDATE SET
                        phase = excluded.phase,
                        updated_at_ms = excluded.updated_at_ms,
                        payload = excluded.payload
                    """,
                    (
                        record.attempt_id,
                        record.iteration_index,
                        record.phase.value,
                        record.updated_at_ms,
                        json.dumps(iteration_to_json(record)),
                    ),
                )
            except Exception:
                await db.rollback()
                raise
            await db.commit()

    async def latest_iteration(self, attempt_id: str) -> IterationRecord | None:
        db = self._connection()
        async with self._lock:
            cursor = await db.execute(
                """
                SELECT payload FROM research_iterations
                WHERE attempt_id = ?
                ORDER BY iteration_index DESC LIMIT 1
                """,
                (attempt_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return None if row is None else iteration_from_json(json.loads(row[0]))

    async def iterations(self, attempt_id: str) -> tuple[IterationRecord, ...]:
        db = self._connection()
        async with self._lock:
            cursor = await db.execute(
                """
                SELECT payload FROM research_iterations
                WHERE attempt_id = ?
                ORDER BY iteration_index ASC
                """,
                (attempt_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return tuple(iteration_from_json(json.loads(row[0])) for row in rows)

    async def save_escalation(self, request: EscalationRequest) -> None:
        db = self._connection()
        async with self._lock:
            await db.execute(
                """
                INSERT INTO research_escalations
                    (request_id, attempt_id, problem_id, reason, created_at_ms, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET payload = excluded.payload
                """,
                (
                    request.request_id,
                    request.attempt_id,
                    request.problem_id,
                    request.reason.value,
                    request.created_at_ms,
                    json.dumps(escalation_to_json(request)),
                ),
            )
            await db.commit()

    async def load_escalation(self, request_id: str) -> EscalationRequest | None:
        db = self._connection()
        async with self._lock:
            cursor = await db.execute(
                "SELECT payload FROM research_escalations WHERE request_id = ?",
                (request_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return None if row is None else escalation_from_json(json.loads(row[0]))
