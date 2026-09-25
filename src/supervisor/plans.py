"""Реестр планов Supervisor (Этап 3, ARCHITECTURE-V2 §4.5).

План должен переживать переподключение WS: состояние каждого плана
пишется в data/plans/<plan_id>.json (best-effort) и читается через
REST GET /api/plans/{plan_id} и GET /api/plans?session_id=...,
когда клиент вернулся онлайн.

Только stdlib; падение дампа никогда не ломает выполнение плана
(мягкие хуки, как в ConversationSession).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class PlanRegistry:
    """Хранит состояние планов в памяти + мягкий дамп на диск."""

    def __init__(self, dump_dir: str | Path | None = None,
                 keep_in_memory: int = 200):
        self.dump_dir = Path(dump_dir) if dump_dir else None
        self._plans: dict[str, dict] = {}
        self._order: list[str] = []          # порядок создания (FIFO-обрезка)
        self._keep = keep_in_memory

    # ═══════════════════════════════════════════════════════
    # Создание / обновление
    # ═══════════════════════════════════════════════════════
    def create(self, plan_id: str, *, session_id: str = "", query: str = "",
               intent: str = "", mode: str = "sequential",
               steps: list[dict] | None = None,
               needs_approval: bool = False) -> dict:
        record = {
            "plan_id": plan_id,
            "session_id": session_id or "",
            "query": (query or "")[:2000],
            "intent": intent or "",
            "mode": mode,                     # sequential | dag
            "status": "awaiting_approval" if needs_approval else "running",
            "needs_approval": bool(needs_approval),
            "success": None,
            "replans": 0,
            "steps": [dict(s) for s in (steps or [])],
            "message": "",
            "error": "",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._plans[plan_id] = record
        self._order.append(plan_id)
        while len(self._order) > self._keep:
            old = self._order.pop(0)
            self._plans.pop(old, None)
        self._dump(record)
        return record

    def update(self, plan_id: str, **fields) -> dict | None:
        rec = self._plans.get(plan_id)
        if rec is None:
            return None
        rec.update(fields)
        rec["updated_at"] = time.time()
        self._dump(rec)
        return rec

    def set_step(self, plan_id: str, step_id: str, **fields) -> dict | None:
        """Обновляет один шаг плана (находит по id, добавляет при отсутствии)."""
        rec = self._plans.get(plan_id)
        if rec is None:
            return None
        for st in rec["steps"]:
            if str(st.get("id")) == str(step_id):
                st.update(fields)
                rec["updated_at"] = time.time()
                self._dump(rec)
                return st
        st = {"id": str(step_id), **fields}
        rec["steps"].append(st)
        rec["updated_at"] = time.time()
        self._dump(rec)
        return st

    # ═══════════════════════════════════════════════════════
    # Чтение
    # ═══════════════════════════════════════════════════════
    def get(self, plan_id: str) -> dict | None:
        rec = self._plans.get(plan_id)
        if rec is not None:
            return rec
        return self._load_file(plan_id)   # после перезапуска сервера

    def list(self, limit: int = 20, session_id: str = "") -> list[dict]:
        """Последние планы (новые сверху); фильтр по session_id опционален.

        Подмешивает планы с диска, которых нет в памяти (после рестарта).
        """
        out = [r for r in self._plans.values()
               if not session_id or r.get("session_id") == session_id]
        out.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        seen = {r["plan_id"] for r in out}
        if self.dump_dir is not None and self.dump_dir.exists():
            try:
                files = sorted(self.dump_dir.glob("*.json"),
                               key=lambda p: p.stat().st_mtime,
                               reverse=True)[: self._keep]
            except Exception:
                files = []
            for p in files:
                rec = self._load_file(p.stem)
                if rec is None or rec["plan_id"] in seen:
                    continue
                if session_id and rec.get("session_id") != session_id:
                    continue
                out.append(rec)
                seen.add(rec["plan_id"])
        out.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return out[: max(0, limit)]

    def for_session(self, session_id: str) -> dict | None:
        """Последний план сессии (для восстановления UI после reconnect)."""
        if not session_id:
            return None
        plans = self.list(limit=1, session_id=session_id)
        return plans[0] if plans else None

    # ═══════════════════════════════════════════════════════
    # Дамп (json, мягкий)
    # ═══════════════════════════════════════════════════════
    def _dump(self, rec: dict) -> None:
        if self.dump_dir is None:
            return
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            path = self.dump_dir / f"{rec['plan_id']}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
        except Exception:
            logger.debug("Plan dump failed (пропускаю)", exc_info=True)

    def _load_file(self, plan_id: str) -> dict | None:
        if self.dump_dir is None:
            return None
        path = self.dump_dir / f"{plan_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("plan_id"):
                self._plans[data["plan_id"]] = data
                return data
        except Exception:
            logger.debug("Plan load failed: %s", plan_id, exc_info=True)
        return None


def view_for_client(rec: dict) -> dict:
    """Срез состояния плана для клиента (без лишних деталей)."""
    return {
        "plan_id": rec.get("plan_id", ""),
        "session_id": rec.get("session_id", ""),
        "intent": rec.get("intent", ""),
        "mode": rec.get("mode", "sequential"),
        "status": rec.get("status", ""),
        "success": rec.get("success"),
        "replans": rec.get("replans", 0),
        "steps": [
            {
                "id": st.get("id", ""),
                "agent": st.get("agent", ""),
                "task": st.get("task", ""),
                "status": st.get("status", "pending"),
                "summary": (st.get("summary") or "")[:200],
                "error": (st.get("error") or "")[:200],
                "wave": st.get("wave"),
            }
            for st in rec.get("steps", [])
        ],
        "message": rec.get("message", ""),
        "created_at": rec.get("created_at", 0),
        "updated_at": rec.get("updated_at", 0),
    }
