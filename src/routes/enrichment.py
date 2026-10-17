# -*- coding: utf-8 -*-
"""Enrichment (1 эндпоинт) — вынесен из src/main.py.

Sprint 1.B: GET /api/enrichment/status.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from src.memory.facade import Memory
from src.ollama.client import get_ollama
from src.ollama.worker import EnrichmentWorker
from src.state import state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/enrichment", tags=["enrichment"])


@router.get("/status")
async def enrichment_status() -> dict:
    """Статус воркера обогащения (Ollama + PostgreSQL)."""
    worker: EnrichmentWorker | None = state.enrichment_worker
    mem: Memory | None = state.memory

    if not mem or not mem.pg_pool:
        return {"enabled": False, "reason": "PostgreSQL not configured"}

    try:
        rows = await mem.pg_pool.execute("""
            SELECT
                COUNT(*) FILTER (WHERE enriched_at IS NULL) AS pending,
                COUNT(*) FILTER (WHERE enriched_at IS NOT NULL) AS enriched,
                AVG(importance) AS avg_importance,
                COUNT(*) FILTER (WHERE importance >= 0.7) AS important,
                COUNT(*) FILTER (WHERE importance < 0.3) AS noise
            FROM memory.events
            WHERE created_at > now() - interval '30 days'
        """)
        stats = rows[0] if rows else {}
    except Exception as e:
        stats = {"error": str(e)}

    ollama_info = {}
    try:
        ollama = get_ollama()
        ollama_info = {
            "url": ollama.base_url,
            "model": ollama.model,
            "healthy": await ollama.health(),
            "models": await ollama.list_models(),
            "running": await ollama.running_models(),
        }
    except Exception as e:
        ollama_info = {"error": str(e)}

    return {
        "enabled": worker is not None,
        "worker": worker.stats() if worker else None,
        "db_stats": stats,
        "ollama": ollama_info,
    }
