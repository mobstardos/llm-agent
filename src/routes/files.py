# -*- coding: utf-8 -*-
"""Вложения файлов в чат — upload/preview из .inbox рабочей папки.

Sprint «attachments»: POST /api/files/upload (multipart, лимиты,
whitelist расширений, анти-path-traversal), GET /api/files/preview.
Общие хелперы — src/attachments.py (их же использует ws/chat).
"""
from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from src.attachments import (
    MAX_FILES_PER_REQUEST,
    MAX_TOTAL_BYTES,
    inbox_dir,
    safe_resolve,
    sanitize_name,
    validate_file,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["files"])


@router.post("/upload")
async def upload_files(files: list[UploadFile] = File(...)) -> dict:
    """Сохранить вложения в <workspace>/.inbox/."""
    if not files:
        raise HTTPException(400, "Нет файлов")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(400,
                            f"Максимум {MAX_FILES_PER_REQUEST} файлов за раз")
    inbox = inbox_dir()
    if inbox is None:
        raise HTTPException(400, "Рабочая папка проекта не задана")
    try:
        inbox.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(500, f"Не удалось создать .inbox: {e}")

    saved: list[dict] = []
    total = 0
    for uf in files:
        raw_name = uf.filename or "file"
        data = await uf.read()
        err = validate_file(raw_name, len(data))
        if err:
            raise HTTPException(400, f"{raw_name}: {err}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(
                400,
                f"Суммарный лимит {MAX_TOTAL_BYTES // (1024 * 1024)} МБ")
        target = inbox / f"{uuid.uuid4().hex[:8]}_{sanitize_name(raw_name)}"
        try:
            target.write_bytes(data)
        except OSError as e:
            raise HTTPException(500, f"Не удалось сохранить {raw_name}: {e}")
        saved.append({
            "path": str(target.resolve()),
            "relpath": f".inbox/{target.name}",
            "name": target.name,
            "size": len(data),
            "mime": uf.content_type or "application/octet-stream",
        })
        logger.info("attachment saved: %s (%d bytes)", target.name, len(data))

    # Журнал (best-effort — не роняем upload из-за журнала)
    try:
        from src.state import state
        if state.journal is not None:
            state.journal.record_system(
                action="file_upload",
                meta={"files": [{"name": s["name"], "size": s["size"]}
                                for s in saved]},
            )
    except Exception:
        pass

    return {"ok": True, "files": saved}


@router.get("/preview")
async def preview(path: str) -> FileResponse:
    """Отдать файл из .inbox (превью картинок / скачивание)."""
    target = safe_resolve(path)
    if target is None or not target.is_file():
        raise HTTPException(404, "Файл не найден (доступны только файлы .inbox)")
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=mime, filename=target.name)
