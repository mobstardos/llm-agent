"""API фичи «Память» — семантический индекс прошлых задач (pgvector).

Контракт: create_router() -> fastapi.APIRouter (FeatureLoader монтирует
без правок main.py).

Источник данных: memory.tasks (db/init.sql), наполняет
AgentMemoryIndexer (src/db/agent_memory.py, main.py блок 7.5) из зеркал
репликатора. Фича живёт на СВОЁМ ленивом пуле (изоляция от горячего
пути), DSN — тот же settings.postgres.dsn(). PG недоступен → 503.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

try:
    import psycopg
except Exception:                       # фича монтируется, но требует PG
    psycopg = None

try:
    from src.db.pool import DatabasePool
except Exception:
    DatabasePool = None

_POOL = None
_POOL_LOCK: asyncio.Lock | None = None


def _dsn() -> str:
    from src.config import get_settings
    return get_settings().postgres.dsn()


async def _db():
    """Пул соединений или None, если PG недоступен."""
    global _POOL, _POOL_LOCK
    if psycopg is None or DatabasePool is None:
        return None
    if _POOL is not None:
        return _POOL
    if _POOL_LOCK is None:
        _POOL_LOCK = asyncio.Lock()
    async with _POOL_LOCK:
        if _POOL is None:
            try:
                conn = await psycopg.AsyncConnection.connect(
                    _dsn(), connect_timeout=3)
                await conn.close()
            except Exception as e:
                logger.info("memory: PG недоступен: %s", e)
                return None
            pool = DatabasePool(_dsn(), min_size=1, max_size=4)
            await pool.start()
            _POOL = pool
    return _POOL


def _memory(pool):
    from src.db.agent_memory import AgentMemory
    return AgentMemory(pool)


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="PostgreSQL недоступен — память агентов требует PG "
               "(memory.tasks). Проверьте DATABASE_URL/PG_APP_* и "
               "накатите схему: python scripts/init_db.py")


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=50)
    kind: str | None = None
    session_id: str | None = None
    min_score: float = Field(default=0.15, ge=0.0, le=1.0)


class IndexBody(BaseModel):
    limit: int = Field(default=200, ge=1, le=2000)


router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("/stats")
async def stats():
    pool = await _db()
    if pool is None:
        raise _unavailable()
    m = _memory(pool)
    try:
        return await m.stats()
    except Exception as e:
        raise HTTPException(503, f"memory.stats: {e}") from e


@router.get("/recent")
async def recent(limit: int = Query(20, ge=1, le=200),
                 kind: str | None = None):
    pool = await _db()
    if pool is None:
        raise _unavailable()
    try:
        return {"items": await _memory(pool).recent(limit, kind=kind)}
    except Exception as e:
        raise HTTPException(503, f"memory.recent: {e}") from e


@router.post("/search")
async def search(body: SearchBody):
    pool = await _db()
    if pool is None:
        raise _unavailable()
    try:
        return await _memory(pool).search(
            body.query, top_k=body.top_k, kind=body.kind,
            session_id=body.session_id, min_score=body.min_score)
    except Exception as e:
        raise HTTPException(503, f"memory.search: {e}") from e


@router.post("/index/run")
async def index_run(body: IndexBody | None = None):
    """Ручной прогон индексатора (обычно он фоновый, раз в 2 минуты)."""
    pool = await _db()
    if pool is None:
        raise _unavailable()
    limit = body.limit if body else 200
    try:
        res = await _memory(pool).backfill_from_mirrors(limit=limit)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(503, f"memory.index: {e}") from e


@router.delete("")
async def clear(confirm: bool = False, kind: str | None = None):
    """Очистка памяти. Требует confirm=true — защита от случайности."""
    if not confirm:
        raise HTTPException(
            400, "Требуется confirm=true — операция удаляет записи")
    pool = await _db()
    if pool is None:
        raise _unavailable()
    try:
        deleted = await _memory(pool).clear(kind=kind)
        return {"ok": True, "deleted": deleted, "kind": kind}
    except Exception as e:
        raise HTTPException(503, f"memory.clear: {e}") from e


def create_router() -> APIRouter:
    return router
