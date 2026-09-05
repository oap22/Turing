"""Hot-path covering indexes — remove temp-B-tree sorts from the read path.

Revision ID: 003
Revises: 002
Create Date: 2026-08-13 00:00:00.000000

Every hot read in `memory/store.py` filters on one column and orders by
another. With single-column indexes SQLite could use the index for the filter
but then had to materialise every matched row into a temp B-tree just to sort
it — even when the query ends in `LIMIT 1`. `EXPLAIN QUERY PLAN` showed
"USE TEMP B-TREE FOR ORDER BY" on all of them, and `task_outcomes` had no
index at all, so its filter was a full table scan.

Pairing the filter column with the sort column lets the index supply the
ordering, so `LIMIT` stops early instead of after a full sort. Each new index
starts with the same column as the single-column index it replaces, so the
old one is a redundant prefix — dropping it removes a B-tree that every
INSERT had to maintain, which speeds up writes as well.

`memory/store.py` keeps its own idempotent `CREATE INDEX IF NOT EXISTS`
schema for runtime databases; this revision is the migration-managed mirror
of the same change.
"""

from __future__ import annotations

from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # get_active_conversation: WHERE channel_id = ? ORDER BY last_message_at
    # DESC LIMIT 1 — runs once per inbound message.
    op.create_index(
        "idx_conversations_channel_recent",
        "conversations",
        ["channel_id", "last_message_at"],
    )
    op.drop_index("idx_conversations_channel", table_name="conversations")

    # get_recent_messages / get_messages: filter by conversation, order by time.
    op.create_index(
        "idx_messages_conversation_time",
        "messages",
        ["conversation_id", "timestamp"],
    )
    op.drop_index("idx_messages_conversation", table_name="messages")

    # get_audit_log: optional user filter, always ordered by timestamp.
    op.create_index(
        "idx_audit_log_user_time",
        "audit_log",
        ["user_id", "timestamp"],
    )
    op.drop_index("idx_audit_log_user", table_name="audit_log")

    # get_task_outcomes: previously a full scan plus a sort.
    op.create_index(
        "idx_task_outcomes_tool_created",
        "task_outcomes",
        ["tool_used", "created_at"],
    )
    op.create_index(
        "idx_task_outcomes_created",
        "task_outcomes",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_task_outcomes_created", table_name="task_outcomes")
    op.drop_index("idx_task_outcomes_tool_created", table_name="task_outcomes")

    op.create_index("idx_audit_log_user", "audit_log", ["user_id"])
    op.drop_index("idx_audit_log_user_time", table_name="audit_log")

    op.create_index("idx_messages_conversation", "messages", ["conversation_id"])
    op.drop_index("idx_messages_conversation_time", table_name="messages")

    op.create_index("idx_conversations_channel", "conversations", ["channel_id"])
    op.drop_index("idx_conversations_channel_recent", table_name="conversations")
