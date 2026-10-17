"""Простой extractor для текстовых файлов."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from src.extraction.base import ExtractionResult

logger = logging.getLogger(__name__)


class TextExtractor:
    id = "text"
    version = "0.1.0"
    mime_types = ["text/*", "application/json", "application/x-yaml"]
    priority = 100

    def can_handle(self, path: Path, mime: str) -> bool:
        return mime.startswith("text/") or mime in (
            "application/json", "application/x-yaml", "application/xml",
        )

    def extract(
        self, path: Path, max_chars: int = 500000, **kwargs,
    ) -> ExtractionResult:
        t0 = time.perf_counter()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ExtractionResult(
                source=str(path), mime="text/*",
                extractor_id=self.id, extractor_version=self.version,
                text="", warnings=[f"Ошибка чтения: {e}"],
            )

        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True

        duration_ms = (time.perf_counter() - t0) * 1000
        return ExtractionResult(
            source=str(path), mime="text/*",
            extractor_id=self.id, extractor_version=self.version,
            text=text, chars=len(text),
            truncated=truncated, duration_ms=duration_ms,
        )
