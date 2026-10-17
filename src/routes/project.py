# -*- coding: utf-8 -*-
"""Проект (2 эндпоинта) — вынесены из src/main.py.

Sprint 1.B: GET /api/project, POST /api/project/set.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException

from src.app import BASE_DIR
from src.config import get_settings
from src.file_state import FileState
from src.runtime_config import get_project_root, set_project_root
from src.state import get_mcp, state
from src.workspace import ensure_workspace_dirs, is_allowed, workspace_roots

router = APIRouter(prefix="/api/project", tags=["project"])


@router.get("")
async def project_current() -> dict:
    s = get_settings()
    path = get_project_root(default=s.project_root)
    p = Path(path) if path else None
    return {
        "project_root": path,
        "exists": bool(p and p.exists() and p.is_dir()),
        "writable": bool(p and p.exists() and os.access(p, os.W_OK)),
        "workspace_roots": [str(r) for r in workspace_roots(s.project_root)],
        "enabled_servers": [],  # TODO: snap.enabled_mcp_ids (после 1.B в state.snapshot)
    }


@router.post("/set")
async def project_set(payload: dict = Body(...)) -> dict:
    raw = (payload.get("path") or "").strip()
    if not raw:
        raise HTTPException(400, "Пустой путь")
    p = Path(raw).expanduser()
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, f"Папка не существует: {p}")
    s = get_settings()
    if not is_allowed(p, s.project_root):
        roots = ", ".join(str(r) for r in workspace_roots(s.project_root))
        raise HTTPException(
            403,
            f"Папка вне белого списка WORKSPACE_ROOTS: {p}. "
            f"Разрешено: {roots or '—'}",
        )
    saved = set_project_root(str(p))
    ensure_workspace_dirs(saved)
    fs: FileState | None = state.file_state
    if fs:
        fs.root = Path(saved).resolve()
        fs.invalidate()
    return {"ok": True, "project_root": saved}


@router.post("/select-dialog")
async def project_select_dialog() -> dict:
    """Выбор папки из белого списка (браузер не умеет нативный диалог).

    Возвращает список доступных корней; UI подставляет их в подсказку
    поля ввода (datalist). Если список пуст — options: [].
    """
    s = get_settings()
    opts = [str(r) for r in workspace_roots(s.project_root) if r.is_dir()]
    return {"ok": True, "path": opts[0] if opts else "", "options": opts}
