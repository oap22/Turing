"""Embedding model wrapper for semantic search using ONNX Runtime."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingModel:
    """Wrapper for all-MiniLM-L6-v2 embeddings using ONNX Runtime.

    The model produces 384-dimensional normalized embeddings suitable for
    cosine similarity.  Inference runs in a thread executor so it does not
    block the async event loop.

    If the ONNX model or required packages are not available the class
    degrades gracefully — ``embed`` returns a zero vector and a warning is
    logged once.
    """

    DIMENSION: int = 384

    def __init__(self, model_path: str | Path | None = None) -> None:
        self._model_path = Path(model_path) if model_path else None
        self._session: "onnxruntime.InferenceSession | None" = None  # type: ignore[name-defined]
        self._tokenizer: "tokenizers.Tokenizer | None" = None  # type: ignore[name-defined]
        self._ready: bool = False
        self._warned: bool = False

    # ── lifecycle ──────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Load the model and tokenizer in a background thread."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load_model)

    def _load_model(self) -> None:
        """Synchronously load the ONNX session and HuggingFace tokenizer."""
        try:
            from tokenizers import Tokenizer  # type: ignore[import-untyped]
            import onnxruntime as ort  # type: ignore[import-untyped]
        except ImportError:
            logger.warning(
                "onnxruntime or tokenizers not installed. Vector search disabled."
            )
            return

        model_path = self._model_path
        if model_path is None or not model_path.exists():
            logger.warning(
                "Embedding model not found at %s. Vector search disabled.",
                model_path,
            )
            return

        onnx_file = model_path / "model.onnx"
        tokenizer_file = model_path / "tokenizer.json"

        if not onnx_file.exists():
            # Try the optimized variant
            onnx_file = model_path / "model_optimized.onnx"

        if not onnx_file.exists() or not tokenizer_file.exists():
            logger.warning(
                "Required model files (model.onnx, tokenizer.json) missing in %s. "
                "Vector search disabled.",
                model_path,
            )
            return

        sess_options = ort.SessionOptions()
        sess_options.inter_op_num_threads = 1
        sess_options.intra_op_num_threads = 2
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            str(onnx_file),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(str(tokenizer_file))
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", length=128)
        self._tokenizer.enable_truncation(max_length=128)
        self._ready = True
        logger.info("Embedding model loaded from %s", model_path)

    # ── public API ─────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """Whether the model is loaded and operational."""
        return self._ready

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string.

        Returns a 384-dimensional normalised vector, or a zero vector if
        the model is unavailable.
        """
        if not self._ready:
            self._warn_once()
            return [0.0] * self.DIMENSION
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._embed_sync, text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in a single call.

        Returns a list of 384-dimensional normalised vectors.
        """
        if not self._ready:
            self._warn_once()
            return [[0.0] * self.DIMENSION for _ in texts]
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._embed_batch_sync, texts)

    # ── synchronous inference ─────────────────────────────────────────

    def _embed_sync(self, text: str) -> list[float]:
        """Run single-text embedding inference (blocking)."""
        assert self._tokenizer is not None
        assert self._session is not None

        encoded = self._tokenizer.encode(text)
        input_ids = np.array([encoded.ids], dtype=np.int64)
        attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

        feeds = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }

        # Some ONNX exports only have input_ids + attention_mask
        input_names = {inp.name for inp in self._session.get_inputs()}
        feeds = {k: v for k, v in feeds.items() if k in input_names}

        outputs = self._session.run(None, feeds)

        # outputs[0] shape: (1, seq_len, hidden_dim)
        token_embeddings = outputs[0]

        # Mean pooling with attention mask
        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        summed = np.sum(token_embeddings * mask_expanded, axis=1)
        counts = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        pooled = summed / counts  # (1, hidden_dim)

        # L2 normalise
        norm = np.linalg.norm(pooled, axis=1, keepdims=True)
        norm = np.clip(norm, a_min=1e-9, a_max=None)
        normalised = pooled / norm

        return normalised[0].tolist()

    def _embed_batch_sync(self, texts: list[str]) -> list[list[float]]:
        """Run batched embedding inference (blocking)."""
        assert self._tokenizer is not None
        assert self._session is not None

        encoded_list = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encoded_list], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded_list], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

        feeds = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }

        input_names = {inp.name for inp in self._session.get_inputs()}
        feeds = {k: v for k, v in feeds.items() if k in input_names}

        outputs = self._session.run(None, feeds)

        token_embeddings = outputs[0]  # (batch, seq_len, hidden)

        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        summed = np.sum(token_embeddings * mask_expanded, axis=1)
        counts = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        pooled = summed / counts  # (batch, hidden)

        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-9, a_max=None)
        normalised = pooled / norms

        return normalised.tolist()

    # ── internal helpers ──────────────────────────────────────────────

    def _warn_once(self) -> None:
        if not self._warned:
            logger.warning("Embedding model not available — returning zero vectors.")
            self._warned = True
