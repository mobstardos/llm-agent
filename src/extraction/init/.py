"""Extraction — извлечение текста из файлов разных форматов."""
from src.extraction.pipeline import ExtractionPipeline
from src.extraction.detector import detect_mime

__all__ = ["ExtractionPipeline", "detect_mime"]
