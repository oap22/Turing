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
from typing import Any, Optional

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


class RingBuffer:
    def __init__(self, config: RingBufferConfig) -> None:
        self._config = config
        self._db: Optional[aiosqlite.Connection] = None

    # ── lifecycle ──────────────────────────────────────────────────────

    async def open(self) -> None:
        Path(self._config.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._config.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

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
        await self._enforce_size_cap()

    async def _enforce_size_cap(self) -> None:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT COALESCE(SUM(bytes), 0) AS total FROM telemetry_events"
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None or row["total"] <= self._config.max_bytes:
            return
        # Delete oldest rows until under the cap.
        await self._db.execute(
            """
            DELETE FROM telemetry_events
            WHERE id IN (
                SELECT id FROM telemetry_events
                ORDER BY id ASC
                LIMIT (
                    SELECT COUNT(*) FROM (
                        SELECT id, SUM(bytes) OVER (ORDER BY id ASC) AS running
                        FROM telemetry_events
                    )
                    WHERE running <= (
                        SELECT COALESCE(SUM(bytes), 0) - ? FROM telemetry_events
                    )
                )
            )
            """,
            (self._config.max_bytes,),
        )
        await self._db.commit()

    # ── pruning ────────────────────────────────────────────────────────

    async def prune(self, *, now_ms: Optional[int] = None) -> int:
        assert self._db is not None
        cutoff_ms = (now_ms if now_ms is not None else int(time.time() * 1000)) - (
            self._config.retention_seconds * 1000
        )
        cur = await self._db.execute(
            "DELETE FROM telemetry_events WHERE timestamp_ms < ?",
            (cutoff_ms,),
        )
        await self._db.commit()
        return cur.rowcount or 0

    # ── reads ──────────────────────────────────────────────────────────

    async def query(
        self,
        *,
        node_name: Optional[str] = None,
        event_type: Optional[str] = None,
        since_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        assert self._db is not None
        clauses: list[str] = []
        params: list[Any] = []
        if node_name is not None:
            clauses.append("node_name = ?")
            params.append(node_name)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if since_ms is not None:
            clauses.append("timestamp_ms >= ?")
            params.append(since_ms)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM telemetry_events {where} ORDER BY id ASC"
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
