"""Embedder-фабрика для памяти агентов (Task 14).

Два бэкенда:
- model — bge-m3 через sentence-transformers (качественный, тяжёлый,
  первая загрузка качает модель);
- hash  — детерминированный feature-hashing на stdlib (мгновенный,
  без зависимостей). Качество ниже («мешок слов» + символьные n-граммы),
  но для дедупликации, грубой похожести и тестов — достаточно.

Выбор через env AGENT_MEMORY_EMBEDDER:
    auto  (по умолчанию) — попробовать model, при недоступности — hash;
    model                — только модель (ошибка, если пакет/модель недоступны);
    hash                 — только hashing (детерминизм, офлайн, CI).

Все бэкенды обязаны выдавать векторы размерности AGENT_MEMORY_DIM (1024),
совместимой с memory.tasks.embedding halfvec(1024) и VectorStore.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)

AGENT_MEMORY_DIM = 1024

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)


class MemoryEmbedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def embed_one(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Детерминированный embedding через feature hashing.

    Признаки: слова (вес 1.0) + символьные 4-граммы (вес 0.5).
    Хэш — hashlib.sha1 (стабилен между запусками, в отличие от builtin
    hash с PYTHONHASHSEED). Вектор L2-нормализован.
    """

    def __init__(self, dim: int = AGENT_MEMORY_DIM):
        self.dim = dim
        self.name = f"hash({dim})"

    def _features(self, text: str) -> dict[str, float]:
        feats: dict[str, float] = {}
        tokens = _TOKEN_RE.findall(text or "")
        for tok in tokens:
            low = tok.lower()
            feats[low] = feats.get(low, 0.0) + 1.0
            if len(low) > 4:
                for i in range(len(low) - 3):
                    g = low[i:i + 4]
                    feats["#" + g] = feats.get("#" + g, 0.0) + 0.5
        return feats

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = np.zeros(self.dim, dtype=np.float32)
            for feat, weight in self._features(text).items():
                digest = hashlib.sha1(feat.encode("utf-8")).digest()
                bucket = int.from_bytes(digest[:8], "big") % self.dim
                sign = 1.0 if digest[8] % 2 == 0 else -1.0
                vec[bucket] += sign * weight
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec /= norm
            out.append(vec.tolist())
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class ModelEmbedder:
    """Обёртка над src.memory.embedder.Embedder (bge-m3, локально)."""

    def __init__(self, dim: int = AGENT_MEMORY_DIM):
        from src.memory.config import EmbedderSettings
        from src.memory.embedder import Embedder
        self._inner = Embedder(EmbedderSettings())
        self.dim = dim
        self.name = "bge-m3"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)

    def embed_one(self, text: str) -> list[float]:
        return self._inner.embed_one(text)


def make_embedder(kind: str | None = None) -> MemoryEmbedder:
    """Фабрика: auto | model | hash (env AGENT_MEMORY_EMBEDDER)."""
    kind = (kind or os.getenv("AGENT_MEMORY_EMBEDDER", "auto")).strip().lower()
    if kind == "hash":
        return HashingEmbedder()
    if kind == "model":
        return ModelEmbedder()
    # auto: модель, если пакет установлен и грузится; иначе hash
    try:
        import sentence_transformers  # noqa: F401
        emb = ModelEmbedder()
        if emb.dim != AGENT_MEMORY_DIM:
            logger.warning(
                "Embedder %s даёт dim=%d (ожидалось %d) — "
                "векторный поиск может быть неточным",
                emb.name, emb.dim, AGENT_MEMORY_DIM,
            )
        return emb
    except Exception as e:
        logger.info("AGENT_MEMORY_EMBEDDER=auto: модель недоступна (%s) "
                    "→ hashing-embedder", e.__class__.__name__)
        return HashingEmbedder()
