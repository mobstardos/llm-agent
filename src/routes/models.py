# -*- coding: utf-8 -*-
"""Модели (2 эндпоинта) — вынесены из src/main.py.

Sprint 1.B: GET /api/models, POST /api/model/select.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from src.config import get_settings
from src.core.registry import Registry
from src.llm_providers import aggregate
from src.state import get_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["models"])


@router.get("/api/model/s")
@router.get("/api/models", include_in_schema=False)
async def list_models(reg: Registry | None = Depends(get_registry)) -> dict:
    """GET /api/model/s (+ alias /api/models) — агрегатор провайдеров.

    Живой опрос всех провайдеров параллельно (таймаут ~1.5с).
    """
    s = get_settings()
    saved = reg.runtime.get_model_pref() if reg else None
    try:
        data = await aggregate()
    except Exception as e:
        logger.warning("providers aggregate failed: %s", e)
        data = {"providers": []}

    flat: list[dict] = []
    for p in data["providers"]:
        if not p.get("available"):
            continue
        for m in p.get("models", []):
            qid = f"{p['id']}/{m['id']}"
            flat.append({"id": qid, "name": m.get("name") or m["id"],
                         "provider": p["id"]})
    if not flat:
        flat = [
            {"id": "qwen3.8-max", "name": "Qwen 3.8 Max", "provider": "qwen"},
            {"id": "qwen3-fast", "name": "Qwen 3 Fast", "provider": "qwen"},
        ]
    return {
        "providers": data["providers"],
        "models": flat,
        "default": s.llm.model,
        "saved": saved,
    }


@router.post("/api/model/select")
async def select_model(payload: dict = Body(...),
                       reg: Registry = Depends(get_registry)) -> dict:
    """POST /api/model/select — сохранить выбранную в чате модель."""
    model = str(payload.get("model") or "").strip()
    if reg is None:
        raise HTTPException(503, "Registry not ready")
    reg.runtime.set_model_pref(model or None)
    return {"ok": True, "saved": model}
