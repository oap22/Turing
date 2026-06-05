"""Tests for the ``turing-vault-watcher`` entry point (``turing.vault.cli``).

The daemon's poll loop in ``_run`` is an infinite ``time.sleep`` loop driven by
signals, so the unit-testable seams are the embedder adapter and the index
builder. Both are exercised here with the ONNX ``EmbeddingModel`` mocked so no
model weights are loaded.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from turing.memory.embeddings import EmbeddingModel
from turing.vault.cli import _build_index, _OnnxEmbedderAdapter


class TestOnnxEmbedderAdapter:
    def test_returns_zero_vector_when_model_not_ready(self):
        model = MagicMock()
        model.ready = False
        adapter = _OnnxEmbedderAdapter(model)

        vec = adapter.embed("anything")

        assert vec.shape == (EmbeddingModel.DIMENSION,)
        assert vec.dtype == np.float32
        assert not np.any(vec)  # all zeros
        model._embed_sync.assert_not_called()

    def test_delegates_to_model_when_ready(self):
        model = MagicMock()
        model.ready = True
        model._embed_sync.return_value = [0.5] * EmbeddingModel.DIMENSION
        adapter = _OnnxEmbedderAdapter(model)

        vec = adapter.embed("hello world")

        assert vec.dtype == np.float32
        assert vec.shape == (EmbeddingModel.DIMENSION,)
        assert np.allclose(vec, 0.5)
        model._embed_sync.assert_called_once_with("hello world")


class TestBuildIndex:
    def test_builds_index_with_loaded_model(self):
        config = MagicMock()
        config.embedding_model_path = "/models/minilm"

        fake_model = MagicMock()
        fake_model.ready = True

        with patch("turing.vault.cli.EmbeddingModel", return_value=fake_model) as model_cls:
            index = _build_index(config)

        model_cls.assert_called_once_with(model_path="/models/minilm")
        fake_model._load_model.assert_called_once()
        # The index wraps the adapter around our model.
        assert index.snapshot() == {}

    def test_warns_but_still_builds_when_model_unavailable(self):
        config = MagicMock()
        config.embedding_model_path = "/models/missing"

        fake_model = MagicMock()
        fake_model.ready = False

        with (
            patch("turing.vault.cli.EmbeddingModel", return_value=fake_model),
            patch("turing.vault.cli.logger") as mock_logger,
        ):
            index = _build_index(config)

        assert index is not None
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.args[0] == "vault_watcher.embedding_model_unavailable"
