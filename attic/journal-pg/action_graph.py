"""Граф зависимостей между действиями.

Использует recursive CTE PostgreSQL.
"""
from __future__ import annotations

import logging

from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)


class ActionGraph:
    def __init__(self, pool: DatabasePool):
        self.pool = pool

    async def add_dependency(
        self, src: str, dst: str, kind: str = "caused_by",
    ) -> None:
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO journal.dependencies (src, dst, kind)
                        VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (src, dst, kind))
        except Exception as e:
            logger.debug("Add dep %s→%s: %s", src, dst, e)

    async def get_parents(self, action_id: str) -> list[dict]:
        return await self.pool.execute("""
            SELECT a.*, d.kind
            FROM journal.dependencies d
            JOIN journal.actions a ON a.id = d.dst
            WHERE d.src = %s
        """, (action_id,))

    async def get_children(self, action_id: str) -> list[dict]:
        return await self.pool.execute("""
            SELECT a.*, d.kind
            FROM journal.dependencies d
            JOIN journal.actions a ON a.id = d.src
            WHERE d.dst = %s
        """, (action_id,))

    async def transitive_children(
        self, action_id: str, max_depth: int = 5,
    ) -> list[dict]:
        """Все, кто зависит от действия (recursive)."""
        return await self.pool.execute("""
            WITH RECURSIVE tree AS (
                SELECT dst AS action_id, kind, 1 AS depth
                FROM journal.dependencies
                WHERE src = %s
                UNION
                SELECT d.dst, d.kind, t.depth + 1
                FROM journal.dependencies d
                JOIN tree t ON d.src = t.action_id
                WHERE t.depth < %s
            )
            SELECT DISTINCT
                a.id, a.agent, a.tool, a.created_at,
                t.depth, t.kind
            FROM tree t
            JOIN journal.actions a ON a.id = t.action_id
            ORDER BY t.depth, a.created_at
        """, (action_id, max_depth))

    async def transitive_parents(
        self, action_id: str, max_depth: int = 5,
    ) -> list[dict]:
        return await self.pool.execute("""
            WITH RECURSIVE tree AS (
                SELECT src AS action_id, kind, 1 AS depth
                FROM journal.dependencies
                WHERE dst = %s
                UNION
                SELECT d.src, d.kind, t.depth + 1
                FROM journal.dependencies d
                JOIN tree t ON d.dst = t.action_id
                WHERE t.depth < %s
            )
            SELECT DISTINCT
                a.id, a.agent, a.tool, a.created_at,
                t.depth, t.kind
            FROM tree t
            JOIN journal.actions a ON a.id = t.action_id
            ORDER BY t.depth, a.created_at
        """, (action_id, max_depth))

    async def auto_link(
        self, action_id: str,
        session_id: str | None = None,
        parent_id: str | None = None,
        affects_files: list[str] | None = None,
    ) -> int:
        """Автоматически связать действие с предыдущими.

        Правила:
        - Если parent_id — caused_by
        - Иначе — последнее write-действие в той же сессии к тем же файлам
        """
        linked = 0

        if parent_id:
            await self.add_dependency(action_id, parent_id, "caused_by")
            linked += 1

        if not session_id or not affects_files:
            return linked

        # Найти предыдущее write-действие в этой же сессии
        try:
            rows = await self.pool.execute("""
                SELECT id
                FROM journal.actions
                WHERE session_id = %s
                  AND id != %s
                  AND category IN ('write', 'patch', 'delete', 'move')
                  AND affects_files && %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (session_id, action_id, affects_files))

            if rows:
                await self.add_dependency(action_id, rows[0]["id"], "depends_on")
                linked += 1
        except Exception as e:
            logger.debug("Auto link: %s", e)

        return linked

    async def conflicts(
        self, action_id: str,
    ) -> list[dict]:
        """Найти конфликтующие действия (те же файлы, другие агенты)."""
        return await self.pool.execute("""
            SELECT a.id, a.agent, a.tool, a.created_at, a.affects_files
            FROM journal.actions a
            WHERE a.affects_files && (
                SELECT affects_files FROM journal.actions WHERE id = %s
            )
              AND a.id != %s
              AND a.category IN ('write', 'patch', 'delete', 'move')
            ORDER BY a.created_at DESC
            LIMIT 50
        """, (action_id, action_id))

    async def stats(self) -> dict:
        rows = await self.pool.execute("""
            SELECT
                (SELECT COUNT(*) FROM journal.actions) AS actions,
                (SELECT COUNT(*) FROM journal.dependencies) AS edges,
                (SELECT COUNT(DISTINCT session_id) FROM journal.actions
                    WHERE session_id IS NOT NULL) AS sessions
        """)
        return rows[0] if rows else {}
