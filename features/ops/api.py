"""API фичи «Эксплуатация» — бэкапы и ретенция зеркал (Task 14).

Контракт: create_router() -> fastapi.APIRouter.

Бэкапы: BackupManager (pg_dump/pg_restore, env PG_APP_* + BACKUP_*),
папка data/db_backups. pg_dump не установлен → 503 с подсказкой.
Ретенция: PGRetention (src/db/retention.py) поверх ленивого пула;
по умолчанию dry-run — реальное удаление требует confirm=true.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

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
                logger.info("ops: PG недоступен: %s", e)
                return None
            pool = DatabasePool(_dsn(), min_size=1, max_size=4)
            await pool.start()
            _POOL = pool
    return _POOL


def _project_root() -> str:
    from src.config import get_settings
    return str(get_settings().project_root)


def _backup_manager():
    from src.db.backup import BackupManager
    return BackupManager(
        backup_dir=os.path.join(_project_root(), "data", "db_backups"),
        host=os.getenv("PG_APP_HOST", "localhost"),
        port=int(os.getenv("PG_APP_PORT", "5432")),
        user=os.getenv("PG_APP_USER", "llmagent"),
        password=os.getenv("PG_APP_PASSWORD", "secret"),
        database=os.getenv("PG_APP_DATABASE", "llmagent"),
        keep_last=int(os.getenv("BACKUP_KEEP_LAST", "7")),
    )


def _retention(pool):
    from src.db.retention import PGRetention
    return PGRetention(
        pool, default_days=int(os.getenv("PG_RETENTION_DAYS", "0") or 0))


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="PostgreSQL недоступен. Проверьте DATABASE_URL/PG_APP_*")


class RetentionBody(BaseModel):
    days: int | None = Field(default=None, ge=1, le=3650)
    dry_run: bool = True
    confirm: bool = False


router = APIRouter(prefix="/api/ops-admin", tags=["ops"])


@router.get("/overview")
async def overview():
    """Сводка: PG, бэкапы, ретенция (кратко)."""
    pool = await _db()
    b = _backup_manager()
    out: dict = {
        "pg": {"available": pool is not None},
        "backups": b.stats(),
        "retention_days": int(os.getenv("PG_RETENTION_DAYS", "0") or 0),
    }
    if pool is not None:
        try:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT now()::text AS t, version() AS v")
                row = await cur.fetchone()
                out["pg"]["server_time"] = row["t"]
                out["pg"]["version"] = (row["v"] or "").split(",")[0]
        except Exception as e:
            out["pg"]["error"] = str(e)[:200]
    return out


@router.get("/backups")
async def backups_list():
    return {"items": _backup_manager().list_backups(),
            "stats": _backup_manager().stats()}


@router.post("/backups")
async def backup_create(confirm: bool = Query(False)):
    """Создать бэкап сейчас. Требует confirm=true (запись на диск)."""
    if not confirm:
        raise HTTPException(400, "Требуется confirm=true")
    import shutil
    if not shutil.which("pg_dump"):
        raise HTTPException(
            503, "pg_dump не найден в PATH — установите PostgreSQL "
                 "client tools (см. docs/DATABASE.md §7)")
    b = _backup_manager()
    try:
        path = await b.create_backup()
    except Exception as e:
        raise HTTPException(503, f"pg_dump: {e}") from e
    if path is None:
        raise HTTPException(503, "pg_dump завершился с ошибкой — "
                                 "смотрите логи сервера")
    return {"ok": True, "path": str(path), "stats": b.stats()}


@router.get("/retention")
async def retention_status(days: int = Query(0, ge=0, le=3650)):
    """Статистика зеркал: сколько строк, сколько удаляемо за days дней."""
    pool = await _db()
    if pool is None:
        raise _unavailable()
    try:
        return await _retention(pool).status(
            days=days if days > 0 else None)
    except Exception as e:
        raise HTTPException(503, f"retention: {e}") from e


@router.post("/retention/run")
async def retention_run(body: RetentionBody):
    """Прогон ретенции. dry_run=true (по умолчанию) — только посчитать.

    Реальное удаление: {\"days\": N, \"dry_run\": false, \"confirm\": true}.
    """
    if not body.dry_run and not body.confirm:
        raise HTTPException(
            400, "Реальное удаление требует confirm=true")
    pool = await _db()
    if pool is None:
        raise _unavailable()
    try:
        return await _retention(pool).run(
            days=body.days, dry_run=body.dry_run)
    except Exception as e:
        raise HTTPException(503, f"retention: {e}") from e


def create_router() -> APIRouter:
    return router
