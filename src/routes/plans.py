# -*- coding: utf-8 -*-
"""Планы Supervisor (2 эндпоинта) — вынесены из src/main.py.

Sprint 1.B: GET /api/plans, GET /api/plans/{plan_id}.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.state import get_plans
from src.supervisor.plans import PlanRegistry, view_for_client

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.get("")
async def list_plans(reg: PlanRegistry = Depends(get_plans),
                     limit: int = 20, session_id: str = "") -> dict:
    """Последние планы Supervisor (переживают переподключение WS)."""
    plans = reg.list(limit=limit, session_id=session_id) if reg else []
    return {"plans": [view_for_client(r) for r in plans]}


@router.get("/{plan_id}")
async def plan_detail(plan_id: str, reg: PlanRegistry = Depends(get_plans)) -> dict:
    """Состояние одного плана (в т.ч. после перезапуска сервера — с диска)."""
    rec = reg.get(plan_id) if reg else None
    if rec is None:
        raise HTTPException(404, "План не найден")
    return view_for_client(rec)
