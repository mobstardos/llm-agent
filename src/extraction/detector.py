"""Определение MIME-типа файла: расширение + magic bytes."""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

logger = logging.getLogger(__name__)

# Расширения → MIME (там, где mimetypes не хватает)
EXT_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/x-typescript",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}

# Magic bytes → MIME
MAGIC = [
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"RIFF", "audio/wav"),
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
    (b"OggS", "audio/ogg"),
]


def detect_mime(path: Path) -> str:
    """Определяет MIME-тип: расширение → magic bytes."""
    ext = path.suffix.lower()

    # 1. Сначала расширение
    if ext in EXT_MIME:
        return EXT_MIME[ext]

    # 2. Стандартный mimetypes
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime

    # 3. Magic bytes
    try:
        with open(path, "rb") as f:
            head = f.read(32)
        for magic, m in MAGIC:
            if head.startswith(magic):
                return m
    except Exception:
        pass

    return "application/octet-stream"


def is_textual(mime: str) -> bool:
    return (
        mime.startswith("text/")
        or mime in ("application/json", "application/x-yaml", "application/xml")
    )
