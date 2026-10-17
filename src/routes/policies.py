# -*- coding: utf-8 -*-
"""Policies (12 эндпоинтов) — вынесены из src/main.py.

Sprint 1.B: GET /api/policies, DELETE /api/policies/{policy_id},
GET /api/policies/metrics, POST /api/policies/clear,
POST /api/policies/cleanup, POST /api/policies/backup,
GET /api/policies/export, POST /api/policies/import/preview,
POST /api/policies/import.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from src.config import get_settings
from src.policies import PolicyStore
from src.state import get_policies

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/policies", tags=["policies"])


# ─── Helpers ────────────────────────────────────────────────────────
def _parse_policies_file(raw: bytes) -> list[dict]:
    """Парсинг экспорт-файла политик; ошибка формы — HTTP 400."""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise HTTPException(400, "Файл не читается как JSON")
    items = data.get("policies") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise HTTPException(400, "В файле нет массива policies")
    return items


def _classify_policy(item: dict, existing: list[dict]) -> str:
    """new | conflict | invalid — для превью импорта."""
    if not isinstance(item, dict):
        return "invalid"
    tool = str(item.get("tool") or "").strip()
    scope = str(item.get("scope") or "").strip()
    decision = str(item.get("decision") or "").strip().lower()
    if not tool or scope not in ("tool", "path") \
            or decision not in ("allow", "deny"):
        return "invalid"
    pattern = item.get("path_pattern")
    if scope == "path" and not (pattern or "").strip():
        return "invalid"
    for p in existing:
        if (p.get("tool") == tool and p.get("scope") == scope
                and (p.get("path_pattern") or "") == (pattern or "")
                and p.get("decision") == decision):
            return "conflict"
    return "new"


# ─── Endpoints ───────────────────────────────────────────────────────
@router.get("")
async def policies_list(st: PolicyStore = Depends(get_policies)) -> dict:
    return {"policies": st.list_all(), "stats": st.stats()}


@router.delete("/{policy_id}")
async def policies_delete(policy_id: int,
                          st: PolicyStore = Depends(get_policies)) -> dict:
    if not st.delete(policy_id):
        raise HTTPException(404, f"Политика #{policy_id} не найдена")
    return {"ok": True}


@router.get("/metrics")
async def policies_metrics(top: int = 10,
                           st: PolicyStore = Depends(get_policies)) -> dict:
    rows = st.list_all()
    top_active = sorted(
        rows, key=lambda p: (-(p.get("use_count") or 0),
                             -(p.get("last_used_at") or 0)),
    )[:max(1, top)]
    deleted_overall = {"count": 0}
    try:
        with sqlite3.connect(st.path) as conn:
            deleted_overall["count"] = conn.execute(
                "SELECT COUNT(*) FROM deleted_policies").fetchone()[0]
    except Exception:
        pass
    return {"top_active": top_active, "deleted_overall": deleted_overall}


@router.post("/clear")
async def policies_clear(st: PolicyStore = Depends(get_policies)) -> dict:
    removed = st.clear_all()
    return {"ok": True, "removed": removed}


@router.post("/cleanup")
async def policies_cleanup(st: PolicyStore = Depends(get_policies)) -> dict:
    removed = st.cleanup_old()
    return {"ok": True, "removed": removed}


@router.post("/backup")
async def policies_backup(st: PolicyStore = Depends(get_policies)) -> dict:
    s = get_settings()
    path = st.backup_timestamped(
        s.policies.backup_dir, keep=s.policies.backup_keep,
    )
    return {"ok": True, "backup_path": str(path)}


@router.get("/export")
async def policies_export(st: PolicyStore = Depends(get_policies)) -> JSONResponse:
    """Выгрузка всех политик в JSON (скачивается как файл)."""
    data = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(st.list_all()),
        "policies": st.list_all(),
    }
    return JSONResponse(
        data,
        headers={
            "Content-Disposition": 'attachment; filename="policies-export.json"',
        },
    )


@router.post("/import/preview")
async def policies_import_preview(request: Request, mode: str = "merge",
                                  st: PolicyStore = Depends(get_policies)) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        raise HTTPException(400, "Нет файла в form-data (поле 'file')")
    items = _parse_policies_file(await upload.read())
    existing = st.list_all()
    details: dict[str, list] = {"new": [], "conflicts": [], "invalid": []}
    for item in items:
        cls = _classify_policy(item, existing)
        details["conflicts" if cls == "conflict" else cls].append(
            item if isinstance(item, dict) else {})
    added = len(details["new"])
    if mode == "overwrite":
        added = sum(
            1 for it in items
            if isinstance(it, dict)
            and str(it.get("tool") or "").strip()
            and str(it.get("decision") or "").lower() in ("allow", "deny")
        )
    return {
        "added": added,
        "skipped": len(details["conflicts"]) if mode == "merge" else 0,
        "details": details,
        "mode": mode,
    }


@router.post("/import")
async def policies_import(request: Request, mode: str = "merge",
                          st: PolicyStore = Depends(get_policies)) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        raise HTTPException(400, "Нет файла в form-data (поле 'file')")
    items = _parse_policies_file(await upload.read())
    existing = st.list_all()
    added = skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        tool = str(item.get("tool") or "").strip()
        scope = str(item.get("scope") or "").strip()
        decision = str(item.get("decision") or "").strip().lower()
        pattern = (item.get("path_pattern") or "").strip() or None
        if not tool or scope not in ("tool", "path") \
                or decision not in ("allow", "deny") \
                or (scope == "path" and not pattern):
            skipped += 1
            continue
        cls = _classify_policy(item, existing)
        if cls == "conflict" and mode == "merge":
            skipped += 1
            continue
        st.add(tool, scope, decision, pattern)
        existing.append({
            "tool": tool, "scope": scope,
            "path_pattern": pattern, "decision": decision,
        })
        added += 1
    return {"ok": True, "added": added, "skipped": skipped, "mode": mode}
