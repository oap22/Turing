"""Regression test for issue #145.

`alembic check` is not supported in this project — the migrations are
raw-SQL (no declarative MetaData), so the autogenerate machinery has
nothing to diff against. Rather than wire a fake `target_metadata`,
we document the constraint and lock it in here so a future contributor
can't quietly remove the note from `alembic/env.py` or README.md.
"""

from __future__ import annotations

from pathlib import Path


def test_env_py_documents_unsupported_alembic_check() -> None:
    text = Path("alembic/env.py").read_text()
    assert "target_metadata = None" in text
    assert "alembic check" in text, (
        "alembic/env.py must explain why target_metadata is None — see issue #145"
    )


def test_readme_documents_alembic_check_unsupported() -> None:
    text = Path("README.md").read_text()
    assert "Database migrations" in text
    assert "alembic check" in text and "not supported" in text, (
        "README.md must document that `alembic check` is unsupported — see issue #145"
    )
