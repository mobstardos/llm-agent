# -*- coding: utf-8 -*-
"""Chats + Digest (3 эндпоинта) — вынесены из src/main.py.

Sprint 1.B: POST /api/chats/import, GET /api/digest/latest,
POST /api/digest/refresh.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from src.app import BASE_DIR
from src.state import state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chats"])


@router.post("/chats/import")
async def chats_import(request: Request, provider: str = "deepseek") -> dict:
    """Импорт чатов с сайта провайдера (Task 27).

    Тело — сырой экспорт-JSON сайта (chat.deepseek.com → «Экспорт данных»).
    Каждый чат становится локальной сессией (data/sessions/<id>.jsonl).
    """
    from src.chat_import import import_chats

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Тело запроса — не JSON (нужен файл экспорта)")
    try:
        created = import_chats(
            data, BASE_DIR / "data" / "sessions", provider=provider,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("chats import failed")
        raise HTTPException(500, "Импорт не удался — см. лог сервера")
    return {"ok": True, "imported": created, "count": len(created)}


@router.get("/digest/latest")
async def digest_latest() -> dict:
    """Последний дайджест журнала (локальная модель, Task 24-c)."""
    mw = state.micro_worker
    if mw is None:
        return {
            "enabled": False,
            "digest": None,
            "hint": "включите LOCAL_MICRO_TASKS_ENABLED=1",
        }
    return {"enabled": True, "digest": mw.last_digest, "stats": mw.stats()}


@router.post("/digest/refresh")
async def digest_refresh() -> dict:
    """Пересобрать дайджест сейчас (локальной моделью)."""
    mw = state.micro_worker
    if mw is None:
        raise HTTPException(503, "Микрозадачи выключены")
    digest = await mw.force_digest()
    return {"ok": True, "digest": digest}
