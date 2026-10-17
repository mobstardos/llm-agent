# -*- coding: utf-8 -*-
"""API фичи «__template__» (эталонный скелетон).

Контракт (такой же, как у features/journal, features/notes, features/ops):

  def create_router() -> fastapi.APIRouter

FeatureLoader (src/core/features.py) вызывает `create_router()` и
монтирует роутер через `app.include_router(router)`. ВАЖНО: метод
возвращает APIRouter — НЕ декорирует функции как @router.get(...)
на уровне модуля (так хрупко к порядку импортов). Вместо этого
внутри `create_router()` создайте локальный APIRouter и добавьте
эндпоинты через @router.get(...).

Это позволяет:
  - отложенный импорт зависимостей (lazy imports) — фича грузится
    только если enable: true в feature.yaml;
  - тестировать фичу через `from features.__template__.api import
    create_router; app = FastAPI(); app.include_router(create_router())`;
  - изолировать state фичи в closure (см. _state ниже).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)


def create_router() -> APIRouter:
    """Создаёт и возвращает роутер фичи.

    Вызывается FeatureLoader-ом при `enable: true` в feature.yaml.
    Все импорты зависимостей — внутри функции (lazy), чтобы фича
    не падала на старте, если зависимости нет (enable: false).
    """
    # ── Lazy imports ──────────────────────────────────────────────
    # Если фиче нужны пакеты из requires.python_packages, импортируем
    # их ЗДЕСЬ, не на уровне модуля. Так enable: false в feature.yaml
    # пропускает загрузку фичи полностью.
    try:
        # from src.config import get_settings  # пример
        pass
    except ImportError as e:
        logger.warning("Фича __template__ не может загрузить "
                        "зависимости: %s", e)
        raise

    # ── Локальный state ───────────────────────────────────────────
    # Если фиче нужно хранить in-memory state (например, подписки
    # на события), делаем это через closure в create_router().
    state: dict[str, Any] = {
        "started_at": __import__("time").time(),
        "items": [],
    }

    router = APIRouter(prefix="/api/__template__", tags=["__template__"])

    # ── Endpoints ─────────────────────────────────────────────────
    # Минимум: list, get, create, health. Добавляйте по необходимости.

    @router.get("/health")
    async def health() -> dict:
        """Health-check — вызывается из main.py lifespan /api/features/health."""
        return {"status": "ok", "items": len(state["items"])}

    @router.get("/items")
    async def list_items() -> dict:
        """Список всех items."""
        return {"items": state["items"]}

    @router.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict:
        """Получить item по id."""
        for it in state["items"]:
            if it.get("id") == item_id:
                return it
        raise HTTPException(404, detail=f"item {item_id} not found")

    @router.post("/items")
    async def create_item(payload: dict) -> dict:
        """Создать новый item."""
        item = {
            "id": __import__("uuid").uuid4().hex[:16],
            "data": payload,
        }
        state["items"].append(item)
        # ── Публикация события (опционально) ──────────────────────
        # Если фича стримит события через ws_events, используйте
        # src.events.publish:
        try:
            from src import events
            await events.publish("__template__.status_changed", item)
        except Exception:  # noqa: BLE001 — события не должны валить API
            logger.debug("events.publish failed (events bus not initialised)")

        return item

    logger.info("Фича __template__ загружена: %d эндпоинтов",
                len(router.routes))
    return router
