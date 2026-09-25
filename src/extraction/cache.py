"""SQLite-кэш extraction-результатов."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path

from src.extraction.base import ExtractionResult

logger = logging.getLogger(__name__)


class ExtractionCache:
    def __init__(self, path: str | Path, ttl_days: int = 30):
        self.path = Path(path)
        self.ttl_days = ttl_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS extraction_cache (
                    file_hash TEXT NOT NULL,
                    extractor_id TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    use_count INTEGER DEFAULT 0,
                    PRIMARY KEY (file_hash, extractor_id, extractor_version)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_created "
                "ON extraction_cache(created_at)"
            )

    @staticmethod
    def file_hash(path: Path) -> str:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                while chunk := f.read(65536):
                    h.update(chunk)
        except Exception:
            return ""
        return h.hexdigest()

    def get(
        self, path: Path, extractor_id: str, extractor_version: str,
    ) -> ExtractionResult | None:
        fh = self.file_hash(path)
        if not fh:
            return None
        try:
            with sqlite3.connect(self.path) as conn:
                row = conn.execute(
                    "SELECT result_json, created_at FROM extraction_cache "
                    "WHERE file_hash = ? AND extractor_id = ? "
                    "AND extractor_version = ?",
                    (fh, extractor_id, extractor_version),
                ).fetchone()
                if not row:
                    return None
                result_json, created_at = row
                if time.time() - created_at > self.ttl_days * 86400:
                    conn.execute(
                        "DELETE FROM extraction_cache WHERE file_hash = ? "
                        "AND extractor_id = ? AND extractor_version = ?",
                        (fh, extractor_id, extractor_version),
                    )
                    return None
                conn.execute(
                    "UPDATE extraction_cache SET last_used_at = ?, "
                    "use_count = use_count + 1 "
                    "WHERE file_hash = ? AND extractor_id = ? "
                    "AND extractor_version = ?",
                    (time.time(), fh, extractor_id, extractor_version),
                )
            return ExtractionResult.from_dict(json.loads(result_json))
        except Exception as e:
            logger.warning("Cache get: %s", e)
            return None

    def set(
        self, path: Path, result: ExtractionResult,
    ) -> None:
        fh = self.file_hash(path)
        if not fh:
            return
        try:
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO extraction_cache "
                    "(file_hash, extractor_id, extractor_version, "
                    "result_json, created_at, last_used_at, use_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (
                        fh, result.extractor_id, result.extractor_version,
                        json.dumps(result.to_dict(), ensure_ascii=False),
                        time.time(), time.time(),
                    ),
                )
        except Exception as e:
            logger.warning("Cache set: %s", e)

    def cleanup(self) -> int:
        cutoff = time.time() - self.ttl_days * 86400
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "DELETE FROM extraction_cache WHERE created_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0

    def stats(self) -> dict:
        try:
            with sqlite3.connect(self.path) as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM extraction_cache"
                ).fetchone()[0]
                by_extractor = dict(conn.execute(
                    "SELECT extractor_id, COUNT(*) FROM extraction_cache "
                    "GROUP BY extractor_id"
                ).fetchall())
            return {
                "total": total,
                "by_extractor": by_extractor,
                "ttl_days": self.ttl_days,
            }
        except Exception as e:
            return {"error": str(e)}
