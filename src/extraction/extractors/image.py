"""Image extractor: metadata + OCR + опционально Vision."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from src.extraction.base import ExtractionResult

logger = logging.getLogger(__name__)


class ImageMetadataExtractor:
    id = "image_metadata"
    version = "0.1.0"
    mime_types = ["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"]
    priority = 20

    def can_handle(self, path: Path, mime: str) -> bool:
        return mime.startswith("image/")

    def extract(self, path: Path, **kwargs) -> ExtractionResult:
        t0 = time.perf_counter()
        warnings: list[str] = []
        metadata: dict = {}

        try:
            from PIL import Image
            with Image.open(path) as img:
                metadata = {
                    "width": img.width,
                    "height": img.height,
                    "format": img.format,
                    "mode": img.mode,
                }
                # EXIF
                exif = img.getexif()
                if exif:
                    metadata["exif_keys"] = list(exif.keys())[:20]
        except ImportError:
            warnings.append("Pillow не установлен")
        except Exception as e:
            warnings.append(f"Ошибка чтения: {e}")

        duration_ms = (time.perf_counter() - t0) * 1000
        return ExtractionResult(
            source=str(path), mime="image/*",
            extractor_id=self.id, extractor_version=self.version,
            text=f"[Изображение: {metadata.get('width')}×"
                 f"{metadata.get('height')} {metadata.get('format')}]",
            metadata=metadata, warnings=warnings,
            duration_ms=duration_ms, confidence=0.3,
        )


class ImageOcrExtractor:
    id = "image_ocr"
    version = "0.1.0"
    mime_types = ["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"]
    priority = 50

    def can_handle(self, path: Path, mime: str) -> bool:
        return mime.startswith("image/")

    def extract(
        self, path: Path, lang: str = "rus+eng", **kwargs,
    ) -> ExtractionResult:
        t0 = time.perf_counter()
        warnings: list[str] = []

        try:
            from PIL import Image
            import pytesseract
        except ImportError:
            return ExtractionResult(
                source=str(path), mime="image/*",
                extractor_id=self.id, extractor_version=self.version,
                text="",
                warnings=["Pillow или pytesseract не установлен"],
                confidence=0.0,
            )

        try:
            with Image.open(path) as img:
                text = pytesseract.image_to_string(img, lang=lang)
        except Exception as e:
            return ExtractionResult(
                source=str(path), mime="image/*",
                extractor_id=self.id, extractor_version=self.version,
                text="", warnings=[f"OCR: {e}"], confidence=0.0,
            )

        duration_ms = (time.perf_counter() - t0) * 1000
        return ExtractionResult(
            source=str(path), mime="image/*",
            extractor_id=self.id, extractor_version=self.version,
            text=text.strip(), chars=len(text.strip()),
            metadata={"method": "ocr", "lang": lang},
            warnings=warnings, duration_ms=duration_ms,
            confidence=0.6,
        )


class ImageVisionExtractor:
    """Описание через Vision-провайдера (capability vision)."""

    id = "image_vision"
    version = "0.1.0"
    mime_types = ["image/png", "image/jpeg", "image/webp"]
    priority = 100

    def can_handle(self, path: Path, mime: str) -> bool:
        return mime in ("image/png", "image/jpeg", "image/webp")

    def extract(
        self, path: Path, vision_provider=None, question: str = "",
        **kwargs,
    ) -> ExtractionResult:
        t0 = time.perf_counter()
        if vision_provider is None:
            return ExtractionResult(
                source=str(path), mime="image/*",
                extractor_id=self.id, extractor_version=self.version,
                text="", warnings=["Vision-провайдер недоступен"],
                confidence=0.0,
            )

        # Реальный вызов делается через capability resolver
        try:
            text = vision_provider.describe_sync(str(path), question)
        except Exception as e:
            return ExtractionResult(
                source=str(path), mime="image/*",
                extractor_id=self.id, extractor_version=self.version,
                text="", warnings=[f"Vision: {e}"], confidence=0.0,
            )

        duration_ms = (time.perf_counter() - t0) * 1000
        return ExtractionResult(
            source=str(path), mime="image/*",
            extractor_id=self.id, extractor_version=self.version,
            text=text, chars=len(text),
            metadata={"method": "vision"},
            duration_ms=duration_ms, confidence=0.9,
        )
