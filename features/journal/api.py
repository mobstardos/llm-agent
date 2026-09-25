"""API фичи «Журнал» (Этап 5, V2 §2.2).

Роутер /api/journal/* реализован в src/journal/api.py (SQLite-поколение,
18 эндпоинтов: события, diff, поиск, таймлайн, граф, провенанс, откат,
replay, ретенция, отчёты). Здесь только мост для FeatureLoader:

    api_router: api.py:create_router

Контракт тот же, что у эталонной features/notes: create_router() ->
fastapi.APIRouter; FeatureLoader делает app.include_router() без правок
src/main.py.
"""
from __future__ import annotations

from fastapi import APIRouter


def create_router() -> APIRouter:
    """Возвращает готовый роутер журнала."""
    from src.journal.api import build_router

    return build_router()
