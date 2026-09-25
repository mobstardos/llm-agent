"""PDF extractor: text → ocr fallback."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from src.extraction.base import ExtractionResult

logger = logging.getLogger(__name__)


class PdfTextExtractor:
    id = "pdf_text"
    version = "0.1.0"
    mime_types = ["application/pdf"]
    priority = 100

    def can_handle(self, path: Path, mime: str) -> bool:
        return mime == "application/pdf"

    def extract(
        self, path: Path,
        max_pages: int = 500, max_chars: int = 100000, **kwargs,
    ) -> ExtractionResult:
        t0 = time.perf_counter()
        warnings: list[str] = []
        text_parts: list[str] = []
        tables: list[list[list]] = []
        pages = 0
        truncated = False

        try:
            import pdfplumber
        except ImportError:
            return ExtractionResult(
                source=str(path), mime="application/pdf",
                extractor_id=self.id, extractor_version=self.version,
                text="", warnings=["pdfplumber не установлен"],
            )

        try:
            with pdfplumber.open(path) as pdf:
                pages = len(pdf.pages)
                for i, page in enumerate(pdf.pages):
                    if i >= max_pages:
                        truncated = True
                        warnings.append(f"Обработано только {max_pages} стр.")
                        break
                    try:
                        t = page.extract_text() or ""
                        text_parts.append(t)
                        for tbl in (page.extract_tables() or []):
                            tables.append(tbl)
                    except Exception as e:
                        warnings.append(f"Страница {i+1}: {e}")
        except Exception as e:
            return ExtractionResult(
                source=str(path), mime="application/pdf",
                extractor_id=self.id, extractor_version=self.version,
                text="", warnings=[f"Ошибка чтения: {e}"],
            )

        text = "\n\n".join(text_parts)
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
            warnings.append(f"Обрезано до {max_chars} символов")

        duration_ms = (time.perf_counter() - t0) * 1000
        return ExtractionResult(
            source=str(path), mime="application/pdf",
            extractor_id=self.id, extractor_version=self.version,
            text=text, pages=pages, chars=len(text),
            tables=tables[:50],
            metadata={"pages": pages},
            truncated=truncated, warnings=warnings,
            duration_ms=duration_ms,
            confidence=1.0 if len(text) > 500 else 0.3,
        )


class PdfOcrExtractor:
    id = "pdf_ocr"
    version = "0.1.0"
    mime_types = ["application/pdf"]
    priority = 50

    def can_handle(self, path: Path, mime: str) -> bool:
        return mime == "application/pdf"

    def extract(
        self, path: Path,
        max_pages: int = 50, max_chars: int = 100000,
        lang: str = "rus+eng", **kwargs,
    ) -> ExtractionResult:
        t0 = time.perf_counter()
        warnings: list[str] = []

        try:
            from pdf2image import convert_from_path
            import pytesseract
        except ImportError:
            return ExtractionResult(
                source=str(path), mime="application/pdf",
                extractor_id=self.id, extractor_version=self.version,
                text="",
                warnings=["pdf2image или pytesseract не установлен"],
                confidence=0.0,
            )

        try:
            images = convert_from_path(
                str(path), dpi=200, first_page=1, last_page=max_pages,
            )
        except Exception as e:
            return ExtractionResult(
                source=str(path), mime="application/pdf",
                extractor_id=self.id, extractor_version=self.version,
                text="", warnings=[f"pdf2image: {e}"], confidence=0.0,
            )

        text_parts: list[str] = []
        for i, img in enumerate(images):
            try:
                t = pytesseract.image_to_string(img, lang=lang)
                text_parts.append(t)
            except Exception as e:
                warnings.append(f"OCR страница {i+1}: {e}")

        text = "\n\n".join(text_parts)
        if len(text) > max_chars:
            text = text[:max_chars]

        duration_ms = (time.perf_counter() - t0) * 1000
        return ExtractionResult(
            source=str(path), mime="application/pdf",
            extractor_id=self.id, extractor_version=self.version,
            text=text, pages=len(images), chars=len(text),
            metadata={"method": "ocr", "lang": lang},
            warnings=warnings, duration_ms=duration_ms,
            confidence=0.6,
        )
