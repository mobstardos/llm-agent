"""Гибридный поиск: vector + BM25 через RRF."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024


@dataclass
class HybridResult:
    id: str
    file: str
    content: str
    combined_score: float
    vector_rank: int | None
    bm25_rank: int | None
    start_line: int | None
    end_line: int | None
    language: str | None
    symbols: list[str]


class HybridSearch:
    """RRF: 0.6 × vector + 0.3 × BM25 + 0.1 × recency."""

    def __init__(self, pool: DatabasePool, weights: dict | None = None):
        self.pool = pool
        self.weights = weights or {
            "vector": 0.6,
            "bm25": 0.3,
            "recency": 0.1,
        }

    async def search(
        self,
        query: str,
        embedding: list[float],
        *,
        top_k: int = 10,
        candidate_k: int = 50,
        language_filter: str | None = None,
        file_filter: str | None = None,
    ) -> list[HybridResult]:
        vec = np.asarray(embedding, dtype=np.float32)
        if vec.shape[0] != EMBEDDING_DIM:
            raise ValueError(f"Ожидается {EMBEDDING_DIM} измерений")

        half = vec.astype(np.float16).tolist()

        filters: list[str] = []
        params: dict = {
            "emb": half,
            "query": query,
            "cand_k": candidate_k,
            "k": top_k,
            "w_vec": self.weights["vector"],
            "w_bm25": self.weights["bm25"],
        }
        if language_filter:
            filters.append("language = %(lang)s")
            params["lang"] = language_filter
        if file_filter:
            filters.append("file LIKE %(file_f)s")
            params["file_f"] = f"%{file_filter}%"

        where = ("WHERE " + " AND ".join(filters)) if filters else ""

        query_sql = f"""
        WITH
        vector_search AS (
            SELECT id::text, file, content, start_line, end_line,
                   language, symbols, created_at,
                   ROW_NUMBER() OVER (
                       ORDER BY embedding_half <=> %(emb)s::halfvec
                   ) AS rank
            FROM vectors.chunks
            {where}
            ORDER BY embedding_half <=> %(emb)s::halfvec
            LIMIT %(cand_k)s
        ),
        bm25_search AS (
            SELECT id::text, file, content, start_line, end_line,
                   language, symbols, created_at,
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank_cd(
                           to_tsvector('russian', content),
                           plainto_tsquery('russian', %(query)s)
                       ) DESC
                   ) AS rank
            FROM vectors.chunks
            {where + (' AND ' if where else 'WHERE ')}
                to_tsvector('russian', content) @@
                plainto_tsquery('russian', %(query)s)
            LIMIT %(cand_k)s
        ),
        candidates AS (
            SELECT id FROM vector_search
            UNION
            SELECT id FROM bm25_search
        )
        SELECT
            c.id,
            COALESCE(v.file, b.file) AS file,
            COALESCE(v.content, b.content) AS content,
            COALESCE(v.start_line, b.start_line) AS start_line,
            COALESCE(v.end_line, b.end_line) AS end_line,
            COALESCE(v.language, b.language) AS language,
            COALESCE(v.symbols, b.symbols, ARRAY[]::text[]) AS symbols,
            v.rank AS vector_rank,
            b.rank AS bm25_rank,
            COALESCE(%(w_vec)s::real / (60 + v.rank), 0) +
            COALESCE(%(w_bm25)s::real / (60 + b.rank), 0) AS combined_score
        FROM candidates c
        LEFT JOIN vector_search v ON v.id = c.id
        LEFT JOIN bm25_search b ON b.id = c.id
        ORDER BY combined_score DESC
        LIMIT %(k)s
        """

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                from pgvector.psycopg import register_vector_async
                await register_vector_async(conn)
                await cur.execute(query_sql, params)
                rows = await cur.fetchall()

        results: list[HybridResult] = []
        for r in rows:
            results.append(HybridResult(
                id=r["id"],
                file=r["file"],
                content=r["content"],
                combined_score=float(r["combined_score"]),
                vector_rank=int(r["vector_rank"]) if r["vector_rank"] else None,
                bm25_rank=int(r["bm25_rank"]) if r["bm25_rank"] else None,
                start_line=r["start_line"],
                end_line=r["end_line"],
                language=r["language"],
                symbols=r["symbols"] or [],
            ))
        return results
