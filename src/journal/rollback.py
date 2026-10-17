"""Движок отката: возврат проекта к состоянию «как было».

Принципы:
  * Откат выполняется в ОБРАТНОМ топологическом порядке (последние правки
    отменяются первыми) — как LIFO-стек.
  * Перед откатом создаётся теневая копия ТЕКУЩЕГО состояния
    (предохранительный трос: сам откат тоже откатываем).
  * Конфликт-детекция: если файл изменился после события (текущий хэш
    не равен after_hash), в safe-режиме файл пропускается с пометкой;
    force=True переопределяет.
  * Каждый откат сам записывается в журнал (событие rollback со списком
    отменённых событий) — полный аудит.
  * SQL-события (mysql/postgres) помечены как частично обратимые:
    они пропускаются с пометкой, если нет тривиальной инверсии.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .dependencies import DependencyGraph
from .schema import Event, EventKind, EventStatus
from .shadows import ShadowStore
from .store import JournalStore
from .utils import sha256_file


class RollbackEngine:
    def __init__(self, store: JournalStore, shadows: ShadowStore,
                 project_root: Path):
        self.store = store
        self.shadows = shadows
        self.root = Path(project_root)
        self.graph = DependencyGraph(store)

    # ═════════════════════════════════════════════════════════
    # Публичный API
    # ═════════════════════════════════════════════════════════
    def rollback_event(
        self, event_id: str, mode: str = "dry_run", force: bool = False,
    ) -> dict:
        """Откат одного события (последнего состояния файла)."""
        ev = self.store.get(event_id)
        if not ev:
            return {"ok": False, "error": f"Событие не найдено: {event_id}"}
        return self._rollback_events([ev], mode=mode, force=force)

    def rollback_task(
        self, task_id: str, mode: str = "dry_run", force: bool = False,
    ) -> dict:
        """Откат всех изменений задачи (в обратном порядке)."""
        events = self.store.query(
            task_id=task_id, only_mutating=True, limit=5000, order_desc=False)
        if not events:
            return {"ok": False, "error": f"Изменяющих событий задачи нет: {task_id}"}
        return self._rollback_events(events, mode=mode, force=force)

    def rollback_session(
        self, session_id: str, mode: str = "dry_run", force: bool = False,
    ) -> dict:
        events = self.store.query(
            session_id=session_id, only_mutating=True, limit=20000,
            order_desc=False)
        if not events:
            return {"ok": False, "error": f"Изменяющих событий сессии нет: {session_id}"}
        return self._rollback_events(events, mode=mode, force=force)

    def rollback_to_point(
        self, event_id: str, mode: str = "dry_run", force: bool = False,
    ) -> dict:
        """Откат ВСЕГО, что случилось ПОСЛЕ указанного события (включительно)."""
        anchor = self.store.get(event_id)
        if not anchor:
            return {"ok": False, "error": f"Событие не найдено: {event_id}"}
        events = self.store.query(
            since=anchor.ts, only_mutating=True, limit=20000, order_desc=False)
        events = [e for e in events if e.seq >= anchor.seq]
        if not events:
            return {"ok": False, "error": "После указанной точки изменений нет"}
        return self._rollback_events(events, mode=mode, force=force)

    # ═════════════════════════════════════════════════════════
    # Ядро
    # ═════════════════════════════════════════════════════════
    def _rollback_events(
        self, events: list[Event], mode: str, force: bool,
    ) -> dict:
        # 1. Топологический порядок, затем инвертируем (последний — первым)
        emap = {e.event_id: e for e in events}
        order = self.graph.topo_order(emap)
        plan: list[dict] = []
        executed: list[str] = []
        skipped: list[dict] = []
        errors: list[str] = []

        for eid in reversed(order):
            ev = emap[eid]
            step = self._plan_step(ev)
            plan.append(step)
            if step["action"] == "skip":
                skipped.append(step)
                continue

            if mode == "dry_run":
                continue

            # 2. Текущие хэши целей (конфликт-детекция)
            current_hashes = {}
            for t in ev.targets:
                p = (self.root / t)
                if p.is_file():
                    current_hashes[t] = sha256_file(p)

            # Предохранительный трос: теневая копия текущего состояния
            if self.shadows and ev.targets:
                rescue_id = f"pre-rollback-{ev.event_id[:12]}-{int(time.time())}"
                try:
                    self.shadows.save_after(
                        rescue_id,
                        {t: (self.root / t) for t in ev.targets},
                    )
                except Exception:
                    pass

            result = self.shadows.restore_before(
                ev.shadow_dir, self.root,
                force=force, current_hashes=current_hashes,
            ) if ev.shadow_dir else {
                "restored": [], "removed": [], "skipped": [], "errors": []
            }

            # Файлы, создававшиеся событием и не имеющие тени "before",
            # удаляются через inverse-спеку
            if not ev.shadow_dir and ev.inverse.get("op") == "delete_created":
                for t in ev.targets:
                    p = (self.root / t)
                    try:
                        if p.is_file():
                            p.unlink()
                            result["removed"].append(t)
                    except OSError as e:
                        result["errors"].append(f"{t}: {e}")

            if result["errors"]:
                errors.extend(f"{ev.event_id}: {e}" for e in result["errors"])
            if result["skipped"]:
                for s in result["skipped"]:
                    skipped.append({"event_id": ev.event_id, **s})
            if result["restored"] or result["removed"]:
                executed.append(ev.event_id)
                step["executed"] = {
                    "restored": result["restored"],
                    "removed": result["removed"],
                }

        # 3. Аудит отката в журнал
        audit: dict | None = None
        if mode == "execute":
            audit_ev = Event(
                kind=EventKind.ROLLBACK,
                action="rollback_execute",
                status=(EventStatus.OK if not errors
                        else EventStatus.CONFLICT if executed else EventStatus.ERROR),
                targets=sorted({t for ev in events for t in ev.targets}),
                meta={
                    "rolled_back_events": executed,
                    "skipped": skipped[:100],
                    "errors": errors[:100],
                    "scope": events[0].task_id and f"task:{events[0].task_id}" or "",
                    "count": len(events),
                },
            )
            # Пишет аудит отката напрямую в store (без контекста рекордера)
            self.store.insert(audit_ev)
            audit = {"event_id": audit_ev.event_id}

        return {
            "ok": not errors,
            "mode": mode,
            "planned": len(plan),
            "executed_count": len(executed),
            "executed_events": executed,
            "skipped": skipped,
            "errors": errors,
            "plan": plan if mode == "dry_run" else plan[:20],
            "audit": audit,
        }

    def _plan_step(self, ev: Event) -> dict:
        """Описание шага отката для плана/отчёта."""
        base = {
            "event_id": ev.event_id,
            "seq": ev.seq,
            "time": ev.iso_time,
            "server": ev.server_name,
            "tool": ev.tool_name,
            "targets": ev.targets,
            "status": ev.status,
        }
        if ev.status != "ok":
            return {**base, "action": "skip", "reason": f"статус события: {ev.status}"}
        if ev.reversible == 0:
            return {**base, "action": "skip",
                    "reason": "событие необратимо (reversible=0)"}
        if not ev.shadow_dir:
            inv = ev.inverse.get("op")
            if inv == "delete_created":
                return {**base, "action": "delete_created"}
            return {**base, "action": "skip",
                    "reason": "нет теней (частичная обратимость)"}
        manifest = self.shadows.read_manifest(ev.shadow_dir, "before")
        if not manifest:
            # тени могли быть сжаты ретенцией
            archive = self.cfg_archive_for(ev.shadow_dir)
            if archive:
                return {**base, "action": "skip",
                        "reason": f"тени сжаты в архив {archive.name}; распакуйте для отката"}
            return {**base, "action": "skip", "reason": "манифест теней не найден"}
        return {**base, "action": "restore_shadow", "shadow_dir": ev.shadow_dir}

    def cfg_archive_for(self, shadow_dir: str):
        day = shadow_dir.split("/")[0] if "/" in shadow_dir else ""
        if not day:
            return None
        p = self.shadows.root.parent / "archives" / f"shadows-{day}.zip"
        return p if p.exists() else None

    # ═════════════════════════════════════════════════════════
    # Проверка целостности (для UI и статистики)
    # ═════════════════════════════════════════════════════════
    def verify_targets(self, task_id: str = "", session_id: str = "",
                       limit: int = 500) -> dict:
        """Сверяет текущие хэши файлов с after_hash последних событий."""
        events = self.store.query(
            task_id=task_id or None, session_id=session_id or None,
            only_mutating=True, limit=limit, order_desc=True)
        seen: set[str] = set()
        current: dict[str, str] = {}
        drifted: list[dict] = []
        for ev in events:
            for t in ev.targets:
                if t in seen:
                    continue
                seen.add(t)
                p = self.root / t
                cur = sha256_file(p) if p.is_file() else ""
                current[t] = cur
                if ev.after_hash and cur and cur != ev.after_hash.split(",")[0]:
                    drifted.append({
                        "target": t,
                        "last_event": ev.event_id,
                        "expected": ev.after_hash.split(",")[0][:12],
                        "current": cur[:12],
                        "note": "файл изменён после последнего события",
                    })
        return {
            "checked": len(seen),
            "drifted": drifted,
            "intact": len(seen) - len(drifted),
        }
