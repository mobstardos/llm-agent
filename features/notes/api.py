"""API фичи «Заметки» — эталонный пример для Feature SDK (Этап 4).

Контракт: create_router() -> fastapi.APIRouter. FeatureLoader вызывает
его и делает app.include_router() — без правок src/main.py.

Хранилище — data/notes.json (stdlib), не требует БД.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from fastapi import Body, HTTPException
from fastapi.responses import JSONResponse

try:
    from src import events   # события шины (Этап 4) — необязательны
except Exception:            # фича должна работать и вне проекта
    events = None

_BASE = Path(__file__).resolve().parents[2]
_NOTES_FILE = _BASE / "data" / "notes.json"


def _load() -> list[dict]:
    try:
        return json.loads(_NOTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(notes: list[dict]) -> None:
    _NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _NOTES_FILE.write_text(
        json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")


def create_router():
    from fastapi import APIRouter

    router = APIRouter(prefix="/api/notes", tags=["feature:notes"])

    @router.get("")
    async def list_notes():
        return {"count": len(_load()), "notes": _load()}

    @router.post("")
    async def add_note(text: str = Body(..., embed=True)):
        text = (text or "").strip()
        if not text:
            raise HTTPException(422, "Пустой текст заметки")
        notes = _load()
        note = {
            "id": uuid.uuid4().hex[:8],
            "text": text[:2000],
            "created_at": time.time(),
        }
        notes.insert(0, note)
        _save(notes[:500])
        if events is not None:
            try:
                await events.publish("notes.changed", {
                    "action": "add", "id": note["id"],
                })
            except Exception:
                pass
        return JSONResponse({"ok": True, "note": note}, status_code=201)

    @router.delete("/{note_id}")
    async def delete_note(note_id: str):
        notes = _load()
        rest = [n for n in notes if n.get("id") != note_id]
        if len(rest) == len(notes):
            raise HTTPException(404, "Заметка не найдена")
        _save(rest)
        if events is not None:
            try:
                await events.publish("notes.changed", {
                    "action": "delete", "id": note_id,
                })
            except Exception:
                pass
        return {"ok": True}

    return router
