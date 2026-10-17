"""Extraction Pipeline: выбор extractor + fallback chain + кэш."""
from __future__ import annotations

import logging
from pathlib import Path

from src.extraction.base import Extractor, ExtractionResult
from src.extraction.cache import ExtractionCache
from src.extraction.detector import detect_mime

logger = logging.getLogger(__name__)


class ExtractionPipeline:
    """Реестр extractors + последовательный fallback."""

    def __init__(
        self,
        cache: ExtractionCache | None = None,
        vision_provider=None,
    ):
        self.extractors: list[Extractor] = []
        self.cache = cache
        self.vision_provider = vision_provider
        self._register_builtin()

    def _register_builtin(self) -> None:
        """Регистрирует встроенные extractors."""
        try:
            from src.extraction.extractors.text import TextExtractor
            from src.extraction.extractors.pdf import (
                PdfOcrExtractor, PdfTextExtractor,
            )
            from src.extraction.extractors.docx import DocxExtractor
            from src.extraction.extractors.xlsx import XlsxExtractor
            from src.extraction.extractors.image import (
                ImageMetadataExtractor, ImageOcrExtractor, ImageVisionExtractor,
            )
            from src.extraction.extractors.audio import (
                AudioMetadataExtractor, AudioWhisperExtractor,
            )

            candidates = [
                PdfTextExtractor(), PdfOcrExtractor(),
                DocxExtractor(), XlsxExtractor(),
                ImageVisionExtractor(),
                ImageOcrExtractor(), ImageMetadataExtractor(),
                AudioWhisperExtractor(), AudioMetadataExtractor(),
                TextExtractor(),
            ]
            for e in candidates:
                self.register(e)
        except Exception as e:
            logger.warning("Ошибка регистрации extractors: %s", e)

    def register(self, extractor: Extractor) -> None:
        self.extractors.append(extractor)
        self.extractors.sort(key=lambda x: -x.priority)

    def available(self) -> list[dict]:
        return [
            {
                "id": e.id, "version": e.version,
                "mime_types": e.mime_types, "priority": e.priority,
            }
            for e in self.extractors
        ]

    # ═══════════════════════════════════════════════════════
    # Main
    # ═══════════════════════════════════════════════════════
    def extract(
        self,
        path: str | Path,
        *,
        use_cache: bool = True,
        min_chars: int = 100,
        max_chars: int = 100000,
        vision_question: str = "",
    ) -> ExtractionResult:
        p = Path(path).resolve()
        if not p.exists():
            return ExtractionResult(
                source=str(p), mime="unknown",
                extractor_id="none", extractor_version="0",
                text="", warnings=[f"Файл не существует: {p}"],
                confidence=0.0,
            )

        mime = detect_mime(p)

        # Выбираем подходящие extractors
        suitable = [e for e in self.extractors if e.can_handle(p, mime)]
        if not suitable:
            # Text fallback
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                return ExtractionResult(
                    source=str(p), mime=mime,
                    extractor_id="fallback_text",
                    extractor_version="0.1.0",
                    text=text[:max_chars], chars=min(len(text), max_chars),
                    warnings=["Нет специализированного extractor"],
                    confidence=0.3,
                )
            except Exception:
                return ExtractionResult(
                    source=str(p), mime=mime,
                    extractor_id="none", extractor_version="0",
                    text="", warnings=[f"Нет extractor для {mime}"],
                    confidence=0.0,
                )

        # Пробуем extractors по очереди
        results: list[ExtractionResult] = []
        for ext in suitable:
            # Кэш
            if use_cache and self.cache:
                cached = self.cache.get(p, ext.id, ext.version)
                if cached:
                    logger.debug("Cache hit: %s / %s", p.name, ext.id)
                    return cached

            # Извлечение
            kwargs = {
                "max_chars": max_chars,
                "vision_provider": self.vision_provider,
                "question": vision_question,
            }
            try:
                result = ext.extract(p, **kwargs)
            except Exception as e:
                logger.warning("Extractor %s упал: %s", ext.id, e)
                continue

            results.append(result)

            # Проверяем "достаточно ли"
            if result.chars >= min_chars and result.confidence >= 0.4:
                if use_cache and self.cache:
                    self.cache.set(p, result)
                return result

            logger.info(
                "Extractor %s: %d chars, confidence %.2f — пробуем следующий",
                ext.id, result.chars, result.confidence,
            )

        # Возвращаем лучшее из того, что получили
        if results:
            best = max(results, key=lambda r: (r.confidence, r.chars))
            if use_cache and self.cache:
                self.cache.set(p, best)
            return best

        return ExtractionResult(
            source=str(p), mime=mime,
            extractor_id="none", extractor_version="0",
            text="", warnings=["Все extractors не сработали"],
            confidence=0.0,
        )
