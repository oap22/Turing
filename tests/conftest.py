"""Shared pytest fixtures for the Turing test suite."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from turing.config import TuringConfig

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Exit watchdog (issue #384)
# ---------------------------------------------------------------------------
# The suite intermittently finishes (summary printed, coverage written) but the
# process never exits — a lingering non-daemon thread or unclosed loop keeps
# Python alive and CI hangs until the workflow timeout. A daemon timer armed at
# session finish force-exits with the *real* pytest status after a grace
# period; on a normal run the process exits first and the timer dies with it.
# Before exiting it names the surviving non-daemon threads so the next hang
# identifies its culprit instead of stalling silently.

_EXIT_GRACE_SECONDS = 30.0


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    import faulthandler
    import os
    import sys
    import threading

    def _stragglers() -> list[threading.Thread]:
        return [
            t
            for t in threading.enumerate()
            if t is not threading.current_thread() and not t.daemon and t.is_alive()
        ]

    # Report immediately rather than only after the grace period. A leak that
    # the timer would eventually paper over still shows up in a green run's
    # log, so the next one is caught while it is cheap to fix.
    survivors = _stragglers()
    if survivors:
        print(
            f"\n[conftest watchdog] {len(survivors)} non-daemon thread(s) survived the "
            f"session: {[t.name for t in survivors]}. Something opened a resource and "
            "never closed it — an aiosqlite handle runs on a non-daemon worker thread, "
            "so this is what hangs CI. See issue #384.",
            file=sys.stderr,
            flush=True,
        )

    def _force_exit() -> None:
        print(
            f"\n[conftest watchdog] process still alive {_EXIT_GRACE_SECONDS}s after "
            f"session finish; non-daemon threads: "
            f"{[t.name for t in _stragglers()] or 'none visible'}. Stacks of every "
            f"live thread follow; forcing exit({exitstatus}). See issue #384.",
            file=sys.stderr,
            flush=True,
        )
        # Names alone rarely identify the culprit — "Thread-36" says nothing.
        # The stacks name the frame each straggler is parked in.
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
        os._exit(exitstatus)

    timer = threading.Timer(_EXIT_GRACE_SECONDS, _force_exit)
    timer.daemon = True
    timer.start()


# ---------------------------------------------------------------------------
# LLMResponse stub (mirrors the shape used by turing.llm)
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """Lightweight representation of an LLM completion used in tests."""

    content: str = ""
    model: str = "test-model"
    provider: str = "test"
    tokens_used: int = 0
    latency_ms: float = 0.0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_dir(tmp_path: Path) -> Path:
    """Return the pytest-provided temporary directory."""
    return tmp_path


@pytest.fixture()
def temp_db(tmp_path: Path) -> Path:
    """Return a temporary SQLite database file path.

    The parent directory is guaranteed to exist but the file itself is *not*
    created — this lets tests exercise the "create-on-first-use" behaviour.
    """
    return tmp_path / "test_turing.db"


@pytest.fixture()
def mock_config(tmp_path: Path) -> TuringConfig:
    """Build a ``TuringConfig`` with sensible test defaults.

    Environment variables are isolated via ``monkeypatch`` semantics (we
    patch ``env_file`` to ``None`` so no real ``.env`` is loaded).
    """
    db_path = tmp_path / "data" / "turing.db"
    embedding_path = tmp_path / "models" / "all-MiniLM-L6-v2"
    embedding_path.mkdir(parents=True, exist_ok=True)

    with patch.dict(
        "os.environ",
        {
            "TURING_NODE_NAME": "test-node",
            "TURING_NODE_ID": "00000000-0000-0000-0000-000000000001",
            "TURING_ANTHROPIC_API_KEY": "test-anthropic-key",
            "TURING_DB_PATH": str(db_path),
            "TURING_EMBEDDING_MODEL_PATH": str(embedding_path),
            "TURING_ENV": "development",
            "TURING_LOG_LEVEL": "DEBUG",
            "TURING_MESH_ENABLED": "false",
            "TURING_SANDBOX_ENABLED": "false",
        },
        clear=False,
    ):
        config = TuringConfig(_env_file=None)  # type: ignore[call-arg]
    return config


@pytest.fixture()
def mock_llm_response() -> type[LLMResponse]:
    """Return the ``LLMResponse`` class so tests can construct instances.

    Usage::

        def test_something(mock_llm_response):
            resp = mock_llm_response(content="Hello!", model="gemma3:1b")
            assert resp.content == "Hello!"
    """
    return LLMResponse


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
