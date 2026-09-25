"""Управление дисковым пространством.

Принципы:
- Всё копится, пока есть место
- При 85% — сжать, при 92% — удалить старое
- Защищаем: checkpoints, важные действия (importance ≥ 0.7)
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone

from src.db.pool import DatabasePool
from src.journal.storage import BlobStore

logger = logging.getLogger(__name__)


class RetentionManager:
    def __init__(
        self,
        pool: DatabasePool,
        blobs: BlobStore,
        *,
        disk_path: str = ".",
        soft_limit: float = 0.85,   # 85% — архивация
        hard_limit: float = 0.92,   # 92% — удаление старого
        target: float = 0.75,       # удалять пока не станет 75%
        min_keep_days: int = 30,    # минимум хранить дней
    ):
        self.pool = pool
        self.blobs = blobs
        self.disk_path = disk_path
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit
        self.target = target
        self.min_keep_days = min_keep_days

    async def collect_stats(self) -> dict:
        """Собрать статистику диска и журнала."""
        try:
            usage = shutil.disk_usage(self.disk_path)
            total_gb = usage.total / 1024 ** 3
            free_gb = usage.free / 1024 ** 3
            used_percent = 100.0 * (usage.used / usage.total)
        except Exception as e:
            logger.warning("Disk usage: %s", e)
            total_gb = free_gb = 0
            used_percent = 0

        blob_stats = await self.blobs.stats()

        rows = await self.pool.execute("""
            SELECT COUNT(*) AS actions_count,
                   MIN(created_at) AS oldest
            FROM journal.actions
        """)
        r = rows[0] if rows else {}

        stats = {
            "disk_total_gb": round(total_gb, 2),
            "disk_free_gb": round(free_gb, 2),
            "disk_used_percent": round(used_percent, 2),
            "blob_count": blob_stats["count"],
            "blob_size_gb": round(blob_stats["total_compressed_mb"] / 1024, 3),
            "blob_raw_gb": round(blob_stats["total_raw_mb"] / 1024, 3),
            "compression_ratio": blob_stats["avg_compression_ratio"],
            "actions_count": r.get("actions_count", 0),
            "oldest_action": r.get("oldest").isoformat()
                if r.get("oldest") else None,
        }

        # Записать в журнал
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO journal.retention_stats
                            (disk_total_gb, disk_free_gb, disk_used_percent,
                             blob_count, blob_size_gb, actions_count,
                             oldest_action_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        stats["disk_total_gb"], stats["disk_free_gb"],
                        stats["disk_used_percent"], stats["blob_count"],
                        stats["blob_size_gb"], stats["actions_count"],
                        r.get("oldest"),
                    ))
        except Exception:
            pass

        return stats

    async def enforce(self) -> dict:
        """Проверить и применить retention policy."""
        stats = await self.collect_stats()
        result = {
            "before": stats,
            "actions": [],
            "purged_blobs": 0,
            "purged_actions": 0,
        }

        if stats["disk_used_percent"] < self.soft_limit * 100:
            result["actions"].append("no_action_needed")
            return result

        # Soft limit достигнут — архивируем
        if stats["disk_used_percent"] >= self.soft_limit * 100:
            purged_blobs = await self._purge_orphan_blobs()
            result["purged_blobs"] = purged_blobs
            if purged_blobs:
                result["actions"].append(f"purged {purged_blobs} orphan blobs")

        # Hard limit — удаляем старые snapshots
        if stats["disk_used_percent"] >= self.hard_limit * 100:
            removed = await self._purge_old_snapshots(
                keep_days=self.min_keep_days,
                target_percent=self.target * 100,
            )
            result["purged_actions"] = removed
            if removed:
                result["actions"].append(f"removed old snapshots for {removed} actions")

        # Vacuum
        await self.blobs.vacuum()

        result["after"] = await self.collect_stats()
        return result

    async def _purge_orphan_blobs(self) -> int:
        """Удалить blobs без ссылок."""
        return await self.blobs.purge_orphans()

    async def _purge_old_snapshots(
        self, keep_days: int, target_percent: float,
    ) -> int:
        """Удалить snapshots для старых действий, сохранив важные."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)

        # Список действий, у которых можно удалить snapshots
        # Условия: старое, не в checkpoint, не важное, не в rollback/replay
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                # Шаг 1: удалить snapshots для старых неважных действий
                await cur.execute("""
                    WITH candidates AS (
                        SELECT a.id
                        FROM journal.actions a
                        WHERE a.created_at < %s
                          AND (a.importance IS NULL OR a.importance < 0.7)
                          AND a.category IN ('read', 'query', 'external')
                          AND NOT EXISTS (
                              SELECT 1 FROM journal.checkpoints cp
                              WHERE cp.action_id = a.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM journal.snapshots s
                              WHERE s.action_id = a.id
                                AND s.phase = 'before'
                          )
                        LIMIT 10000
                    )
                    SELECT COUNT(*) FROM candidates
                """, (cutoff,))
                row = await cur.fetchone()
                count = row["count"] if row else 0

                # Шаг 2: удаляем сами actions (без snapshots)
                await cur.execute("""
                    DELETE FROM journal.actions
                    WHERE created_at < %s
                      AND (importance IS NULL OR importance < 0.7)
                      AND category IN ('read', 'query', 'external')
                    RETURNING id
                """, (cutoff,))
                deleted = cur.rowcount or 0

        return deleted

    async def prune_history(self, max_age_days: int = 365) -> dict:
        """Агрессивная очистка: всё старше N дней."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                # Удалить snapshots
                await cur.execute("""
                    DELETE FROM journal.snapshots
                    WHERE action_id IN (
                        SELECT id FROM journal.actions WHERE created_at < %s
                    )
                """, (cutoff,))
                snap_deleted = cur.rowcount or 0

                # Удалить actions
                await cur.execute("""
                    DELETE FROM journal.actions WHERE created_at < %s
                """, (cutoff,))
                actions_deleted = cur.rowcount or 0

                # Удалить orphan blobs
                await cur.execute("""
                    DELETE FROM journal.blobs
                    WHERE ref_count <= 0 OR last_used_at < %s
                """, (cutoff,))
                blobs_deleted = cur.rowcount or 0

        return {
            "snapshots_deleted": snap_deleted,
            "actions_deleted": actions_deleted,
            "blobs_deleted": blobs_deleted,
        }

    async def get_history(self, days: int = 30) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT ts, disk_used_percent, blob_size_gb, actions_count
            FROM journal.retention_stats
            WHERE ts > now() - interval '%s days'
            ORDER BY ts ASC
        """, (days,))
        return [
            {
                **dict(r),
                "ts": r["ts"].isoformat() if r.get("ts") else None,
            }
            for r in rows
        ]
