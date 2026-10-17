"""MCP-сервер: медиа (аудио/видео)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("media-mcp")

_whisper = None
_whisper_available_cache = None


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    val = os.getenv("PROJECT_ROOT", "").strip()
    return Path(val or os.getcwd()).resolve()


def _safe(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


def _get_whisper():
    global _whisper
    if _whisper is None:
        from src.media.whisper_runner import WhisperRunner
        model = os.getenv("WHISPER_MODEL", "base")
        device = os.getenv("WHISPER_DEVICE", "cpu")
        compute = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        _whisper = WhisperRunner(
            model=model, device=device, compute_type=compute,
        )
    return _whisper


app = Server("media")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="metadata",
             description="Метаданные аудио/видео.",
             inputSchema={
                 "type": "object",
                 "properties": {"path": {"type": "string"}},
                 "required": ["path"],
             }),
        Tool(name="transcribe",
             description="Транскрибировать аудио/видео (Whisper).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "lang": {"type": "string"},
                     "model": {"type": "string"},
                 },
                 "required": ["path"],
             }),
        Tool(name="translate",
             description="Перевести аудио на английский (Whisper).",
             inputSchema={
                 "type": "object",
                 "properties": {"path": {"type": "string"}},
                 "required": ["path"],
             }),
        Tool(name="extract_audio",
             description="Извлечь аудио из видео в WAV.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "output": {"type": "string"},
                 },
                 "required": ["path"],
             }),
        Tool(name="extract_frames",
             description="Извлечь ключевые кадры из видео.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "output_dir": {"type": "string"},
                     "count": {"type": "integer", "default": 5},
                     "interval_sec": {"type": "number"},
                     "width": {"type": "integer", "default": 640},
                 },
                 "required": ["path"],
             }),
        Tool(name="clip",
             description="Обрезать медиа по времени.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "output": {"type": "string"},
                     "start_sec": {"type": "number"},
                     "end_sec": {"type": "number"},
                     "re_encode": {"type": "boolean", "default": False},
                 },
                 "required": ["path", "output", "start_sec", "end_sec"],
             }),
        Tool(name="convert",
             description="Конвертировать медиа.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "output": {"type": "string"},
                     "quality": {"type": "string", "default": "medium"},
                 },
                 "required": ["path", "output"],
             }),
        Tool(name="merge_audio_video",
             description="Заменить аудио у видео.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "video_path": {"type": "string"},
                     "audio_path": {"type": "string"},
                     "output": {"type": "string"},
                 },
                 "required": ["video_path", "audio_path", "output"],
             }),
        Tool(name="media_info",
             description="Информация о доступных провайдерах.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="transcription_cache_stats",
             description="Статистика кэша транскрипций.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _whisper_available_cache

    try:
        if name == "media_info":
            from src.media.ffmpeg_utils import (
                ffmpeg_available, ffprobe_available,
            )
            w = _get_whisper()
            return [TextContent(
                type="text",
                text=json.dumps({
                    "ffmpeg": ffmpeg_available(),
                    "ffprobe": ffprobe_available(),
                    "whisper": w.available(),
                    "whisper_model": w.model_name,
                    "whisper_device": w.device,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "transcription_cache_stats":
            cache_dir = Path(_get_root()) / "data" / "whisper_cache"
            stats = {"cache_dir": str(cache_dir), "exists": cache_dir.exists()}
            if cache_dir.exists():
                size = sum(
                    f.stat().st_size for f in cache_dir.rglob("*")
                    if f.is_file()
                )
                stats["size_bytes"] = size
            return [TextContent(
                type="text",
                text=json.dumps(stats, ensure_ascii=False, indent=2),
            )]

        p = _safe(arguments["path"])
        if not p.exists():
            return [TextContent(type="text", text=f"Файл не найден: {p}")]

        if name == "metadata":
            from src.media.ffmpeg_utils import get_metadata
            meta = await get_metadata(p)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "duration_sec": round(meta.duration_sec, 2),
                    "size_bytes": meta.size_bytes,
                    "format": meta.format_name,
                    "video": {
                        "codec": meta.video_codec,
                        "width": meta.video_width,
                        "height": meta.video_height,
                        "fps": round(meta.video_fps, 2),
                    } if meta.video_codec else None,
                    "audio": {
                        "codec": meta.audio_codec,
                        "sample_rate": meta.audio_sample_rate,
                        "channels": meta.audio_channels,
                    } if meta.audio_codec else None,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "transcribe":
            # Если видео — сначала извлекаем аудио
            audio_path = p
            if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".avi"):
                from src.media.ffmpeg_utils import extract_audio
                audio_path = p.parent / f"{p.stem}_audio.wav"
                ok, err = await extract_audio(p, audio_path)
                if not ok:
                    return [TextContent(
                        type="text",
                        text=f"Не удалось извлечь аудио: {err}",
                    )]

            w = _get_whisper()
            model_override = arguments.get("model")
            if model_override and model_override != w.model_name:
                from src.media.whisper_runner import WhisperRunner
                w = WhisperRunner(model=model_override, device=w.device)

            transcript = await w.transcribe(
                audio_path, lang=arguments.get("lang"),
            )
            if transcript.error:
                return [TextContent(
                    type="text", text=f"Ошибка: {transcript.error}",
                )]
            return [TextContent(
                type="text",
                text=json.dumps({
                    "text": transcript.text[:50000],
                    "language": transcript.language,
                    "duration_sec": round(transcript.duration, 2),
                    "model": transcript.model,
                    "duration_ms": round(transcript.duration_ms, 1),
                    "segments_count": len(transcript.segments),
                    "segments_sample": [
                        {"start": round(s.start, 2),
                         "end": round(s.end, 2), "text": s.text}
                        for s in transcript.segments[:20]
                    ],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "translate":
            w = _get_whisper()
            # Если видео — извлекаем аудио
            audio_path = p
            if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".avi"):
                from src.media.ffmpeg_utils import extract_audio
                audio_path = p.parent / f"{p.stem}_audio.wav"
                ok, err = await extract_audio(p, audio_path)
                if not ok:
                    return [TextContent(
                        type="text", text=f"Ошибка извлечения: {err}",
                    )]

            transcript = await w.translate(audio_path)
            if transcript.error:
                return [TextContent(
                    type="text", text=f"Ошибка: {transcript.error}",
                )]
            return [TextContent(
                type="text",
                text=json.dumps({
                    "text": transcript.text[:50000],
                    "language": transcript.language,
                    "duration_ms": round(transcript.duration_ms, 1),
                }, ensure_ascii=False, indent=2),
            )]

        if name == "extract_audio":
            out = arguments.get("output")
            if out:
                out_path = _safe(out)
            else:
                out_path = p.parent / f"{p.stem}.wav"
            from src.media.ffmpeg_utils import extract_audio
            ok, result = await extract_audio(p, out_path)
            return [TextContent(
                type="text",
                text=result if ok else f"Ошибка: {result}",
            )]

        if name == "extract_frames":
            out_dir = arguments.get("output_dir")
            if out_dir:
                out_path = _safe(out_dir)
            else:
                out_path = p.parent / f"{p.stem}_frames"
            from src.media.ffmpeg_utils import extract_frames
            frames = await extract_frames(
                p, out_path,
                count=arguments.get("count", 5),
                interval_sec=arguments.get("interval_sec"),
                width=arguments.get("width", 640),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "frames_count": len(frames),
                    "frames": frames,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "clip":
            out = _safe(arguments["output"])
            from src.media.ffmpeg_utils import clip
            ok, result = await clip(
                p, out,
                arguments["start_sec"], arguments["end_sec"],
                arguments.get("re_encode", False),
            )
            return [TextContent(
                type="text",
                text=result if ok else f"Ошибка: {result}",
            )]

        if name == "convert":
            out = _safe(arguments["output"])
            from src.media.ffmpeg_utils import convert_format
            ok, result = await convert_format(
                p, out, arguments.get("quality", "medium"),
            )
            return [TextContent(
                type="text",
                text=result if ok else f"Ошибка: {result}",
            )]

        if name == "merge_audio_video":
            v = _safe(arguments["video_path"])
            a = _safe(arguments["audio_path"])
            out = _safe(arguments["output"])
            from src.media.ffmpeg_utils import merge_audio_video
            ok, result = await merge_audio_video(v, a, out)
            return [TextContent(
                type="text",
                text=result if ok else f"Ошибка: {result}",
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Media tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
