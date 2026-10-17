# -*- coding: utf-8 -*-
"""Database (6 эндпоинтов) — вынесены из src/main.py.

Sprint 1.B: GET /api/db/health, POST /api/db/migrate-vectors,
POST /api/db/migrate-memory, GET /api/db/autodetect,
POST /api/db/autodetect/refresh, GET /api/db/status.
"""
from __future__ import annotations

import logging
import subprocess

from fastapi import APIRouter, Depends

from src.memory.facade import Memory
from src.state import get_memory, state

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/db", tags=["database"])


@router.get("/health")
async def db_health() -> dict:
    mem: Memory | None = state.memory
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    ok = await mem.pg_pool.health()
    return {"enabled": True, "healthy": ok}


@router.post("/migrate-vectors")
async def migrate_vectors() -> dict:
    """Переключить primary vector store на postgres."""
    result = subprocess.run(
        ["python", "scripts/migrate_lancedb_to_pg.py"],
        capture_output=True, text=True, timeout=1800,
    )
    return {
        "exit_code": result.returncode,
        "output": result.stdout[-5000:],
        "stderr": result.stderr[-2000:],
    }


@router.post("/migrate-memory")
async def migrate_memory() -> dict:
    """Перенос memory SQLite → PostgreSQL."""
    result = subprocess.run(
        ["python", "scripts/migrate_sqlite_memory.py"],
        capture_output=True, text=True, timeout=1800,
    )
    return {
        "exit_code": result.returncode,
        "output": result.stdout[-5000:],
        "stderr": result.stderr[-2000:],
    }


@router.get("/autodetect")
async def db_autodetect() -> dict:
    """Отчёт автодетекта PostgreSQL (Task 24-b) — без паролей."""
    try:
        from src.db.autodetect import last_result, load_report
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    rep = last_result() or load_report()
    clean = {k: v for k, v in rep.items()
             if k not in ("password", "dsn", "admin_dsn", "app_dsn")}
    clean["ok"] = rep.get("status") in ("ok", "already")
    return clean


@router.post("/autodetect/refresh")
async def db_autodetect_refresh() -> dict:
    """Повторить автодетект (без интерактива — из UI пароль не спросить)."""
    try:
        from src.db.autodetect import ensure_pg
        rep = ensure_pg(interactive=False, force=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    clean = {k: v for k, v in rep.items()
             if k not in ("password", "dsn", "admin_dsn", "app_dsn")}
    clean["ok"] = rep.get("status") in ("ok", "already")
    return clean


@router.get("/status")
async def db_status() -> dict:
    """Состояние PostgreSQL и репликатора (мягко: PG может быть выключен)."""
    result: dict = {
        "postgres": {"enabled": False, "healthy": False},
        "replicator": None,
    }
    try:
        s = get_settings()
        result["postgres"]["enabled"] = bool(s.use_postgres)
        try:
            from urllib.parse import urlparse
            u = urlparse(s.postgres.dsn())
            result["postgres"]["host"] = u.hostname or ""
            result["postgres"]["port"] = u.port or 5432
            result["postgres"]["database"] = (u.path or "/").lstrip("/")
        except Exception:
            pass
    except Exception:
        pass

    memory = state.memory
    if memory is not None:
        result["postgres"]["backend"] = (
            "postgresql" if memory.use_postgres else "sqlite+lancedb"
        )
        if memory.pg_pool is not None:
            try:
                result["postgres"]["healthy"] = await memory.pg_pool.health()
            except Exception:
                pass

    rep = state.pg_replicator
    if rep is not None:
        st = rep.status()
        result["replicator"] = st
        if st.get("pg_ok"):
            result["postgres"]["healthy"] = True
    return result


# Local helper to avoid circular import
def get_settings():
    from src.config import get_settings as gs
    return gs()
