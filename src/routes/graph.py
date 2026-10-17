# -*- coding: utf-8 -*-
"""Graph + CDC (7 эндпоинтов) — вынесены из src/main.py.

Sprint 1.B: GET /api/age/stats, POST /api/age/cypher,
GET /api/age/impact/{node_id:path}, GET /api/age/who-uses/{node_id:path},
GET /api/age/concepts, POST /api/age/sync, GET /api/cdc/status.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body

from src.state import state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["graph"])


@router.get("/age/stats")
async def age_stats() -> dict:
    """Статистика AGE-графа (узлы/рёбра по типам)."""
    age = state.age_store
    if age is None:
        return {"enabled": False}
    return await age.stats()


@router.post("/age/cypher")
async def age_cypher(payload: dict = Body(...)) -> dict:
    """Выполнить Cypher-запрос (read-only по умолчанию)."""
    age = state.age_store
    if age is None:
        return {"error": "AGE not enabled"}
    query = payload.get("query", "")
    return await age.cypher(query)


@router.get("/age/impact/{node_id:path}")
async def age_impact(node_id: str, depth: int = 5) -> dict:
    """Анализ влияния узла на других (depth уровней)."""
    age = state.age_store
    if age is None:
        return {"error": "AGE not enabled"}
    return await age.impact_analysis(node_id, depth=depth)


@router.get("/age/who-uses/{node_id:path}")
async def age_who_uses(node_id: str, depth: int = 3) -> dict:
    """Кто ссылается на узел (обратный обход графа)."""
    age = state.age_store
    if age is None:
        return {"error": "AGE not enabled"}
    return await age.who_uses(node_id, depth=depth)


@router.get("/age/concepts")
async def age_concepts(query: str = "", limit: int = 20) -> dict:
    """Поиск концепций в графе по подстроке."""
    age = state.age_store
    if age is None:
        return {"concepts": []}
    return {"concepts": await age.search_concepts(query, limit=limit)}


@router.post("/age/sync")
async def age_sync_endpoint() -> dict:
    """Синхронизировать граф из memory/graph_store (фоново)."""
    sync = state.age_sync
    if sync is None:
        return {"error": "AGE sync not enabled"}
    return await sync.run_once()


@router.get("/cdc/status")
async def cdc_status() -> dict:
    """Статус CDC-воркера (PostgreSQL → Kafka)."""
    # CDC worker хранится локально в app.py lifespan. Доступ через state.
    # TODO: добавить cdc_worker в AppState в Sprint 1.C
    return {"enabled": False, "reason": "CDC worker not in state (TODO 1.C)"}
