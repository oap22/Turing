"""Migration 002 — close-the-loop persistence (issue #124).

Alembic isn't installed in the test env (the project ships migration files as
static SQL templates and applies them via the production runtime). These
tests directly execute the DDL the migration would emit against an in-memory
SQLite, mirroring what `alembic upgrade 002` would do in production.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

MIGRATION_PATH = Path("alembic/versions/002_close_the_loop_persistence.py")


def test_migration_file_exists_and_declares_revision():
    spec = importlib.util.spec_from_file_location("mig_002", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    # Skip exec — alembic.op import will fail without alembic installed.
    text = MIGRATION_PATH.read_text()
    assert 'revision: str = "002"' in text
    assert 'down_revision: str | None = "001"' in text


def _ddl_from_migration() -> str:
    """The pure-SQL form of the migration upgrade — kept in sync with the
    `alembic.op.create_table` calls in `002_close_the_loop_persistence.py`."""
    return """
    CREATE TABLE episode_rewards (
        episode_id TEXT NOT NULL,
        source TEXT NOT NULL,
        value REAL NOT NULL,
        recorded_at_ms INTEGER NOT NULL,
        discord_user_id TEXT,
        discord_message_id TEXT,
        PRIMARY KEY (episode_id, source, recorded_at_ms)
    );
    CREATE INDEX idx_episode_rewards_episode ON episode_rewards(episode_id);

    CREATE TABLE task_messages (
        message_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        posted_at_ms INTEGER NOT NULL
    );
    CREATE INDEX idx_task_messages_task ON task_messages(task_id);

    CREATE TABLE subtask_threads (
        thread_id TEXT PRIMARY KEY,
        subtask_id TEXT NOT NULL,
        posted_at_ms INTEGER NOT NULL
    );
    CREATE INDEX idx_subtask_threads_subtask ON subtask_threads(subtask_id);

    CREATE TABLE lessons (
        task_id TEXT PRIMARY KEY,
        specialty TEXT NOT NULL,
        text TEXT NOT NULL,
        embedding BLOB NOT NULL,
        created_at_ms INTEGER NOT NULL,
        pinned_until_ms INTEGER,
        schema_version INTEGER NOT NULL DEFAULT 2
    );
    CREATE INDEX idx_lessons_specialty ON lessons(specialty);

    CREATE TABLE last_canary_worker (
        specialty TEXT PRIMARY KEY,
        worker_id TEXT NOT NULL
    );

    CREATE TABLE hard_examples (
        specialty TEXT NOT NULL,
        adapter_name TEXT NOT NULL,
        adapter_version TEXT NOT NULL,
        eval_id TEXT NOT NULL,
        expected TEXT NOT NULL,
        got TEXT NOT NULL,
        failed_at_ms INTEGER NOT NULL,
        PRIMARY KEY (adapter_name, adapter_version, eval_id)
    );
    CREATE INDEX idx_hard_examples_adapter ON hard_examples(adapter_name, adapter_version);
    """


def _fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_ddl_from_migration())
    return conn


def test_ddl_creates_all_close_the_loop_tables():
    conn = _fresh_conn()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {
        "episode_rewards",
        "task_messages",
        "subtask_threads",
        "lessons",
        "last_canary_worker",
        "hard_examples",
    } <= names


def test_episode_rewards_round_trip():
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO episode_rewards"
        " (episode_id, source, value, recorded_at_ms, discord_user_id, discord_message_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("s-1", "subtask_thumb", 1.0, 100, "op", "m-1"),
    )
    rows = conn.execute(
        "SELECT episode_id, source, value, discord_user_id FROM episode_rewards"
    ).fetchall()
    assert rows == [("s-1", "subtask_thumb", 1.0, "op")]


def test_episode_rewards_pk_prevents_duplicate_event():
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO episode_rewards (episode_id, source, value, recorded_at_ms)"
        " VALUES ('s-1', 'subtask_thumb', 1.0, 100)"
    )
    try:
        conn.execute(
            "INSERT INTO episode_rewards (episode_id, source, value, recorded_at_ms)"
            " VALUES ('s-1', 'subtask_thumb', 1.0, 100)"
        )
    except sqlite3.IntegrityError:
        return
    raise AssertionError("expected IntegrityError on duplicate PK")


def test_lessons_blob_embedding_and_default_schema_version():
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO lessons (task_id, specialty, text, embedding, created_at_ms)"
        " VALUES (?, ?, ?, ?, ?)",
        ("t-1", "research", "cite sources", b"\x01\x02\x03", 1000),
    )
    row = conn.execute(
        "SELECT specialty, embedding, schema_version, pinned_until_ms FROM lessons WHERE task_id='t-1'"
    ).fetchone()
    assert row[0] == "research"
    assert bytes(row[1]) == b"\x01\x02\x03"
    assert row[2] == 2
    assert row[3] is None


def test_task_and_subtask_surface_tables_round_trip():
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO task_messages (message_id, task_id, posted_at_ms) VALUES ('m1', 't1', 1)"
    )
    conn.execute(
        "INSERT INTO subtask_threads (thread_id, subtask_id, posted_at_ms) VALUES ('th1', 'st1', 1)"
    )
    msg = conn.execute("SELECT task_id FROM task_messages WHERE message_id='m1'").fetchone()
    thr = conn.execute("SELECT subtask_id FROM subtask_threads WHERE thread_id='th1'").fetchone()
    assert msg == ("t1",)
    assert thr == ("st1",)


def test_hard_examples_pk_and_adapter_query():
    conn = _fresh_conn()
    rows = [
        ("research", "r", "v1", "e1", "exp", "got", 100),
        ("research", "r", "v1", "e2", "exp2", "got2", 101),
        ("research", "r", "v2", "e1", "exp3", "got3", 200),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO hard_examples"
            " (specialty, adapter_name, adapter_version, eval_id, expected, got, failed_at_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            r,
        )
    cnt = conn.execute(
        "SELECT COUNT(*) FROM hard_examples WHERE adapter_name='r' AND adapter_version='v1'"
    ).fetchone()
    assert cnt == (2,)


def test_last_canary_worker_round_trip():
    conn = _fresh_conn()
    conn.execute("INSERT INTO last_canary_worker (specialty, worker_id) VALUES ('x', 'w-a')")
    wid = conn.execute("SELECT worker_id FROM last_canary_worker WHERE specialty='x'").fetchone()
    assert wid == ("w-a",)
