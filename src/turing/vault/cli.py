"""``turing-vault-watcher`` entry point — the git-commit-driven reindex daemon.

ADR 0010 §6 gives the vault watcher its own systemd unit
(``turing-vault-watcher.service``). This module is its ``ExecStart`` target. It

1. loads config + the ONNX embedding model,
2. wraps the model in a sync :class:`~turing.vault.embedder.Embedder` adapter,
3. rebuilds the in-memory :class:`~turing.vault.index.VaultIndex` from the
   current ``HEAD`` (a cold start indexes the whole tree once),
4. then polls ``git HEAD`` on an interval, applying each new commit's diff to
   the index incrementally (:class:`~turing.vault.watcher.VaultWatcher`).

The watcher holds the index in memory; on restart it bootstraps from ``HEAD``
again. Persisting the cursor + serialised vectors across restarts is a later
optimisation (the cold rebuild is O(vault) but only on boot).
"""

from __future__ import annotations

import contextlib
import signal
import sys
import time
from typing import TYPE_CHECKING

import numpy as np
import structlog

from turing.config import TuringConfig
from turing.logging import setup_logging
from turing.memory.embeddings import EmbeddingModel
from turing.vault.index import VaultIndex
from turing.vault.watcher import VaultWatcher

if TYPE_CHECKING:
    from numpy.typing import NDArray

logger = structlog.get_logger(__name__)


class _OnnxEmbedderAdapter:
    """Adapts the async :class:`EmbeddingModel` to the sync ``Embedder`` protocol.

    The watcher loop is synchronous (it shells out to ``git``), so it needs a
    blocking ``embed``. ``EmbeddingModel`` already exposes a synchronous inner
    inference path; this wrapper drives it directly and converts the result to
    the ``float32`` numpy vector :class:`VaultIndex` stores. When the model is
    unavailable, ``embed`` returns a zero vector — vault_query simply yields no
    useful hits rather than crashing the daemon.
    """

    def __init__(self, model: EmbeddingModel) -> None:
        self._model = model

    def embed(self, text: str) -> NDArray[np.float32]:
        if not self._model.ready:
            return np.zeros(EmbeddingModel.DIMENSION, dtype=np.float32)
        return np.asarray(self._model._embed_sync(text), dtype=np.float32)


def _build_index(config: TuringConfig) -> VaultIndex:
    model = EmbeddingModel(model_path=config.embedding_model_path)
    # Synchronous load — the watcher daemon has no event loop of its own.
    model._load_model()
    if not model.ready:
        logger.warning(
            "vault_watcher.embedding_model_unavailable",
            model_path=str(config.embedding_model_path),
            detail="reindex will run but vault_query hits will be empty until the model loads",
        )
    return VaultIndex(embedder=_OnnxEmbedderAdapter(model))


def _run(config: TuringConfig) -> None:
    index = _build_index(config)
    watcher = VaultWatcher(vault_root=config.vault_root, index=index)

    logger.info(
        "vault_watcher.starting",
        vault_root=str(config.vault_root),
        poll_interval_s=config.vault_poll_interval_seconds,
    )

    # Cold start: catch up to whatever HEAD currently is (whole tree on a
    # fresh boot), then enter the poll loop.
    head = watcher.poll_once()
    logger.info("vault_watcher.cold_start_complete", head=head, indexed=len(index.snapshot()))

    stopping = False

    def _stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        logger.info("vault_watcher.stopping", signal=signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stopping:
        time.sleep(config.vault_poll_interval_seconds)
        if stopping:
            break
        try:
            new_head = watcher.poll_once()
        except Exception:
            # A daemon must not die on one bad poll (e.g. a transient git lock).
            logger.exception("vault_watcher.poll_failed")
            continue
        if new_head != head:
            logger.info(
                "vault_watcher.reindexed",
                head=new_head,
                indexed=len(index.snapshot()),
            )
            head = new_head


def main() -> None:
    """CLI entry point for the ``turing-vault-watcher`` command."""
    config = TuringConfig()
    setup_logging(config)
    with contextlib.suppress(KeyboardInterrupt):
        _run(config)
    sys.exit(0)


if __name__ == "__main__":
    main()
