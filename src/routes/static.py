# -*- coding: utf-8 -*-
"""Статические эндпоинты (5 шт.) — вынесены из src/main.py.

Рекомендация 1 (Sprint 1.A — первые роутеры).

Перенесённые эндпоинты (см. отчёт §4.4):
  - GET /             → FileResponse(index.html)
  - GET /favicon.ico  → SVG-ответ с 🤖 emoji
  - GET /analytics    → FileResponse(analytics.html)
  - GET /guide        → FileResponse(guide.html)
  - GET /metrics      → PlainTextResponse (Prometheus exposition format)

Это самые простые эндпоинты в проекте (read-only, без зависимостей),
поэтому они выносятся первыми — отличный "минный канарейка" для проверки
что новая структура работает.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, PlainTextResponse, Response

from src.core.metrics import PrometheusExporter
from src.state import get_registry, state as app_state

logger = logging.getLogger(__name__)

router = APIRouter(tags=["static"])

# WEB_DIR — тот же, что в src/app.py. Локальный импорт, чтобы не циклиться.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@router.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Главная страница веб-чата."""
    return FileResponse(WEB_DIR / "index.html")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Favicon — SVG с 🤖 emoji (без отдельного файла)."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<text y="0.9em" font-size="90">🤖</text></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/analytics", include_in_schema=False)
async def analytics_page() -> FileResponse:
    """HTML-страница дашборда аналитики."""
    return FileResponse(WEB_DIR / "analytics.html")


@router.get("/guide", include_in_schema=False)
async def guide_page() -> FileResponse:
    """HTML-страница интерактивного курса (10 уроков + квизы)."""
    return FileResponse(WEB_DIR / "guide.html")


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(
    registry=Depends(get_registry),
) -> PlainTextResponse:
    """Prometheus exposition format — scrape target для /metrics endpoint.

        GET /metrics
        # HELP llm_agent_agents_total Total registered agents
        # TYPE llm_agent_agents_total gauge
        llm_agent_agents_total{status="active"} 12
        ...
    """
    exporter = PrometheusExporter(
        registry=registry,
        cache=app_state.cache,
        # ... остальные параметры — см. оригинал src/main.py:1353
    )
    # Экспорт в Prometheus exposition format
    return PlainTextResponse(exporter.export(), media_type="text/plain")
