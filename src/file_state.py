"""Хэш состояния файлов проекта для инвалидации кэша."""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_IGNORE = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".idea", ".vscode", "dist", "build", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".next", "target",
}


class FileState:
    def __init__(
        self, root: str | Path, ttl: float = 1.0,
        max_files: int = 50000, ignore: set[str] | None = None,
    ):
        self.root = Path(root).resolve()
        self.ttl = ttl
        self.max_files = max_files
        self.ignore = ignore or DEFAULT_IGNORE
        self._hash: str | None = None
        self._at: float = 0.0

    def hash(self) -> str:
        now = time.monotonic()
        if self._hash and now - self._at < self.ttl:
            return self._hash

        h = hashlib.sha256()
        count = 0
        try:
            for path in self.root.rglob("*"):
                if any(p in self.ignore for p in path.parts):
                    continue
                if not path.is_file():
                    continue
                count += 1
                if count > self.max_files:
                    h.update(b"|TRUNCATED|")
                    break
                try:
                    st = path.stat()
                except OSError:
                    continue
                rel = str(path.relative_to(self.root)).replace("\\", "/")
                h.update(f"{rel}|{int(st.st_mtime)}|{st.st_size}\n".encode())
        except Exception as e:
            logger.warning("FileState: %s", e)

        result = h.hexdigest()
        self._hash = result
        self._at = now
        return result

    def invalidate(self) -> None:
        self._hash = None
        self._at = 0.0
