"""Audio extractor: транскрипция через Whisper."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from src.extraction.base import ExtractionResult

logger = logging.getLogger(__name__)


class AudioMetadataExtractor:
    id = "audio_metadata"
    version = "0.1.0"
    mime_types = ["audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4"]
    priority = 20

    def can_handle(self, path: Path, mime: str) -> bool:
        return mime.startswith("audio/")

    def extract(self, path: Path, **kwargs) -> ExtractionResult:
        t0 = time.perf_counter()
        metadata: dict = {}
        warnings: list[str] = []

        try:
            import wave
            with wave.open(str(path), "rb") as w:
                metadata = {
                    "channels": w.getnchannels(),
                    "framerate": w.getframerate(),
                    "frames": w.getnframes(),
                    "duration_sec": w.getnframes() / w.getframerate(),
                }
        except Exception:
            pass

        duration_ms = (time.perf_counter() - t0) * 1000
        return ExtractionResult(
            source=str(path), mime="audio/*",
            extractor_id=self.id, extractor_version=self.version,
            text="[Аудиофайл]",
            metadata=metadata, warnings=warnings,
            duration_ms=duration_ms, confidence=0.2,
        )


class AudioWhisperExtractor:
    id = "audio_whisper"
    version = "0.1.0"
    mime_types = ["audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4"]
    priority = 100

    def can_handle(self, path: Path, mime: str) -> bool:
        return mime.startswith("audio/")

    def extract(
        self, path: Path, model: str = "base", lang: str | None = None,
        **kwargs,
    ) -> ExtractionResult:
        t0 = time.perf_counter()
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return ExtractionResult(
                source=str(path), mime="audio/*",
                extractor_id=self.id, extractor_version=self.version,
                text="", warnings=["faster-whisper не установлен"],
                confidence=0.0,
            )

        try:
            wm = WhisperModel(model, device="cpu", compute_type="int8")
            segments, info = wm.transcribe(str(path), language=lang)
            text = " ".join(seg.text for seg in segments)
        except Exception as e:
            return ExtractionResult(
                source=str(path), mime="audio/*",
                extractor_id=self.id, extractor_version=self.version,
                text="", warnings=[f"Whisper: {e}"], confidence=0.0,
            )

        duration_ms = (time.perf_counter() - t0) * 1000
        return ExtractionResult(
            source=str(path), mime="audio/*",
            extractor_id=self.id, extractor_version=self.version,
            text=text.strip(), chars=len(text.strip()),
            metadata={"method": "whisper", "model": model},
            duration_ms=duration_ms, confidence=0.85,
        )
