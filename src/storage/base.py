"""Базовые типы и Protocol для storage backend."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass
class StorageObject:
    key: str
    size: int = 0
    last_modified: datetime | None = None
    etag: str = ""
    content_type: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "size": self.size,
            "last_modified": (
                self.last_modified.isoformat() if self.last_modified else None
            ),
            "etag": self.etag,
            "content_type": self.content_type,
            "metadata": self.metadata,
        }


@dataclass
class UploadResult:
    key: str
    size: int
    etag: str = ""
    version_id: str = ""
    url: str = ""
    success: bool = True
    error: str = ""


class StorageBackend(Protocol):
    id: str
    kind: str       # s3 | local

    async def upload(
        self, local_path: str | Path, key: str | None = None,
        content_type: str | None = None,
    ) -> UploadResult: ...

    async def download(
        self, key: str, local_path: str | Path,
    ) -> bool: ...

    async def list(
        self, prefix: str = "", limit: int = 1000,
    ) -> list[StorageObject]: ...

    async def delete(self, key: str) -> bool: ...

    async def exists(self, key: str) -> bool: ...

    async def presign(self, key: str, expires_sec: int = 3600) -> str: ...
