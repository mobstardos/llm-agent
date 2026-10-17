"""Retriever: комбинирует vector search + фильтры."""
from __future__ import annotations

import logging
import re

from src.memory.base import RetrievedChunk
from src.memory.vector import VectorMemory

logger = logging.getLogger(__name__)


class Retriever:
    def __init__(self, vector: VectorMemory):
        self.vector = vector

    def retrieve(
        self, query: str, top_k: int | None = None, context: str = "",
    ) -> list[RetrievedChunk]:
        if not query.strip():
            return []
        results = self.vector.search(query, top_k=top_k)
        symbols = self._extract_symbols(query)
        if symbols:
            for r in results:
                sym_str = r.metadata.get("symbols", "") or ""
                for sym in symbols:
                    if sym in sym_str:
                        r.score += 0.1
        results.sort(key=lambda x: -x.score)
        return results[:top_k or 10]

    def render_context(
        self, chunks: list[RetrievedChunk], max_chars: int = 8000,
    ) -> str:
        if not chunks:
            return ""
        parts: list[str] = []
        total = 0
        for c in chunks:
            header = f"### {c.metadata.get('file', '?')}"
            if c.metadata.get("symbols"):
                header += f" :: {c.metadata['symbols']}"
            header += (
                f" (строки {c.metadata.get('start_line')}-"
                f"{c.metadata.get('end_line')})"
            )
            chunk = (
                f"{header}\n\n"
                f"```{c.metadata.get('language', '')}\n"
                f"{c.text}\n"
                f"```\n"
            )
            parts.append(chunk)
            total += len(chunk)
            if total >= max_chars:
                break
        return "\n\n---\n\n".join(parts)

    def _extract_symbols(self, query: str) -> list[str]:
        """Достаёт идентификаторы (CamelCase / snake_case) из запроса."""
        return re.findall(r"\b(?:[A-Z][a-zA-Z0-9_]*|[a-z_]+_[a-z0-9_]+)\b", query)