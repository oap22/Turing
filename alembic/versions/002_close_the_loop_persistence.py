"""Close-the-loop persistence — episode rewards, lessons, surfaces, canary.

Revision ID: 002
Revises: 001
Create Date: 2026-05-08 00:00:00.000000

Adds the persistent shape mirrored by the in-memory stores from PRs #128,
#129, #131, #133-#138 (close-the-loop wiring quartet, ADR 0004-0007). The
in-memory implementations stay; SQLite-backed DAOs against this schema land
in follow-up work.

Note: The `episode` and `adapters` tables introduced by ADR 0001-0003 do
not yet have a SQL backing in `001_initial_schema.py` — the ALTER TABLE
fragments described in #124 (critic_status / consumed_keys columns,
canary_eval_score column, REJECTED enum value) belong in the same revision
that first persists those stores. This revision only adds the standalone
tables that have no in-DB precedent yet.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── episode_rewards (#111 / G-series) ────────────────────────────
    op.create_table(
        "episode_rewards",
        sa.Column("episode_id", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("recorded_at_ms", sa.Integer, nullable=False),
        sa.Column("discord_user_id", sa.Text, nullable=True),
        sa.Column("discord_message_id", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint(
            "episode_id", "source", "recorded_at_ms", name="pk_episode_rewards"
        ),
    )
    op.create_index(
        "idx_episode_rewards_episode", "episode_rewards", ["episode_id"]
    )

    # ── task_messages / subtask_threads (#115) ───────────────────────
    op.create_table(
        "task_messages",
        sa.Column("message_id", sa.Text, primary_key=True),
        sa.Column("task_id", sa.Text, nullable=False),
        sa.Column("posted_at_ms", sa.Integer, nullable=False),
    )
    op.create_index("idx_task_messages_task", "task_messages", ["task_id"])

    op.create_table(
        "subtask_threads",
        sa.Column("thread_id", sa.Text, primary_key=True),
        sa.Column("subtask_id", sa.Text, nullable=False),
        sa.Column("posted_at_ms", sa.Integer, nullable=False),
    )
    op.create_index(
        "idx_subtask_threads_subtask", "subtask_threads", ["subtask_id"]
    )

    # ── lessons (#112 / #121) ────────────────────────────────────────
    op.create_table(
        "lessons",
        sa.Column("task_id", sa.Text, primary_key=True),
        sa.Column("specialty", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding", sa.LargeBinary, nullable=False),
        sa.Column("created_at_ms", sa.Integer, nullable=False),
        sa.Column("pinned_until_ms", sa.Integer, nullable=True),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="2"),
    )
    op.create_index("idx_lessons_specialty", "lessons", ["specialty"])

    # ── last_canary_worker (#109 / #118) ─────────────────────────────
    op.create_table(
        "last_canary_worker",
        sa.Column("specialty", sa.Text, primary_key=True),
        sa.Column("worker_id", sa.Text, nullable=False),
    )

    # ── hard_examples (#118) ─────────────────────────────────────────
    op.create_table(
        "hard_examples",
        sa.Column("specialty", sa.Text, nullable=False),
        sa.Column("adapter_name", sa.Text, nullable=False),
        sa.Column("adapter_version", sa.Text, nullable=False),
        sa.Column("eval_id", sa.Text, nullable=False),
        sa.Column("expected", sa.Text, nullable=False),
        sa.Column("got", sa.Text, nullable=False),
        sa.Column("failed_at_ms", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint(
            "adapter_name",
            "adapter_version",
            "eval_id",
            name="pk_hard_examples",
        ),
    )
    op.create_index(
        "idx_hard_examples_adapter",
        "hard_examples",
        ["adapter_name", "adapter_version"],
    )


def downgrade() -> None:
    op.drop_index("idx_hard_examples_adapter", table_name="hard_examples")
    op.drop_table("hard_examples")
    op.drop_table("last_canary_worker")
    op.drop_index("idx_lessons_specialty", table_name="lessons")
    op.drop_table("lessons")
    op.drop_index("idx_subtask_threads_subtask", table_name="subtask_threads")
    op.drop_table("subtask_threads")
    op.drop_index("idx_task_messages_task", table_name="task_messages")
    op.drop_table("task_messages")
    op.drop_index("idx_episode_rewards_episode", table_name="episode_rewards")
    op.drop_table("episode_rewards")
