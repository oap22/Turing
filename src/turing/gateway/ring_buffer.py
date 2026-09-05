"""SQLite-backed telemetry ring buffer.

Two eviction policies layered on the same store:

- TTL: ``prune(now_ms)`` deletes events older than ``retention_seconds``.
- Size cap: every ``append`` checks the on-disk byte total and FIFO-evicts
  the oldest rows until under the cap.

Persistence uses a regular SQLite file via ``aiosqlite`` so events survive
an in-process restart. ``query`` filters by node, event_type, and a
``since_ms`` lower bound — the slice 5 SPA's debug pane uses these as the
default filters.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite


@dataclass(frozen=True)
class RingBufferConfig:
    path: Path
    retention_seconds: int
    max_bytes: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER NOT NULL,
    node_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    duration_ms REAL,
    error TEXT,
    payload_json TEXT NOT NULL,
    bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp
    ON telemetry_events(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_telemetry_node
    ON telemetry_events(node_name);
CREATE INDEX IF NOT EXISTS idx_telemetry_event_type
    ON telemetry_events(event_type);
"""


# Rows examined per eviction pass. At steady state an append pushes the total
# over the cap by roughly one row, so a single short batch covers it; the loop
# only iterates when a large backlog has to be shed at once.
_EVICT_BATCH = 128


class RingBuffer:
    def __init__(self, config: RingBufferConfig) -> None:
        self._config = config
        self._db: aiosqlite.Connection | None = None
        # Running byte total, kept in step with the table. The size cap used to
        # be enforced with `SELECT SUM(bytes)` over the whole table on every
        # append — an O(rows) scan per event, so ingesting n events cost O(n²).
        # Tracking the total here makes the common case (still under the cap) a
        # comparison against an integer. It is seeded from the table on open,
        # so it survives restarts against a persisted database.
        self._total_bytes = 0

    # ── lifecycle ──────────────────────────────────────────────────────

    async def open(self) -> None:
        Path(self._config.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._config.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        self._total_bytes = await self._sum_bytes()

    async def _sum_bytes(self, where: str = "", params: tuple[Any, ...] = ()) -> int:
        """Total of the ``bytes`` column, optionally filtered."""
        assert self._db is not None
        cur = await self._db.execute(
            f"SELECT COALESCE(SUM(bytes), 0) AS total FROM telemetry_events {where}",
            params,
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row["total"]) if row is not None else 0

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ── writes ─────────────────────────────────────────────────────────

    async def append(self, event: dict[str, Any]) -> None:
        assert self._db is not None, "RingBuffer.open() must be called first"
        payload_json = json.dumps(event.get("payload") or {}, separators=(",", ":"))
        row_bytes = (
            len(payload_json.encode("utf-8"))
            + len(str(event.get("event_type", "")).encode("utf-8"))
            + len(str(event.get("node_name", "")).encode("utf-8"))
            + 64  # rough overhead for ints + indexes
        )
        await self._db.execute(
            """
            INSERT INTO telemetry_events
            (timestamp_ms, node_name, event_type, seq, duration_ms, error, payload_json, bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(event["timestamp_ms"]),
                str(event.get("node_name", "")),
                str(event.get("event_type", "")),
                int(event.get("seq", 0)),
                event.get("duration_ms"),
                event.get("error"),
                payload_json,
                row_bytes,
            ),
        )
        await self._db.commit()
        self._total_bytes += row_bytes
        await self._enforce_size_cap()

    async def _enforce_size_cap(self) -> None:
        """FIFO-evict the oldest rows once the byte total exceeds the cap.

        Rows are dropped while their running total stays within the excess —
        the same boundary the previous window-function query used, so eviction
        picks exactly the same rows. What changed is the cost: this walks only
        the rows it is about to delete instead of scanning the whole table
        twice per append.
        """
        assert self._db is not None
        excess = self._total_bytes - self._config.max_bytes
        if excess <= 0:
            return

        doomed: list[int] = []
        running = 0
        freed = 0
        done = False
        while not done:
            cur = await self._db.execute(
                "SELECT id, bytes FROM telemetry_events WHERE id > ? ORDER BY id ASC LIMIT ?",
                (doomed[-1] if doomed else -1, _EVICT_BATCH),
            )
            batch = await cur.fetchall()
            await cur.close()
            if not batch:
                break
            for row in batch:
                running += int(row["bytes"])
                if running > excess:
                    done = True
                    break
                doomed.append(int(row["id"]))
                freed = running

        if not doomed:
            return

        placeholders = ",".join("?" * len(doomed))
        await self._db.execute(
            f"DELETE FROM telemetry_events WHERE id IN ({placeholders})",
            tuple(doomed),
        )
        await self._db.commit()
        self._total_bytes -= freed

    # ── pruning ────────────────────────────────────────────────────────

    async def prune(self, *, now_ms: int | None = None) -> int:
        assert self._db is not None
        cutoff_ms = (now_ms if now_ms is not None else int(time.time() * 1000)) - (
            self._config.retention_seconds * 1000
        )
        # Measure before deleting so the running total stays in step with the
        # table; the TTL sweep is periodic, so the extra query is not on any
        # hot path.
        pruned_bytes = await self._sum_bytes("WHERE timestamp_ms < ?", (cutoff_ms,))
        cur = await self._db.execute(
            "DELETE FROM telemetry_events WHERE timestamp_ms < ?",
            (cutoff_ms,),
        )
        await self._db.commit()
        self._total_bytes -= pruned_bytes
        return cur.rowcount or 0

    # ── reads ──────────────────────────────────────────────────────────

    async def query(
        self,
        *,
        node_name: str | None = None,
        event_type: str | None = None,
        node_names: tuple[str, ...] | None = None,
        event_types: tuple[str, ...] | None = None,
        since_ms: int | None = None,
        min_duration_ms: float | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Filter telemetry rows.

        ``node_name`` / ``event_type`` are kept for backwards compat; the
        plural ``node_names`` / ``event_types`` lists are what the trace pane
        sends since it supports multi-select. ``min_duration_ms`` filters out
        the noisy short events when the operator wants to focus on slow calls.
        ``limit`` and ``offset`` paginate the response — slice 7's pane
        fetches recent history in pages so the initial paint stays under
        500 ms even with a 24h ring buffer at full retention.
        """
        assert self._db is not None
        clauses: list[str] = []
        params: list[Any] = []
        if node_name is not None:
            clauses.append("node_name = ?")
            params.append(node_name)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if node_names:
            placeholders = ",".join("?" for _ in node_names)
            clauses.append(f"node_name IN ({placeholders})")
            params.extend(node_names)
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(event_types)
        if since_ms is not None:
            clauses.append("timestamp_ms >= ?")
            params.append(since_ms)
        if min_duration_ms is not None:
            clauses.append("duration_ms >= ?")
            params.append(min_duration_ms)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM telemetry_events {where} ORDER BY id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
            if offset is not None:
                sql += " OFFSET ?"
                params.append(int(offset))
        elif offset is not None:
            # SQLite requires LIMIT when OFFSET is present; -1 means unbounded.
            sql += " LIMIT -1 OFFSET ?"
            params.append(int(offset))
        cur = await self._db.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "timestamp_ms": row["timestamp_ms"],
        "node_name": row["node_name"],
        "event_type": row["event_type"],
        "seq": row["seq"],
        "duration_ms": row["duration_ms"],
        "error": row["error"],
        "payload": json.loads(row["payload_json"]) if row["payload_json"] else {},
    }
