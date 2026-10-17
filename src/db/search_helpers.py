"""Хелперы для полнотекстового поиска с синонимами."""
from __future__ import annotations

import logging

from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)


class SearchHelpers:
    """Обёртка над SQL-функциями поиска."""

    def __init__(self, pool: DatabasePool):
        self.pool = pool

    # ═══════════════════════════════════════════════════════
    # Расширение запроса синонимами
    # ═══════════════════════════════════════════════════════
    async def expand_terms(self, query: str) -> list[str]:
        """Расширить запрос синонимами."""
        rows = await self.pool.execute(
            "SELECT meta.expand_query_terms(%s) AS terms",
            (query,),
        )
        return rows[0]["terms"] if rows and rows[0].get("terms") else []

    # ═══════════════════════════════════════════════════════
    # Поиск событий
    # ═══════════════════════════════════════════════════════
    async def search_events(
        self,
        query: str,
        *,
        limit: int = 50,
        min_importance: float = 0.0,
        days_back: int = 30,
    ) -> list[dict]:
        """Полнотекстовый поиск событий с синонимами."""
        rows = await self.pool.execute("""
            SELECT * FROM meta.search_events(%s, %s, %s, %s)
        """, (query, limit, min_importance, days_back))

        return [
            {
                "event_id": r["event_id"],
                "summary": r["summary"],
                "topic": r["topic"],
                "tags": r["tags"] or [],
                "importance": r["importance"],
                "created_at": r["created_at"].isoformat()
                    if r.get("created_at") else None,
                "rank": round(float(r["rank"] or 0), 4),
            }
            for r in rows
        ]

    async def hybrid_search(
        self, query: str, limit: int = 50,
    ) -> list[dict]:
        """Гибридный поиск (BM25 + recency + importance)."""
        rows = await self.pool.execute("""
            SELECT * FROM meta.hybrid_search_events(%s, %s)
        """, (query, limit))

        return [
            {
                "event_id": r["event_id"],
                "summary": r["summary"],
                "importance": r["importance"],
                "bm25_rank": round(float(r["bm25_rank"] or 0), 4),
                "recency_boost": round(float(r["recency_boost"] or 0), 4),
                "combined_score": round(float(r["combined_score"] or 0), 4),
            }
            for r in rows
        ]

    # ═══════════════════════════════════════════════════════
    # Fuzzy по чанкам
    # ═══════════════════════════════════════════════════════
    async def fuzzy_chunks(
        self,
        query: str,
        limit: int = 20,
        threshold: float = 0.2,
    ) -> list[dict]:
        """Нечёткий поиск по чанкам (pg_trgm)."""
        rows = await self.pool.execute("""
            SELECT * FROM vectors.search_chunks_fuzzy(%s, %s, %s)
        """, (query, limit, threshold))

        return [
            {
                "id": str(r["id"]),
                "file": r["file"],
                "content": r["content"][:500],
                "similarity": round(float(r["similarity"] or 0), 4),
                "start_line": r["start_line"],
                "end_line": r["end_line"],
            }
            for r in rows
        ]

    # ═══════════════════════════════════════════════════════
    # Управление синонимами
    # ═══════════════════════════════════════════════════════
    async def add_synonym(
        self, term: str, syns: list[str], category: str | None = None,
    ) -> None:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO meta.synonyms (term, syns, category)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (term) DO UPDATE SET
                        syns = EXCLUDED.syns,
                        category = EXCLUDED.category,
                        updated_at = now()
                """, (term, syns, category))

    async def list_synonyms(self) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT term, syns, category, updated_at
            FROM meta.synonyms
            ORDER BY category, term
        """)
        return [
            {
                "term": r["term"],
                "syns": r["syns"] or [],
                "category": r["category"],
                "updated_at": r["updated_at"].isoformat()
                    if r.get("updated_at") else None,
            }
            for r in rows
        ]

    async def delete_synonym(self, term: str) -> bool:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM meta.synonyms WHERE term = %s", (term,),
                )
                return cur.rowcount > 0

    async def stats(self) -> dict:
        rows = await self.pool.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(DISTINCT category) AS categories,
                array_agg(DISTINCT category) FILTER (WHERE category IS NOT NULL)
                    AS category_list
            FROM meta.synonyms
        """)
        r = rows[0] if rows else {}
        return {
            "total_terms": r.get("total", 0),
            "categories": r.get("categories", 0),
            "category_list": r.get("category_list") or [],
        }
