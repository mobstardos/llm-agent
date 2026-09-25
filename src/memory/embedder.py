"""Embedding-модель (bge-m3, локально)."""
from __future__ import annotations

import logging

from src.memory.config import EmbedderSettings

logger = logging.getLogger(__name__)


class Embedder:
    def __init__(self, cfg: EmbedderSettings):
        self.cfg = cfg
        self._model = None
        self._dim: int | None = None

    def _load(self):
        if self._model is not None:
            return self._model
        from sentence_transformers import SentenceTransformer
        logger.info("Загружаю embedder: %s", self.cfg.model)
        self._model = SentenceTransformer(
            self.cfg.model,
            cache_folder=self.cfg.cache_dir,
            device=self.cfg.device,
        )
        self._dim = self._model.get_sentence_embedding_dimension()
        logger.info("Embedder готов, dim=%d", self._dim or 0)
        return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._load()
        return self._dim or 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vectors = model.encode(
            texts,
            batch_size=self.cfg.batch_size,
            max_length=self.cfg.max_length,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
