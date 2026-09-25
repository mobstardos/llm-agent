"""Content-addressed storage для snapshots.

Blobs хранятся в PostgreSQL (BYTEA), сжатые через zstd.
Дедупликация: одинаковый контент — один blob, ref_count++.
"""
from __future__ import annotations

import logging
from typing import Any

from src.db.pool import DatabasePool
from src.journal.models import sha256_bytes

logger = logging.getLogger(__name__)

# Попробуем zstd, fallback на gzip
try:
    import zstandard as zstd
    _HAS_ZSTD = True
except ImportError:
    import gzip
    _HAS_ZSTD = False


def _compress(data: bytes) -> tuple[bytes, str]:
    if _HAS_ZSTD:
        cctx = zstd.ZstdCompressor(level=3)
        return cctx.compress(data), "zstd"
    return gzip.compress(data, compresslevel=6), "gzip"


def _decompress(data: bytes, compression: str) -> bytes:
    if compression == "zstd":
        if not _HAS_ZSTD:
            raise RuntimeError("zstd не установлен")
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data)
    if compression == "gzip":
        import gzip
        return gzip.decompress(data)
    return data


class BlobStore:
    """Content-addressed storage для файловых snapshots."""

    def __init__(self, pool: DatabasePool):
        self.pool = pool

    async def put(self, data: bytes) -> str:
        """Сохранить blob, вернуть hash. Идемпотентно (дедупликация)."""
        h = sha256_bytes(data)

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                # Уже есть?
                await cur.execute(
                    "SELECT ref_count FROM journal.blobs WHERE hash = %s",
                    (h,),
                )
                row = await cur.fetchone()
                if row:
                    await cur.execute(
                        "UPDATE journal.blobs SET ref_count = ref_count + 1, "
                        "last_used_at = now() WHERE hash = %s",
                        (h,),
                    )
                    return h

                # Сжать и сохранить
                compressed, compression = _compress(data)
                await cur.execute("""
                    INSERT INTO journal.blobs
                        (hash, compression, content, size_raw, size_compressed)
                    VALUES (%s, %s, %s, %s, %s)
                """, (h, compression, compressed, len(data), len(compressed)))

                logger.debug(
                    "Blob stored: %s (%d→%d bytes, %s)",
                    h[:12], len(data), len(compressed), compression,
                )
        return h

    async def get(self, hash_: str) -> bytes | None:
        """Получить содержимое по hash."""
        rows = await self.pool.execute(
            "SELECT content, compression FROM journal.blobs WHERE hash = %s",
            (hash_,),
        )
        if not rows:
            return None
        row = rows[0]
        try:
            return _decompress(bytes(row["content"]), row["compression"])
        except Exception as e:
            logger.warning("Decompress %s: %s", hash_[:12], e)
            return None

    async def get_text(self, hash_: str) -> str | None:
        data = await self.get(hash_)
        if data is None:
            return None
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:
            return None

    async def release(self, hash_: str, count: int = 1) -> int:
        """Уменьшить ref_count. Вернуть новый ref_count."""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE journal.blobs
                    SET ref_count = GREATEST(ref_count - %s, 0)
                    WHERE hash = %s
                    RETURNING ref_count
                """, (count, hash_))
                row = await cur.fetchone()
                return row["ref_count"] if row else 0

    async def purge_orphans(self) -> int:
        """Удалить blobs с ref_count = 0."""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM journal.blobs WHERE ref_count <= 0"
                )
                return cur.rowcount or 0

    async def stats(self) -> dict:
        rows = await self.pool.execute("""
            SELECT
                COUNT(*) AS count,
                COALESCE(SUM(size_raw), 0) AS total_raw,
                COALESCE(SUM(size_compressed), 0) AS total_compressed,
                COALESCE(AVG(
                    CASE WHEN size_raw > 0
                        THEN 100.0 * size_compressed / size_raw
                        ELSE 100.0
                    END
                ), 100) AS avg_ratio
            FROM journal.blobs
        """)
        r = rows[0] if rows else {}
        return {
            "count": r.get("count", 0),
            "total_raw_mb": round((r.get("total_raw", 0) or 0) / 1024 / 1024, 2),
            "total_compressed_mb": round((r.get("total_compressed", 0) or 0) / 1024 / 1024, 2),
            "avg_compression_ratio": round(float(r.get("avg_ratio", 100)), 1),
        }

    async def vacuum(self) -> int:
        """VACUUM ANALYZE для blobs."""
        try:
            async with self.pool.connection() as conn:
                await conn.set_autocommit(True)
                async with conn.cursor() as cur:
                    await cur.execute("VACUUM ANALYZE journal.blobs")
            return 0
        except Exception as e:
            logger.debug("Vacuum: %s", e)
            return 1
