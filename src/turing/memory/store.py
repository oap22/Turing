"""Persistent memory store backed by SQLite via aiosqlite."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL,
    last_message_at TIMESTAMP NOT NULL,
    summary TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_conversations_channel
    ON conversations(channel_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    user_id TEXT DEFAULT '',
    user_name TEXT DEFAULT '',
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp
    ON messages(timestamp);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_subject
    ON facts(subject);

CREATE TABLE IF NOT EXISTS user_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    user_name TEXT DEFAULT '',
    preference_key TEXT NOT NULL,
    preference_value TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    UNIQUE(user_id, preference_key)
);

CREATE INDEX IF NOT EXISTS idx_user_preferences_user
    ON user_preferences(user_id);

CREATE TABLE IF NOT EXISTS task_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_description TEXT NOT NULL,
    tool_used TEXT DEFAULT '',
    success BOOLEAN NOT NULL DEFAULT 1,
    error_message TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    user_id TEXT DEFAULT '',
    action TEXT NOT NULL,
    tool_name TEXT DEFAULT '',
    arguments TEXT DEFAULT '',
    result TEXT DEFAULT '',
    risk_level TEXT DEFAULT 'low',
    approved BOOLEAN NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
    ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_user
    ON audit_log(user_id);
"""


class MemoryStore:
    """Async SQLite store for conversations, facts, preferences, and audit data."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    # ── lifecycle ──────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open the database connection and create tables."""
        if self._db is None:
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()
        logger.info("memory_store_initialized", db_path=self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        """Return the active database connection or raise."""
        if self._db is None:
            raise RuntimeError("MemoryStore not initialized — call initialize() first")
        return self._db

    # ── conversations ─────────────────────────────────────────────────

    async def create_conversation(self, channel_id: str) -> str:
        """Create a new conversation and return its id."""
        conv_id = str(uuid.uuid4())
        now = _utcnow()
        await self.db.execute(
            "INSERT INTO conversations (id, channel_id, started_at, last_message_at) VALUES (?, ?, ?, ?)",
            (conv_id, channel_id, now, now),
        )
        await self.db.commit()
        return conv_id

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        """Fetch a single conversation by id."""
        cursor = await self.db.execute(
            "SELECT * FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_active_conversation(self, channel_id: str) -> dict[str, Any] | None:
        """Return the most recent conversation for a channel."""
        cursor = await self.db.execute(
            "SELECT * FROM conversations WHERE channel_id = ? ORDER BY last_message_at DESC LIMIT 1",
            (channel_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_conversation_summary(self, conversation_id: str, summary: str) -> None:
        """Set the summary for a conversation."""
        await self.db.execute(
            "UPDATE conversations SET summary = ? WHERE id = ?",
            (summary, conversation_id),
        )
        await self.db.commit()

    async def touch_conversation(self, conversation_id: str) -> None:
        """Update the last_message_at timestamp."""
        await self.db.execute(
            "UPDATE conversations SET last_message_at = ? WHERE id = ?",
            (_utcnow(), conversation_id),
        )
        await self.db.commit()

    # ── messages ──────────────────────────────────────────────────────

    async def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        user_id: str = "",
        user_name: str = "",
    ) -> int:
        """Insert a message and return its row id."""
        now = _utcnow()
        cursor = await self.db.execute(
            "INSERT INTO messages (conversation_id, role, content, timestamp, user_id, user_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, now, user_id, user_name),
        )
        await self.db.execute(
            "UPDATE conversations SET last_message_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_messages(self, conversation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return messages for a conversation, most recent last."""
        cursor = await self.db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY timestamp ASC LIMIT ?",
            (conversation_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_recent_messages(self, channel_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent messages across conversations in a channel."""
        cursor = await self.db.execute(
            """
            SELECT m.* FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE c.channel_id = ?
            ORDER BY m.timestamp DESC
            LIMIT ?
            """,
            (channel_id, limit),
        )
        rows = await cursor.fetchall()
        # Return in chronological order
        return [dict(r) for r in reversed(rows)]

    async def get_message_by_id(self, message_id: int) -> dict[str, Any] | None:
        """Fetch a single message by its id."""
        cursor = await self.db.execute(
            "SELECT * FROM messages WHERE id = ?",
            (message_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def search_messages(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search over message content."""
        cursor = await self.db.execute(
            "SELECT * FROM messages WHERE content LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── facts ─────────────────────────────────────────────────────────

    async def add_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        confidence: float = 1.0,
        source: str = "",
    ) -> int:
        """Insert a fact triple and return its row id."""
        now = _utcnow()
        cursor = await self.db.execute(
            "INSERT INTO facts (subject, predicate, object, confidence, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (subject, predicate, obj, confidence, source, now, now),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_fact(self, fact_id: int) -> dict[str, Any] | None:
        """Fetch a single fact by id."""
        cursor = await self.db.execute("SELECT * FROM facts WHERE id = ?", (fact_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_fact(
        self,
        fact_id: int,
        subject: str | None = None,
        predicate: str | None = None,
        obj: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Update fields of an existing fact."""
        updates: list[str] = []
        values: list[Any] = []
        if subject is not None:
            updates.append("subject = ?")
            values.append(subject)
        if predicate is not None:
            updates.append("predicate = ?")
            values.append(predicate)
        if obj is not None:
            updates.append("object = ?")
            values.append(obj)
        if confidence is not None:
            updates.append("confidence = ?")
            values.append(confidence)
        if not updates:
            return
        updates.append("updated_at = ?")
        values.append(_utcnow())
        values.append(fact_id)
        await self.db.execute(
            f"UPDATE facts SET {', '.join(updates)} WHERE id = ?",
            tuple(values),
        )
        await self.db.commit()

    async def delete_fact(self, fact_id: int) -> None:
        """Delete a fact by id."""
        await self.db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        await self.db.commit()

    async def search_facts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search facts by subject, predicate, or object text."""
        cursor = await self.db.execute(
            """
            SELECT * FROM facts
            WHERE subject LIKE ? OR predicate LIKE ? OR object LIKE ?
            ORDER BY confidence DESC, updated_at DESC
            LIMIT ?
            """,
            (f"%{query}%", f"%{query}%", f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── user preferences ──────────────────────────────────────────────

    async def get_user_preferences(self, user_id: str) -> dict[str, str]:
        """Return all preferences for a user as a key-value dict."""
        cursor = await self.db.execute(
            "SELECT preference_key, preference_value FROM user_preferences WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return {row["preference_key"]: row["preference_value"] for row in rows}

    async def set_user_preference(
        self,
        user_id: str,
        key: str,
        value: str,
        user_name: str = "",
    ) -> None:
        """Upsert a single user preference."""
        now = _utcnow()
        await self.db.execute(
            """
            INSERT INTO user_preferences (user_id, user_name, preference_key, preference_value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, preference_key) DO UPDATE SET
                preference_value = excluded.preference_value,
                user_name = excluded.user_name,
                updated_at = excluded.updated_at
            """,
            (user_id, user_name, key, value, now),
        )
        await self.db.commit()

    async def delete_user_preference(self, user_id: str, key: str) -> None:
        """Remove a single user preference."""
        await self.db.execute(
            "DELETE FROM user_preferences WHERE user_id = ? AND preference_key = ?",
            (user_id, key),
        )
        await self.db.commit()

    # ── task outcomes ─────────────────────────────────────────────────

    async def add_task_outcome(
        self,
        task_description: str,
        tool_used: str = "",
        success: bool = True,
        error_message: str = "",
        duration_ms: int = 0,
    ) -> int:
        """Record the outcome of a tool/task execution."""
        cursor = await self.db.execute(
            "INSERT INTO task_outcomes (task_description, tool_used, success, error_message, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_description, tool_used, success, error_message, duration_ms, _utcnow()),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_task_outcomes(
        self,
        tool_used: str | None = None,
        success: bool | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query task outcomes with optional filters."""
        query = "SELECT * FROM task_outcomes WHERE 1=1"
        params: list[Any] = []
        if tool_used is not None:
            query += " AND tool_used = ?"
            params.append(tool_used)
        if success is not None:
            query += " AND success = ?"
            params.append(success)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self.db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── audit log ─────────────────────────────────────────────────────

    async def log_audit(
        self,
        action: str,
        user_id: str = "",
        tool_name: str = "",
        arguments: dict[str, Any] | None = None,
        result: str = "",
        risk_level: str = "low",
        approved: bool = True,
    ) -> int:
        """Record an action in the audit log."""
        args_json = json.dumps(arguments) if arguments else ""
        cursor = await self.db.execute(
            "INSERT INTO audit_log (timestamp, user_id, action, tool_name, arguments, result, risk_level, approved) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_utcnow(), user_id, action, tool_name, args_json, result, risk_level, approved),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_audit_log(
        self,
        user_id: str | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit log entries with optional filters."""
        query = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        if action is not None:
            query += " AND action = ?"
            params.append(action)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        cursor = await self.db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()
