"""Синхронизация данных из PostgreSQL-графа в AGE.

Читает graph.nodes и graph.edges из основной базы,
пишет в AGE-граф (через AgeStore).
"""
from __future__ import annotations

import logging

from src.db.age_store import AgeStore
from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)


class AgeSync:
    """Синхронизирует SQL-граф → AGE граф."""

    def __init__(self, pool: DatabasePool, age: AgeStore):
        self.pool = pool
        self.age = age

    async def sync_nodes(self, limit: int = 10000) -> int:
        """Синхронизировать узлы."""
        if not await self.age.is_available():
            return 0

        rows = await self.pool.execute("""
            SELECT id, kind, name, file, importance, role
            FROM graph.nodes
            LIMIT %s
        """, (limit,))

        count = 0
        for r in rows:
            try:
                kind = (r["kind"] or "").lower()
                if kind in ("file",):
                    await self.age.create_file_node(
                        path=r["file"] or r["name"],
                        importance=r["importance"],
                        role=r["role"],
                    )
                elif kind == "function":
                    await self.age.create_function_node(
                        id_=r["id"],
                        name=r["name"],
                        file=r["file"] or "",
                        importance=r["importance"],
                        role=r["role"],
                    )
                elif kind == "class":
                    await self.age.create_class_node(
                        id_=r["id"],
                        name=r["name"],
                        file=r["file"] or "",
                        importance=r["importance"],
                    )
                elif kind == "concept":
                    await self.age.create_concept_node(
                        name=r["name"],
                        importance=r["importance"],
                    )
                count += 1
            except Exception as e:
                logger.debug("Sync node %s: %s", r.get("id"), e)

        logger.info("AGE sync: %d nodes", count)
        return count

    async def sync_edges(self, limit: int = 50000) -> int:
        """Синхронизировать рёбра."""
        if not await self.age.is_available():
            return 0

        rows = await self.pool.execute("""
            SELECT src, dst, kind, weight
            FROM graph.edges
            LIMIT %s
        """, (limit,))

        count = 0
        for r in rows:
            try:
                src_kind, src_val = self._parse_node_id(r["src"])
                dst_kind, dst_val = self._parse_node_id(r["dst"])
                if not src_kind or not dst_kind:
                    continue

                await self.age.create_edge(
                    src_prop=(src_kind, src_val),
                    dst_prop=(dst_kind, dst_val),
                    kind=r["kind"],
                    weight=float(r["weight"] or 1.0),
                )
                count += 1
            except Exception as e:
                logger.debug("Sync edge %s→%s: %s", r.get("src"), r.get("dst"), e)

        logger.info("AGE sync: %d edges", count)
        return count

    @staticmethod
    def _parse_node_id(node_id: str) -> tuple[str, str]:
        """Парсит ID вида 'file:src/main.py' или 'func:src/main.py::hello'.

        Возвращает (prop_name, prop_value) для AGE.
        """
        if ":" not in node_id:
            return ("", "")
        prefix, rest = node_id.split(":", 1)
        prefix = prefix.lower()

        if prefix == "file":
            return ("path", rest)
        if prefix in ("func", "function"):
            return ("id", node_id)
        if prefix in ("class", "interface"):
            return ("id", node_id)
        if prefix == "module":
            return ("name", rest)
        if prefix == "concept":
            return ("name", rest)

        return ("id", node_id)

    async def sync_all(self) -> dict:
        """Полная синхронизация."""
        nodes = await self.sync_nodes()
        edges = await self.sync_edges()
        return {"nodes": nodes, "edges": edges}
