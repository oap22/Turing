"""Initial schema — conversations, messages, facts, preferences, tasks, audit.

Revision ID: 001
Revises: None
Create Date: 2025-01-01 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── conversations ────────────────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("channel_id", sa.Text, nullable=False),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("last_message_at", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, server_default=""),
    )
    op.create_index("idx_conversations_channel", "conversations", ["channel_id"])

    # ── messages ─────────────────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            sa.Text,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, server_default=""),
        sa.Column("user_name", sa.Text, server_default=""),
    )
    op.create_index("idx_messages_conversation", "messages", ["conversation_id"])
    op.create_index("idx_messages_timestamp", "messages", ["timestamp"])

    # ── facts ────────────────────────────────────────────────────────
    op.create_table(
        "facts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("predicate", sa.Text, nullable=False),
        sa.Column("object", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("source", sa.Text, server_default=""),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )
    op.create_index("idx_facts_subject", "facts", ["subject"])

    # ── user_preferences ─────────────────────────────────────────────
    op.create_table(
        "user_preferences",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("user_name", sa.Text, server_default=""),
        sa.Column("preference_key", sa.Text, nullable=False),
        sa.Column("preference_value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.UniqueConstraint("user_id", "preference_key"),
    )
    op.create_index("idx_user_preferences_user", "user_preferences", ["user_id"])

    # ── task_outcomes ────────────────────────────────────────────────
    op.create_table(
        "task_outcomes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_description", sa.Text, nullable=False),
        sa.Column("tool_used", sa.Text, server_default=""),
        sa.Column("success", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("error_message", sa.Text, server_default=""),
        sa.Column("duration_ms", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.Text, nullable=False),
    )

    # ── audit_log ────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, server_default=""),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("tool_name", sa.Text, server_default=""),
        sa.Column("arguments", sa.Text, server_default=""),
        sa.Column("result", sa.Text, server_default=""),
        sa.Column("risk_level", sa.Text, server_default="low"),
        sa.Column("approved", sa.Boolean, nullable=False, server_default="1"),
    )
    op.create_index("idx_audit_log_timestamp", "audit_log", ["timestamp"])
    op.create_index("idx_audit_log_user", "audit_log", ["user_id"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("task_outcomes")
    op.drop_table("user_preferences")
    op.drop_table("facts")
    op.drop_table("messages")
    op.drop_table("conversations")
