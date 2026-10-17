# -*- coding: utf-8 -*-
"""Analytics (10 эндпоинтов) — вынесены из src/main.py.

Sprint 1.B: GET /api/analytics/{overview,daily,hourly,enrichment,agents,
topics,tools,files,mv-info}, POST /api/analytics/refresh.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from src.state import state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _analytics() -> "Analytics | None":
    """Получить Analytics (из memory.pg_pool или None)."""
    memory = state.memory
    if memory is None or memory.pg_pool is None:
        return None
    from src.db.analytics import Analytics
    return Analytics(memory.pg_pool)


@router.get("/overview")
async def analytics_overview() -> dict:
    """Обзор: общее число событий, средний importance, активные агенты."""
    a = _analytics()
    if a is None:
        return {"enabled": False}
    return await a.overview()


@router.get("/daily")
async def analytics_daily(days: int = 30) -> dict:
    """Активность по дням."""
    a = _analytics()
    if a is None:
        return {"enabled": False}
    return await a.daily(days=days)


@router.get("/hourly")
async def analytics_hourly(hours: int = 48) -> dict:
    """Активность по часам."""
    a = _analytics()
    if a is None:
        return {"enabled": False}
    return await a.hourly(hours=hours)


@router.get("/enrichment")
async def analytics_enrichment(days: int = 30) -> dict:
    """Статистика enrichment (Ollama): важность, теги, темы."""
    a = _analytics()
    if a is None:
        return {"enabled": False}
    return await a.enrichment_stats(days=days)


@router.get("/agents")
async def analytics_agents() -> dict:
    """Топ агентов по числу tool-calls."""
    a = _analytics()
    if a is None:
        return {"enabled": False}
    return await a.agents_top()


@router.get("/topics")
async def analytics_topics(limit: int = 20) -> dict:
    """Топ тем (по тегам enrichment)."""
    a = _analytics()
    if a is None:
        return {"enabled": False}
    return await a.top_topics(limit=limit)


@router.get("/tools")
async def analytics_tools(limit: int = 20) -> dict:
    """Топ инструментов (по числу вызовов)."""
    a = _analytics()
    if a is None:
        return {"enabled": False}
    return await a.top_tools(limit=limit)


@router.get("/files")
async def analytics_files(limit: int = 20) -> dict:
    """Топ файлов (по числу изменений)."""
    a = _analytics()
    if a is None:
        return {"enabled": False}
    return await a.top_files(limit=limit)


@router.post("/refresh")
async def analytics_refresh(view: str | None = None) -> dict:
    """Принудительно обновить materialized view."""
    a = _analytics()
    if a is None:
        return {"enabled": False}
    await a.refresh_view(view)
    return {"ok": True, "view": view or "all"}


@router.get("/mv-info")
async def analytics_mv_info() -> dict:
    """Информация о materialized views (имена, last_refresh, row_count)."""
    a = _analytics()
    if a is None:
        return {"enabled": False}
    return await a.mv_info()
