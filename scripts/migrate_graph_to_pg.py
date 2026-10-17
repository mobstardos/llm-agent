"""Миграция графа из SQLite (graph.sqlite) в PostgreSQL.

Запуск:
    python scripts/migrate_graph_to_pg.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sqlite_path = BASE_DIR / "data" / "graph.sqlite"
    if not sqlite_path.exists():
        logger.error("SQLite graph не найден: %s", sqlite_path)
        return 1

    logger.info("Читаю SQLite: %s", sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row

    nodes = conn.execute("SELECT * FROM nodes").fetchall()
    edges = conn.execute("SELECT * FROM edges").fetchall()

    logger.info("Nodes: %d, Edges: %d", len(nodes), len(edges))

    if args.dry_run:
        conn.close()
        return 0

    from src.db.pool import get_pool
    from src.db.graph_store import GraphStore

    pool = await get_pool()
    store = GraphStore(pool)

    # ─── Nodes ───────────────────────────────────────────
    nodes_ok = 0
    for i, n in enumerate(nodes):
        try:
            await store.upsert_node(
                id_=n["id"],
                kind=n["kind"],
                name=n["name"],
                file=n["file"],
                line=n["line"] if "line" in n.keys() else None,
            )
            nodes_ok += 1
        except Exception as e:
            logger.debug("Node %s: %s", n["id"], e)

        if (i + 1) % 5000 == 0:
            logger.info("  Nodes: %d/%d", i + 1, len(nodes))
    logger.info("  Nodes: %d/%d", nodes_ok, len(nodes))

    # ─── Edges ───────────────────────────────────────────
    edges_ok = 0
    for i, e in enumerate(edges):
        try:
            await store.upsert_edge(
                src=e["src"],
                dst=e["dst"],
                kind=e["kind"],
                weight=float(e["weight"] or 1.0)
                    if "weight" in e.keys() else 1.0,
            )
            edges_ok += 1
        except Exception as ex:
            logger.debug("Edge: %s", ex)

        if (i + 1) % 5000 == 0:
            logger.info("  Edges: %d/%d", i + 1, len(edges))
    logger.info("  Edges: %d/%d", edges_ok, len(edges))

    conn.close()

    stats = await store.stats()
    logger.info("═" * 60)
    logger.info("✓ Миграция завершена")
    logger.info("  PG graph: nodes=%d edges=%d",
                stats.get("nodes", 0), stats.get("edges", 0))
    logger.info("═" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
