# -*- coding: utf-8 -*-
"""Backup (4 эндпоинта) — вынесены из src/main.py.

Sprint 1.B: GET /api/backup/list, POST /api/backup/create,
POST /api/backup/restore, DELETE /api/backup/{name}.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException

from src.state import state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/list")
async def backup_list() -> dict:
    """Список доступных бэкапов (pg_dump)."""
    bm = state.backup_manager
    if bm is None:
        return {"backups": [], "enabled": False}
    return {"backups": bm.list_backups(), "enabled": True}


@router.post("/create")
async def backup_create() -> dict:
    """Создать новый бэкап сейчас."""
    bm = state.backup_manager
    if bm is None:
        raise HTTPException(503, "BackupManager not configured")
    name = await bm.create_backup()
    return {"ok": True, "backup": name}


@router.post("/restore")
async def backup_restore(payload: dict = Body(...)) -> dict:
    """Восстановить из бэкапа (требует подтверждения через policies)."""
    bm = state.backup_manager
    if bm is None:
        raise HTTPException(503, "BackupManager not configured")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    await bm.restore_backup(name)
    return {"ok": True, "restored": name}


@router.delete("/{name}")
async def backup_delete(name: str) -> dict:
    """Удалить бэкап по имени."""
    bm = state.backup_manager
    if bm is None:
        raise HTTPException(503, "BackupManager not configured")
    if not bm.delete_backup(name):
        raise HTTPException(404, f"Backup {name} not found")
    return {"ok": True, "deleted": name}
