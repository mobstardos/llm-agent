"""VectorStore на PostgreSQL + pgvector.

Трёхуровневый поиск: bit → halfvec → vector.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024


@dataclass
class VectorSearchResult:
    id: str
    file: str
    content: str
    score: float
    start_line: int | None
    end_line: int | None
    language: str | None
    symbols: list[str]
    importance: float | None


def _to_half(vec: np.ndarray) -> np.ndarray:
    return vec.astype(np.float16)


class VectorStore:
    def __init__(self, pool: DatabasePool):
        self.pool = pool

    async def upsert_chunk(
        self,
        *,
        file: str,
        content: str,
        embedding: list[float],
        start_line: int | None = None,
        end_line: int | None = None,
        language: str | None = None,
        symbols: list[str] | None = None,
        importance: float | None = None,
        topic: str | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
    ) -> str:
        vec = np.asarray(embedding, dtype=np.float32)
        if vec.shape[0] != EMBEDDING_DIM:
            raise ValueError(
                f"Ожидается {EMBEDDING_DIM} измерений, получено {vec.shape[0]}"
            )
        half = _to_half(vec)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        query = """
            INSERT INTO vectors.chunks
                (file, content, start_line, end_line, language, symbols,
                 hash, embedding, embedding_half,
                 importance, topic, tags, metadata, updated_at)
            VALUES
                (%(file)s, %(content)s, %(start)s, %(end)s, %(lang)s,
                 %(symbols)s, %(hash)s, %(emb)s, %(emb_half)s,
                 %(importance)s, %(topic)s, %(tags)s, %(metadata)s, now())
            RETURNING id::text
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                from pgvector.psycopg import register_vector_async
                await register_vector_async(conn)
                await cur.execute(query, {
                    "file": file,
                    "content": content,
                    "start": start_line,
                    "end": end_line,
                    "lang": language,
                    "symbols": symbols or [],
                    "hash": content_hash,
                    "emb": vec.tolist(),
                    "emb_half": half.tolist(),
                    "importance": importance,
                    "topic": topic,
                    "tags": tags or [],
                    "metadata": metadata or {},
                })
                row = await cur.fetchone()
                return row["id"] if row else ""

    async def replace_file_chunks(
        self, file: str, chunks: list[dict],
    ) -> int:
        """Атомарно заменить все чанки файла."""
        async with self.pool.transaction() as conn:
            async with conn.cursor() as cur:
                from pgvector.psycopg import register_vector_async
                await register_vector_async(conn)

                await cur.execute(
                    "DELETE FROM vectors.chunks WHERE file = %s", (file,),
                )

                inserted = 0
                for c in chunks:
                    vec = np.asarray(c["embedding"], dtype=np.float32)
                    if vec.shape[0] != EMBEDDING_DIM:
                        continue
                    half = _to_half(vec)
                    content_hash = hashlib.sha256(
                        c["content"].encode("utf-8"),
                    ).hexdigest()

                    await cur.execute("""
                        INSERT INTO vectors.chunks
                            (file, content, start_line, end_line, language,
                             symbols, hash, embedding, embedding_half,
                             importance, topic, tags, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        file, c["content"],
                        c.get("start_line"), c.get("end_line"),
                        c.get("language"),
                        c.get("symbols") or [],
                        content_hash,
                        vec.tolist(), half.tolist(),
                        c.get("importance"), c.get("topic"),
                        c.get("tags") or [], c.get("metadata") or {},
                    ))
                    inserted += 1
                return inserted

    async def delete_file(self, file: str) -> int:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM vectors.chunks WHERE file = %s", (file,),
                )
                return cur.rowcount

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int = 10,
        min_score: float = 0.35,
        file_filter: str | None = None,
        language_filter: str | None = None,
        tags_filter: list[str] | None = None,
    ) -> list[VectorSearchResult]:
        vec = np.asarray(embedding, dtype=np.float32)
        half = _to_half(vec)

        conditions: list[str] = []
        params: dict[str, Any] = {
            "emb": half.tolist(),
            "k": top_k * 3,
        }
        if file_filter:
            conditions.append("file LIKE %(file_filter)s")
            params["file_filter"] = f"%{file_filter}%"
        if language_filter:
            conditions.append("language = %(language)s")
            params["language"] = language_filter
        if tags_filter:
            conditions.append("tags && %(tags)s")
            params["tags"] = tags_filter

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        query = f"""
            SELECT
                id::text, file, content, start_line, end_line, language,
                symbols, importance,
                1 - (embedding_half <=> %(emb)s::halfvec) AS score
            FROM vectors.chunks
            {where_clause}
            ORDER BY embedding_half <=> %(emb)s::halfvec
            LIMIT %(k)s
        """

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                from pgvector.psycopg import register_vector_async
                await register_vector_async(conn)
                await cur.execute(query, params)
                rows = await cur.fetchall()

        results: list[VectorSearchResult] = []
        for r in rows:
            score = float(r["score"])
            if score < min_score:
                continue
            results.append(VectorSearchResult(
                id=r["id"],
                file=r["file"],
                content=r["content"],
                score=score,
                start_line=r["start_line"],
                end_line=r["end_line"],
                language=r["language"],
                symbols=r["symbols"] or [],
                importance=r["importance"],
            ))
            if len(results) >= top_k:
                break

        return results

    async def stats(self) -> dict:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) AS c FROM vectors.chunks")
                total = (await cur.fetchone())["c"]
                await cur.execute(
                    "SELECT COUNT(DISTINCT file) AS c FROM vectors.chunks"
                )
                files = (await cur.fetchone())["c"]
        return {
            "enabled": True,
            "chunks": total,
            "files": files,
            "store": "postgresql+pgvector",
        }

    async def clear(self) -> int:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM vectors.chunks")
                return cur.rowcount
