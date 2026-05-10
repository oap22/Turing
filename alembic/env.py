"""Alembic migration environment for the Turing memory store.

This environment uses synchronous SQLAlchemy with SQLite.  At runtime the
database URL is resolved from the ``TURING_DB_PATH`` environment variable
(falling back to the value in ``alembic.ini``).
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# Alembic Config object — provides access to values in alembic.ini.
config = context.config

# Set up Python logging from the config file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Migrations are hand-written raw SQL (op.create_table / op.execute); the
# project does not define SQLAlchemy declarative models. As a result
# `alembic check` and `alembic revision --autogenerate` are unsupported —
# they require a MetaData object to diff against the DB. See README.md
# ("Database migrations") and issue #145.
target_metadata = None


def _get_url() -> str:
    """Resolve the database URL.

    Prefer the ``TURING_DB_PATH`` environment variable so the same config
    that the application uses also drives migrations.
    """
    db_path = os.environ.get("TURING_DB_PATH")
    if db_path:
        resolved = Path(db_path).resolve()
        return f"sqlite:///{resolved}"
    return config.get_main_option("sqlalchemy.url", "sqlite:///./data/turing.db")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This emits the SQL statements to stdout rather than executing them
    against a live database.
    """
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    url = _get_url()
    connectable = create_engine(
        url,
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
