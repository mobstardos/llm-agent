"""Утилиты ffmpeg: метаданные, извлечение аудио/кадров, clip."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class MediaMetadata:
    duration_sec: float = 0.0
    size_bytes: int = 0
    format_name: str = ""
    format_long_name: str = ""
    bit_rate: int = 0
    video_codec: str = ""
    video_width: int = 0
    video_height: int = 0
    video_fps: float = 0.0
    audio_codec: str = ""
    audio_sample_rate: int = 0
    audio_channels: int = 0
    raw: dict = field(default_factory=dict)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


async def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", f"Timeout {timeout}s"
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except FileNotFoundError:
        return -1, "", "ffmpeg не найден"
    except Exception as e:
        return -1, "", str(e)


async def get_metadata(path: str | Path) -> MediaMetadata:
    """Извлекает метаданные через ffprobe."""
    if not ffprobe_available():
        return MediaMetadata(raw={"error": "ffprobe не установлен"})

    rc, stdout, stderr = await _run([
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ])

    meta = MediaMetadata()
    if rc != 0:
        meta.raw = {"error": stderr}
        return meta

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return meta

    fmt = data.get("format", {})
    meta.duration_sec = float(fmt.get("duration", 0) or 0)
    meta.size_bytes = int(fmt.get("size", 0) or 0)
    meta.format_name = fmt.get("format_name", "")
    meta.format_long_name = fmt.get("format_long_name", "")
    meta.bit_rate = int(fmt.get("bit_rate", 0) or 0)

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not meta.video_codec:
            meta.video_codec = stream.get("codec_name", "")
            meta.video_width = int(stream.get("width", 0) or 0)
            meta.video_height = int(stream.get("height", 0) or 0)
            fps_str = stream.get("r_frame_rate", "0/1")
            try:
                if "/" in fps_str:
                    num, den = fps_str.split("/")
                    meta.video_fps = float(num) / float(den) if float(den) else 0
                else:
                    meta.video_fps = float(fps_str)
            except (ValueError, ZeroDivisionError):
                pass
        elif stream.get("codec_type") == "audio" and not meta.audio_codec:
            meta.audio_codec = stream.get("codec_name", "")
            meta.audio_sample_rate = int(stream.get("sample_rate", 0) or 0)
            meta.audio_channels = int(stream.get("channels", 0) or 0)

    meta.raw = data
    return meta


async def extract_audio(
    video_path: str | Path,
    output_path: str | Path,
    sample_rate: int = 16000,
    mono: bool = True,
) -> tuple[bool, str]:
    """Извлекает аудио в WAV."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(sample_rate),
    ]
    if mono:
        cmd.extend(["-ac", "1"])
    cmd.append(str(out))

    rc, _, stderr = await _run(cmd)
    return rc == 0, stderr if rc != 0 else str(out)


async def extract_frames(
    video_path: str | Path,
    output_dir: str | Path,
    count: int = 5,
    interval_sec: float | None = None,
    width: int = 640,
) -> list[str]:
    """Извлекает ключевые кадры из видео."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = await get_metadata(video_path)
    duration = meta.duration_sec

    if duration <= 0:
        return []

    frames: list[str] = []

    if interval_sec:
        # Равномерно по времени
        times = []
        t = 0.0
        while t < duration and len(times) < count:
            times.append(t)
            t += interval_sec
    else:
        # Равномерно по count
        if count <= 1:
            times = [duration / 2]
        else:
            step = duration / (count + 1)
            times = [step * (i + 1) for i in range(count)]

    for i, t in enumerate(times):
        out = out_dir / f"frame_{i:03d}.jpg"
        rc, _, _ = await _run([
            "ffmpeg", "-y",
            "-ss", f"{t:.2f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", f"scale={width}:-1",
            str(out),
        ])
        if rc == 0 and out.exists():
            frames.append(str(out))

    return frames


async def clip(
    input_path: str | Path,
    output_path: str | Path,
    start_sec: float,
    end_sec: float,
    re_encode: bool = False,
) -> tuple[bool, str]:
    """Обрезает медиафайл."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    duration = end_sec - start_sec
    if duration <= 0:
        return False, "Некорректный диапазон"

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.3f}",
        "-i", str(input_path),
        "-t", f"{duration:.3f}",
    ]
    if not re_encode:
        cmd.extend(["-c", "copy"])
    cmd.append(str(out))

    rc, _, stderr = await _run(cmd)
    return rc == 0, stderr if rc != 0 else str(out)


async def convert_format(
    input_path: str | Path,
    output_path: str | Path,
    quality: str = "medium",
) -> tuple[bool, str]:
    """Конвертирует в другой формат (по расширению)."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    quality_map = {"low": "23", "medium": "20", "high": "17"}

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-crf", quality_map.get(quality, "20"),
        "-preset", "medium",
        "-c:a", "aac",
        "-b:a", "128k",
        str(out),
    ]
    rc, _, stderr = await _run(cmd)
    return rc == 0, stderr if rc != 0 else str(out)


async def merge_audio_video(
    video_path: str | Path,
    audio_path: str | Path,
    output_path: str | Path,
) -> tuple[bool, str]:
    """Заменяет аудиодорожку видео."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c:v", "copy",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        str(out),
    ]
    rc, _, stderr = await _run(cmd)
    return rc == 0, stderr if rc != 0 else str(out)
