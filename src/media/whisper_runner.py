"""Whisper: транскрипция через faster-whisper."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    text: str = ""
    language: str = ""
    duration: float = 0.0
    segments: list[TranscriptSegment] = field(default_factory=list)
    model: str = ""
    duration_ms: float = 0.0
    error: str = ""


class WhisperRunner:
    """Транскрипция через faster-whisper."""

    def __init__(
        self,
        model: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        cache_dir: str = "data/whisper_cache",
    ):
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.cache_dir = cache_dir
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        from faster_whisper import WhisperModel
        logger.info(
            "Загружаю Whisper %s (device=%s, compute=%s)",
            self.model_name, self.device, self.compute_type,
        )
        self._model = WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
            download_root=self.cache_dir,
        )
        return self._model

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    async def transcribe(
        self,
        audio_path: str | Path,
        lang: str | None = None,
        beam_size: int = 5,
    ) -> Transcript:
        """Транскрибирует аудио."""
        if not self.available():
            return Transcript(error="faster-whisper не установлен")

        p = Path(audio_path)
        if not p.exists():
            return Transcript(error=f"Файл не найден: {p}")

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._transcribe_sync, str(p), lang, beam_size,
        )

    def _transcribe_sync(
        self, path: str, lang: str | None, beam_size: int,
    ) -> Transcript:
        t0 = time.perf_counter()
        try:
            model = self._load()
            segments_iter, info = model.transcribe(
                path, language=lang, beam_size=beam_size,
            )

            segments: list[TranscriptSegment] = []
            text_parts: list[str] = []

            for seg in segments_iter:
                segments.append(TranscriptSegment(
                    start=seg.start, end=seg.end, text=seg.text.strip(),
                ))
                text_parts.append(seg.text.strip())

            duration_ms = (time.perf_counter() - t0) * 1000
            return Transcript(
                text=" ".join(text_parts),
                language=info.language,
                duration=info.duration,
                segments=segments,
                model=self.model_name,
                duration_ms=duration_ms,
            )
        except Exception as e:
            logger.exception("Whisper transcribe failed")
            return Transcript(
                error=str(e),
                model=self.model_name,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )

    async def translate(
        self, audio_path: str | Path, beam_size: int = 5,
    ) -> Transcript:
        """Переводит на английский."""
        if not self.available():
            return Transcript(error="faster-whisper не установлен")

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._translate_sync, str(audio_path), beam_size,
        )

    def _translate_sync(self, path: str, beam_size: int) -> Transcript:
        t0 = time.perf_counter()
        try:
            model = self._load()
            segments_iter, info = model.transcribe(
                path, task="translate", beam_size=beam_size,
            )
            segments: list[TranscriptSegment] = []
            text_parts: list[str] = []
            for seg in segments_iter:
                segments.append(TranscriptSegment(
                    start=seg.start, end=seg.end, text=seg.text.strip(),
                ))
                text_parts.append(seg.text.strip())
            return Transcript(
                text=" ".join(text_parts),
                language="en",
                duration=info.duration,
                segments=segments,
                model=self.model_name,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return Transcript(
                error=str(e),
                model=self.model_name,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
