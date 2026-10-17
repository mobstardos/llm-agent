# -*- coding: utf-8 -*-
"""Search + Synonyms (6 эндпоинтов) — вынесены из src/main.py.

Sprint 1.B: GET /api/search/events, GET /api/search/hybrid,
GET /api/search/fuzzy, GET /api/synonyms, POST /api/synonyms,
DELETE /api/synonyms/{term}.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException

from src.state import state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search/events")
async def search_events(q: str = "", kind: str = "", agent: str = "",
                        tool: str = "", limit: int = 50) -> dict:
    """Поиск событий журнала по тексту/kind/agent/tool.

    Sprint 1.B regression-fix: вызывался несуществующий
    journal.search_events — теперь через recorder.store
    (FTS/LIKE-поиск + фильтры query()).
    """
    journal = state.journal
    store = getattr(journal, "store", None) if journal is not None else None
    if store is None:
        return {"events": [], "enabled": False}
    try:
        if q.strip():
            events = store.search(q, limit=limit)
            if kind:
                events = [e for e in events if e.kind == kind]
            if agent:
                events = [e for e in events if e.agent_id == agent]
            if tool:
                events = [e for e in events if e.tool_name == tool]
        else:
            events = store.query(
                kind=kind or None, agent_id=agent or None,
                tool_name=tool or None, limit=limit,
            )
        out = [
            {
                "event_id": e.event_id,
                "ts": e.ts,
                "iso_time": e.iso_time,
                "kind": e.kind,
                "action": e.action,
                "status": e.status,
                "agent_id": e.agent_id,
                "server_name": e.server_name,
                "tool_name": e.tool_name,
                "targets": e.targets,
                "error": e.error,
                "duration_ms": e.duration_ms,
            }
            for e in events
        ]
        return {"events": out, "enabled": True}
    except Exception as e:
        logger.warning("journal search failed: %s", e)
        return {"events": [], "enabled": True, "error": str(e)}


@router.get("/search/hybrid")
async def search_hybrid(q: str, limit: int = 50) -> dict:
    """Гибридный поиск (vector + BM25 через RRF)."""
    memory = state.memory
    if memory is None or memory.pg_hybrid is None:
        return {"results": [], "enabled": False}
    results = await memory.pg_hybrid.search(q, limit=limit)
    return {"results": results, "enabled": True}


@router.get("/search/fuzzy")
async def search_fuzzy(q: str, limit: int = 20, threshold: float = 0.2) -> dict:
    """Fuzzy-поиск (Snowball-морфология + синонимы)."""
    memory = state.memory
    if memory is None or memory.pg_search is None:
        return {"results": [], "enabled": False}
    results = await memory.pg_search.fuzzy(q, limit=limit, threshold=threshold)
    return {"results": results, "enabled": True}


# ─── Synonyms ────────────────────────────────────────────────────────
@router.get("/synonyms")
async def list_synonyms() -> dict:
    """Список синонимов (для FTS-поиска)."""
    memory = state.memory
    if memory is None or memory.pg_search is None:
        return {"synonyms": {}, "enabled": False}
    return {"synonyms": memory.pg_search.list_synonyms(), "enabled": True}


@router.post("/synonyms")
async def add_synonym(payload: dict = Body(...)) -> dict:
    """Добавить синоним (term → [synonym1, synonym2, ...])."""
    memory = state.memory
    if memory is None or memory.pg_search is None:
        raise HTTPException(503, "Search not initialized")
    term = str(payload.get("term") or "").strip()
    synonyms = payload.get("synonyms") or []
    if not term or not synonyms:
        raise HTTPException(400, "term and synonyms required")
    await memory.pg_search.add_synonym(term, synonyms)
    return {"ok": True, "term": term, "synonyms": synonyms}


@router.delete("/synonyms/{term}")
async def delete_synonym(term: str) -> dict:
    """Удалить синоним по терму."""
    memory = state.memory
    if memory is None or memory.pg_search is None:
        raise HTTPException(503, "Search not initialized")
    await memory.pg_search.delete_synonym(term)
    return {"ok": True, "term": term}
