"""Tests for EmbeddingModel — onnxruntime and tokenizers are mocked.

The goal is to cover caching/batching/error-handling logic, not to validate
the actual model. All ONNX/tokenizer interactions are stubbed so tests run
fast and have no external dependencies.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from turing.memory.embeddings import EmbeddingModel

# ── helpers ──────────────────────────────────────────────────────────


class _FakeEncoded:
    def __init__(self, ids: list[int], mask: list[int]) -> None:
        self.ids = ids
        self.attention_mask = mask


class _FakeTokenizer:
    """Stand-in for tokenizers.Tokenizer."""

    def __init__(self) -> None:
        self.padding_enabled = False
        self.truncation_enabled = False

    def enable_padding(self, **kwargs: Any) -> None:
        self.padding_enabled = True

    def enable_truncation(self, **kwargs: Any) -> None:
        self.truncation_enabled = True

    def encode(self, text: str) -> _FakeEncoded:
        # Length 4 to match a fake hidden dim
        return _FakeEncoded(ids=[1, 2, 3, 0], mask=[1, 1, 1, 0])

    def encode_batch(self, texts: list[str]) -> list[_FakeEncoded]:
        return [self.encode(t) for t in texts]


class _FakeInput:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeSession:
    """Stand-in for onnxruntime.InferenceSession."""

    def __init__(self, input_names: tuple[str, ...] = ("input_ids", "attention_mask")) -> None:
        self._inputs = [_FakeInput(n) for n in input_names]
        self.run_calls: list[dict[str, Any]] = []

    def get_inputs(self) -> list[_FakeInput]:
        return self._inputs

    def run(self, outputs: Any, feeds: dict[str, Any]) -> list[np.ndarray]:
        self.run_calls.append(feeds)
        batch = feeds["input_ids"].shape[0]
        seq = feeds["input_ids"].shape[1]
        hidden = 384
        # Deterministic non-zero output so the normaliser produces unit vectors.
        return [np.ones((batch, seq, hidden), dtype=np.float32)]


def _install_fake_onnx(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> MagicMock:
    """Install fake onnxruntime + tokenizers modules in sys.modules."""
    ort = types.ModuleType("onnxruntime")

    class _SessOptions:
        def __init__(self) -> None:
            self.inter_op_num_threads = 0
            self.intra_op_num_threads = 0
            self.graph_optimization_level = 0

    class _GraphOptLevel:
        ORT_ENABLE_ALL = 99

    inference_factory = MagicMock(return_value=session)
    ort.SessionOptions = _SessOptions  # type: ignore[attr-defined]
    ort.GraphOptimizationLevel = _GraphOptLevel  # type: ignore[attr-defined]
    ort.InferenceSession = inference_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)

    tokenizers_mod = types.ModuleType("tokenizers")

    class _Tok:
        @staticmethod
        def from_file(path: str) -> _FakeTokenizer:
            return _FakeTokenizer()

    tokenizers_mod.Tokenizer = _Tok  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tokenizers", tokenizers_mod)
    return inference_factory


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    """Create a directory with empty model.onnx + tokenizer.json files."""
    (tmp_path / "model.onnx").write_bytes(b"")
    (tmp_path / "tokenizer.json").write_text("{}")
    return tmp_path


# ── lifecycle / load_model ──────────────────────────────────────────


class TestLoadModel:
    @pytest.mark.asyncio
    async def test_initialize_success(
        self, monkeypatch: pytest.MonkeyPatch, model_dir: Path
    ) -> None:
        session = _FakeSession()
        factory = _install_fake_onnx(monkeypatch, session)

        m = EmbeddingModel(model_path=model_dir)
        await m.initialize()

        assert m.ready is True
        factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_initialize_no_path(self) -> None:
        m = EmbeddingModel(model_path=None)
        await m.initialize()
        assert m.ready is False

    @pytest.mark.asyncio
    async def test_initialize_missing_directory(self, tmp_path: Path) -> None:
        m = EmbeddingModel(model_path=tmp_path / "nope")
        await m.initialize()
        assert m.ready is False

    @pytest.mark.asyncio
    async def test_initialize_missing_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_fake_onnx(monkeypatch, _FakeSession())
        # Directory exists but no model.onnx or tokenizer.json
        m = EmbeddingModel(model_path=tmp_path)
        await m.initialize()
        assert m.ready is False

    @pytest.mark.asyncio
    async def test_initialize_uses_optimized_variant(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Only the optimised variant is present
        (tmp_path / "model_optimized.onnx").write_bytes(b"")
        (tmp_path / "tokenizer.json").write_text("{}")
        session = _FakeSession()
        factory = _install_fake_onnx(monkeypatch, session)

        m = EmbeddingModel(model_path=tmp_path)
        await m.initialize()

        assert m.ready is True
        # Confirm the optimised path was passed
        args, _ = factory.call_args
        assert "model_optimized.onnx" in args[0]

    @pytest.mark.asyncio
    async def test_initialize_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ensure import of onnxruntime fails inside _load_model
        monkeypatch.setitem(sys.modules, "onnxruntime", None)
        m = EmbeddingModel(model_path=Path("/whatever"))
        await m.initialize()
        assert m.ready is False


# ── embed / embed_batch when not ready ──────────────────────────────


class TestUnreadyFallback:
    @pytest.mark.asyncio
    async def test_embed_returns_zero_vector(self, caplog: pytest.LogCaptureFixture) -> None:
        m = EmbeddingModel()
        with caplog.at_level("WARNING"):
            vec = await m.embed("hello")
        assert vec == [0.0] * EmbeddingModel.DIMENSION
        assert any("not available" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_warn_only_once(self, caplog: pytest.LogCaptureFixture) -> None:
        m = EmbeddingModel()
        await m.embed("a")
        await m.embed("b")
        await m.embed("c")
        warnings = [r for r in caplog.records if "not available" in r.message]
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_embed_batch_returns_zero_vectors(self) -> None:
        m = EmbeddingModel()
        vecs = await m.embed_batch(["a", "b", "c"])
        assert len(vecs) == 3
        assert all(v == [0.0] * EmbeddingModel.DIMENSION for v in vecs)


# ── embed / embed_batch when ready ──────────────────────────────────


class TestInference:
    @pytest.mark.asyncio
    async def test_embed_single(self, monkeypatch: pytest.MonkeyPatch, model_dir: Path) -> None:
        session = _FakeSession()
        _install_fake_onnx(monkeypatch, session)
        m = EmbeddingModel(model_path=model_dir)
        await m.initialize()

        vec = await m.embed("hello world")

        assert len(vec) == EmbeddingModel.DIMENSION
        # Output is L2-normalised so its norm should be ~1
        assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5
        assert len(session.run_calls) == 1

    @pytest.mark.asyncio
    async def test_embed_batch(self, monkeypatch: pytest.MonkeyPatch, model_dir: Path) -> None:
        session = _FakeSession()
        _install_fake_onnx(monkeypatch, session)
        m = EmbeddingModel(model_path=model_dir)
        await m.initialize()

        vecs = await m.embed_batch(["one", "two", "three"])

        assert len(vecs) == 3
        assert all(len(v) == EmbeddingModel.DIMENSION for v in vecs)
        for v in vecs:
            assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5
        # One batched call
        assert len(session.run_calls) == 1
        assert session.run_calls[0]["input_ids"].shape[0] == 3

    @pytest.mark.asyncio
    async def test_feeds_filtered_to_session_inputs(
        self, monkeypatch: pytest.MonkeyPatch, model_dir: Path
    ) -> None:
        # Session only declares input_ids + attention_mask; token_type_ids
        # must be filtered out of feeds.
        session = _FakeSession(input_names=("input_ids", "attention_mask"))
        _install_fake_onnx(monkeypatch, session)
        m = EmbeddingModel(model_path=model_dir)
        await m.initialize()

        await m.embed("x")
        feeds = session.run_calls[0]
        assert "token_type_ids" not in feeds
        assert set(feeds) == {"input_ids", "attention_mask"}

    @pytest.mark.asyncio
    async def test_feeds_include_token_type_ids_when_supported(
        self, monkeypatch: pytest.MonkeyPatch, model_dir: Path
    ) -> None:
        session = _FakeSession(input_names=("input_ids", "attention_mask", "token_type_ids"))
        _install_fake_onnx(monkeypatch, session)
        m = EmbeddingModel(model_path=model_dir)
        await m.initialize()

        await m.embed_batch(["a", "b"])
        feeds = session.run_calls[0]
        assert "token_type_ids" in feeds

    @pytest.mark.asyncio
    async def test_inference_error_propagates(
        self, monkeypatch: pytest.MonkeyPatch, model_dir: Path
    ) -> None:
        session = _FakeSession()
        _install_fake_onnx(monkeypatch, session)
        m = EmbeddingModel(model_path=model_dir)
        await m.initialize()

        def boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("onnx kaboom")

        session.run = boom  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="kaboom"):
            await m.embed("hi")
