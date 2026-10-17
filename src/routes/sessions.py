# -*- coding: utf-8 -*-
"""Сессии (1 эндпоинт) — вынесен из src/main.py.

Sprint 1.B: GET /api/sessions.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from src.app import BASE_DIR

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(limit: int = 20) -> dict:
    """Последние чаты с заголовками (Task 24-c: селектор чатов в UI)."""
    from src.background.micro_tasks import scan_sessions
    return scan_sessions(BASE_DIR / "data" / "sessions", limit=limit)
