# -*- coding: utf-8 -*-
"""Кэш-эндпоинт — вынесен из src/main.py.

Рекомендация 1 (Sprint 1.A).

Перенесённый эндпоинт (см. отчёт §4.4):
  - GET /api/cache/stats → {"enabled": true, ...} или {"enabled": false}
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.cache import ResponseCache
from src.state import get_cache

router = APIRouter()


@router.get("/stats")
async def cache_stats(cache: ResponseCache | None = Depends(get_cache)) -> dict:
    """Статистика кэша LLM-ответов (hits/misses/size).

    Без Depends(get_cache) нельзя — cache может быть None, если
    s.cache.enabled=False в settings.
    """
    if cache is None:
        return {"enabled": False}
    return {"enabled": True, **cache.stats()}
