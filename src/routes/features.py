# -*- coding: utf-8 -*-
"""Реестр фич — вынесен из src/main.py.

Рекомендация 1 (Sprint 1.A).

Перенесённый эндпоинт (см. отчёт §4.4):
  - GET /api/features → {"features": [...], "events": [...]}

Возвращает список всех фич (активных и неактивных) для UI и последние
30 событий из events bus (для диагностики).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src import events
from src.core.features import FeatureLoader
from src.state import get_features_loader

router = APIRouter()


@router.get("/features")
async def list_features(
    fl: FeatureLoader | None = Depends(get_features_loader),
) -> dict:
    """Реестр фич (вкладки UI) + последние события шины.

    app.js подгружает ui.js из features/<id>/ui.js для каждой enabled-фичи.
    """
    return {
        "features": fl.list_for_client() if fl else [],
        "events": events.recent(30),
    }
