"""Local FS backend — работает без S3."""
from __future__ import annotations

import logging
import mimetypes
import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.storage.base import StorageObject, UploadResult

logger = logging.getLogger(__name__)


class LocalBackend:
    """Локальная FS-абстракция: 'bucket' — просто директория."""

    id = "local"
    kind = "local"

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.base_dir / key).resolve()
        if self.base_dir not in p.parents and p != self.base_dir:
            raise ValueError(f"Key вне storage: {key}")
        return p

    def available(self) -> bool:
        return True

    async def upload(
        self, local_path: str | Path, key: str | None = None,
        content_type: str | None = None,
    ) -> UploadResult:
        src = Path(local_path)
        if not src.exists():
            return UploadResult(
                key=key or "", size=0, success=False,
                error=f"Файл не найден: {src}",
            )
        k = key or src.name
        dst = self._path(k)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            return UploadResult(
                key=k, size=dst.stat().st_size, success=True,
                url=str(dst),
            )
        except Exception as e:
            return UploadResult(
                key=k, size=0, success=False, error=str(e),
            )

    async def download(
        self, key: str, local_path: str | Path,
    ) -> bool:
        src = self._path(key)
        if not src.exists():
            return False
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            return True
        except Exception:
            return False

    async def list(
        self, prefix: str = "", limit: int = 1000,
    ) -> list[StorageObject]:
        base = self._path(prefix) if prefix else self.base_dir
        if not base.exists():
            return []
        result: list[StorageObject] = []
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            rel = str(f.relative_to(self.base_dir)).replace("\\", "/")
            st = f.stat()
            ct, _ = mimetypes.guess_type(str(f))
            result.append(StorageObject(
                key=rel,
                size=st.st_size,
                last_modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                content_type=ct or "",
            ))
            if len(result) >= limit:
                break
        return result

    async def delete(self, key: str) -> bool:
        p = self._path(key)
        if not p.exists():
            return False
        try:
            p.unlink()
            return True
        except Exception:
            return False

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def presign(
        self, key: str, expires_sec: int = 3600,
    ) -> str:
        # Для локального — возвращаем file:// URL
        p = self._path(key)
        return f"file://{p}" if p.exists() else ""
