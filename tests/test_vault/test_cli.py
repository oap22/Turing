"""Tests for the ``turing-vault-watcher`` entry point (:mod:`turing.vault.cli`).

The adapter + index builder are exercised elsewhere; this module drives the
``_run`` poll loop and ``main`` with a mocked :class:`VaultWatcher` and a
controllable stop condition so the loop runs a bounded number of iterations —
covering cold start, reindex-on-head-change, the poll-failure ``continue``
branch, and signal-driven shutdown. No real ``git``, no real ``time.sleep``.
"""

from __future__ import annotations

import signal
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import numpy as np
import pytest

from turing.vault import cli

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        vault_root=tmp_path,
        vault_poll_interval_seconds=0.01,
        embedding_model_path=tmp_path / "model.onnx",
    )


class _LoopDriver:
    """Patches ``cli`` so ``_run`` is bounded and instrumented.

    ``poll_results`` is the side-effect list for ``watcher.poll_once`` (the
    first entry is the cold-start head). ``stop_after`` flips the registered
    SIGTERM handler once ``time.sleep`` has been called that many times, so the
    while-loop exits deterministically without a real signal or real sleep.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        poll_results: list[object],
        *,
        stop_after: int,
    ) -> None:
        self.index = MagicMock()
        self.index.snapshot.return_value = []
        self.watcher = MagicMock()
        self.watcher.poll_once.side_effect = poll_results
        self.handlers: dict[int, object] = {}
        self.sleep_calls = 0

        monkeypatch.setattr(cli, "_build_index", lambda config: self.index)
        monkeypatch.setattr(cli, "VaultWatcher", lambda **kw: self.watcher)
        monkeypatch.setattr(
            cli.signal,
            "signal",
            lambda sig, handler: self.handlers.__setitem__(sig, handler),
        )

        def fake_sleep(_seconds: float) -> None:
            self.sleep_calls += 1
            if self.sleep_calls >= stop_after:
                self.handlers[signal.SIGTERM](signal.SIGTERM, None)

        monkeypatch.setattr(cli.time, "sleep", fake_sleep)


class TestRun:
    def test_cold_start_then_reindex_on_head_change(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # cold-start head "h0", one poll returns a new head "h1" → one reindex.
        driver = _LoopDriver(monkeypatch, ["h0", "h1"], stop_after=2)

        cli._run(_config(tmp_path))

        # cold-start poll + one loop poll = 2 calls.
        assert driver.watcher.poll_once.call_count == 2
        # snapshot consulted on cold start and on the reindex log line.
        assert driver.index.snapshot.call_count >= 2

    def test_no_reindex_when_head_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        driver = _LoopDriver(monkeypatch, ["h0", "h0"], stop_after=2)

        cli._run(_config(tmp_path))

        assert driver.watcher.poll_once.call_count == 2

    def test_poll_failure_does_not_kill_loop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # cold-start ok, then a transient git error, then shutdown.
        driver = _LoopDriver(
            monkeypatch,
            ["h0", RuntimeError("transient git lock"), "h2"],
            stop_after=3,
        )

        # Must not raise — the daemon swallows one bad poll and continues.
        cli._run(_config(tmp_path))

        assert driver.watcher.poll_once.call_count >= 2

    def test_signal_shutdown_before_first_poll(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # stop_after=1 → the very first sleep triggers shutdown, so the loop
        # body breaks immediately after the cold-start poll.
        driver = _LoopDriver(monkeypatch, ["h0"], stop_after=1)

        cli._run(_config(tmp_path))

        assert driver.watcher.poll_once.call_count == 1
        # Both SIGTERM and SIGINT handlers were installed.
        assert signal.SIGTERM in driver.handlers
        assert signal.SIGINT in driver.handlers


class TestOnnxEmbedderAdapter:
    def test_returns_zero_vector_when_model_not_ready(self) -> None:
        model = MagicMock()
        model.ready = False
        adapter = cli._OnnxEmbedderAdapter(model)

        vec = adapter.embed("anything")

        assert vec.shape == (cli.EmbeddingModel.DIMENSION,)
        assert vec.dtype == np.float32
        assert not vec.any()
        model._embed_sync.assert_not_called()

    def test_delegates_to_embed_sync_when_ready(self) -> None:
        model = MagicMock()
        model.ready = True
        model._embed_sync.return_value = [1.0] * cli.EmbeddingModel.DIMENSION
        adapter = cli._OnnxEmbedderAdapter(model)

        vec = adapter.embed("hello")

        model._embed_sync.assert_called_once_with("hello")
        assert vec.dtype == np.float32
        assert vec[0] == 1.0


class TestBuildIndex:
    def test_builds_index_when_model_ready(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        model = MagicMock()
        model.ready = True
        monkeypatch.setattr(cli, "EmbeddingModel", lambda model_path: model)
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            cli, "VaultIndex", lambda embedder: captured.setdefault("embedder", embedder)
        )

        cli._build_index(_config(tmp_path))

        model._load_model.assert_called_once()
        assert isinstance(captured["embedder"], cli._OnnxEmbedderAdapter)

    def test_warns_but_still_builds_when_model_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        model = MagicMock()
        model.ready = False
        monkeypatch.setattr(cli, "EmbeddingModel", lambda model_path: model)
        sentinel = object()
        monkeypatch.setattr(cli, "VaultIndex", lambda embedder: sentinel)

        result = cli._build_index(_config(tmp_path))

        model._load_model.assert_called_once()
        assert result is sentinel


class TestMain:
    def test_main_runs_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = _config(tmp_path)
        monkeypatch.setattr(cli, "TuringConfig", lambda: cfg)
        monkeypatch.setattr(cli, "setup_logging", lambda config: None)
        ran: dict[str, object] = {}
        monkeypatch.setattr(cli, "_run", lambda config: ran.setdefault("config", config))

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert ran["config"] is cfg

    def test_main_suppresses_keyboard_interrupt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli, "TuringConfig", lambda: _config(tmp_path))
        monkeypatch.setattr(cli, "setup_logging", lambda config: None)

        def _raise(config: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_run", _raise)

        # KeyboardInterrupt is swallowed; main still exits cleanly with 0.
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
