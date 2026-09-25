"""KnowledgeGraph на PostgreSQL с recursive CTE."""
from __future__ import annotations

import logging

from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)


class GraphStore:
    def __init__(self, pool: DatabasePool):
        self.pool = pool

    async def upsert_node(
        self, id_: str, kind: str, name: str,
        file: str | None = None, line: int | None = None,
        importance: float | None = None,
        role: str | None = None,
        complexity: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO graph.nodes
                        (id, kind, name, file, line, importance, role,
                         complexity, metadata, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        file = EXCLUDED.file,
                        line = EXCLUDED.line,
                        importance = COALESCE(EXCLUDED.importance, graph.nodes.importance),
                        role = COALESCE(EXCLUDED.role, graph.nodes.role),
                        complexity = COALESCE(EXCLUDED.complexity, graph.nodes.complexity),
                        updated_at = now()
                """, (
                    id_, kind, name, file, line, importance, role,
                    complexity, metadata or {},
                ))

    async def upsert_edge(
        self, src: str, dst: str, kind: str,
        weight: float = 1.0, metadata: dict | None = None,
    ) -> None:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO graph.edges (src, dst, kind, weight, metadata, updated_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                    ON CONFLICT (src, dst, kind) DO UPDATE SET
                        weight = EXCLUDED.weight,
                        updated_at = now()
                """, (src, dst, kind, weight, metadata or {}))

    async def who_uses(self, name: str, depth: int = 2) -> list[dict]:
        return await self.pool.execute("""
            WITH RECURSIVE uses AS (
                SELECT src AS node_id, 1 AS depth
                FROM graph.edges
                WHERE dst LIKE %s OR dst = %s
                UNION
                SELECT e.src, u.depth + 1
                FROM graph.edges e
                JOIN uses u ON e.dst = u.node_id
                WHERE u.depth < %s
            )
            SELECT DISTINCT u.node_id AS src, u.depth,
                   n.name, n.file, n.kind
            FROM uses u
            LEFT JOIN graph.nodes n ON n.id = u.node_id
            ORDER BY u.depth, n.name
        """, (f"%::{name}", name, depth))

    async def depends_on(self, file: str, depth: int = 3) -> list[dict]:
        return await self.pool.execute("""
            WITH RECURSIVE deps AS (
                SELECT dst AS node_id, 1 AS depth
                FROM graph.edges WHERE src = %s
                UNION
                SELECT e.dst, d.depth + 1
                FROM graph.edges e
                JOIN deps d ON e.src = d.node_id
                WHERE d.depth < %s
            )
            SELECT DISTINCT d.node_id, d.depth
            FROM deps d ORDER BY d.depth
        """, (f"file:{file}", depth))

    async def impact_analysis(self, node_id: str, max_depth: int = 5) -> list[dict]:
        return await self.pool.execute("""
            WITH RECURSIVE impact AS (
                SELECT node_id, 1 AS depth, ARRAY[node_id] AS path
                FROM (
                    SELECT dst AS node_id FROM graph.edges WHERE src = %s
                ) sub
                UNION ALL
                SELECT e.dst, i.depth + 1, i.path || e.dst
                FROM graph.edges e
                JOIN impact i ON e.src = i.node_id
                WHERE i.depth < %s AND NOT (e.dst = ANY(i.path))
            )
            SELECT DISTINCT i.node_id, i.depth,
                   n.kind, n.name, n.file, n.importance, n.role
            FROM impact i
            LEFT JOIN graph.nodes n ON n.id = i.node_id
            ORDER BY n.importance DESC NULLS LAST, i.depth
        """, (node_id, max_depth))

    async def search_nodes(self, query: str, limit: int = 20) -> list[dict]:
        return await self.pool.execute("""
            SELECT id, kind, name, file, line, importance, role
            FROM graph.nodes
            WHERE name %% %s
            ORDER BY similarity(name, %s) DESC
            LIMIT %s
        """, (query, query, limit))

    async def stats(self) -> dict:
        rows = await self.pool.execute("""
            SELECT
                (SELECT COUNT(*) FROM graph.nodes) AS nodes,
                (SELECT COUNT(*) FROM graph.edges) AS edges
        """)
        return rows[0] if rows else {"nodes": 0, "edges": 0}
