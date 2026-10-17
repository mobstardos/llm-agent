"""API фичи «Кластер» — мультиинстанс-аналитика (Task 14).

Контракт: create_router() -> fastapi.APIRouter.

Данные: ops.instances (реестр инстансов: register/heartbeat/metrics —
src/db/multi_instance.py) + агрегаты нагрузки по зеркалам
(journal.events_mirror/actions, ops.chat_sessions/plans).
Фича только читает; PG недоступен → 503.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

try:
    import psycopg
except Exception:
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
                logger.info("cluster: PG недоступен: %s", e)
                return None
            pool = DatabasePool(_dsn(), min_size=1, max_size=4)
            await pool.start()
            _POOL = pool
    return _POOL


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="PostgreSQL недоступен — кластерная аналитика требует PG "
               "(ops.instances). Накатите схему: python scripts/init_db.py")


router = APIRouter(prefix="/api/cluster", tags=["cluster"])


@router.get("/instances")
async def instances():
    """Реестр инстансов: активные и «отсутствующие»."""
    pool = await _db()
    if pool is None:
        raise _unavailable()
    from src.db.multi_instance import InstanceRegistry
    reg = InstanceRegistry(pool, instance_id="__view__")
    try:
        active = await reg.list_active()
        stale = await reg.list_stale()
        stale = [s for s in stale
                 if s["instance_id"] not in
                 {a["instance_id"] for a in active}]
        return {"active": active, "stale": stale,
                "active_count": len(active), "stale_count": len(stale)}
    except Exception as e:
        raise HTTPException(503, f"cluster.instances: {e}") from e


@router.get("/analytics")
async def analytics(hours: int = Query(24, ge=1, le=720)):
    """Аналитика: реестр + нагрузка по зеркалам за окно (часы)."""
    pool = await _db()
    if pool is None:
        raise _unavailable()
    from src.db.multi_instance import analytics as inst_analytics
    try:
        return await inst_analytics(pool, hours=hours)
    except Exception as e:
        raise HTTPException(503, f"cluster.analytics: {e}") from e


@router.post("/cleanup")
async def cleanup(ttl: int = Query(21600, ge=600, le=86400 * 30)):
    """Вычистить записи инстансов без heartbeat дольше ttl секунд."""
    pool = await _db()
    if pool is None:
        raise _unavailable()
    from src.db.multi_instance import InstanceRegistry
    reg = InstanceRegistry(pool, instance_id="__view__")
    try:
        removed = await reg.cleanup_stale(ttl_seconds=ttl)
        return {"ok": True, "removed": removed, "ttl": ttl}
    except Exception as e:
        raise HTTPException(503, f"cluster.cleanup: {e}") from e


def create_router() -> APIRouter:
    return router
