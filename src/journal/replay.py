"""Движок воспроизведения (replay): воссоздание действий агентов.

Что это даёт:
  * развернуть те же изменения на чистой копии проекта (перенос, чин);
  * повторить действия после отката (частично);
  * показать пошаговый план «что агенты делали» — AI-читаемый.

Режимы:
  * dry_run (по умолчанию) — только план, ничего не выполняется;
  * execute — реальные вызовы MCPManager.call_tool по плану
    (опасные инструменты проходят approval gate как обычно).

План строится в топологическом порядке с сохранением контекста
(агент, задача, аргументы). События с reversible=0 по умолчанию
пропускаются (можно включить force, но это осознанный риск).
"""
from __future__ import annotations

import json
import time
from typing import Any

from .dependencies import DependencyGraph
from .schema import Event, EventKind, EventStatus
from .store import JournalStore


class ReplayEngine:
    def __init__(self, store: JournalStore):
        self.store = store
        self.graph = DependencyGraph(store)

    # ═════════════════════════════════════════════════════════
    # План
    # ═════════════════════════════════════════════════════════
    def build_plan(
        self,
        task_id: str = "",
        session_id: str = "",
        from_event: str = "",
        to_event: str = "",
        include_non_reversible: bool = False,
        limit: int = 1000,
    ) -> dict:
        """Строит упорядоченный план воспроизведения."""
        emap: dict[str, Event]
        if from_event or to_event:
            all_events = self.store.query(limit=limit * 4, order_desc=False)
            ids = {e.event_id for e in all_events}
            start = all_events[0].seq if not from_event or from_event not in ids else \
                next(e.seq for e in all_events if e.event_id == from_event)
            end = all_events[-1].seq if not to_event or to_event not in ids else \
                next(e.seq for e in all_events if e.event_id == to_event)
            emap = {e.event_id: e for e in all_events
                    if start <= e.seq <= end and e.kind == EventKind.TOOL_CALL}
        else:
            emap = {
                e.event_id: e for e in self.store.query(
                    task_id=task_id or None, session_id=session_id or None,
                    kind=EventKind.TOOL_CALL, limit=limit, order_desc=False)
            }
        if not emap:
            return {"ok": False, "error": "Событий для плана не найдено",
                    "steps": []}

        order = self.graph.topo_order(emap)
        steps: list[dict] = []
        skipped: list[dict] = []
        for eid in order:
            ev = emap[eid]
            payload = ev.args or {}
            step = {
                "n": len(steps) + 1,
                "event_id": ev.event_id,
                "server": ev.server_name,
                "tool": ev.tool_name,
                "args": payload,
                "agent": ev.agent_id,
                "task_id": ev.task_id,
                "original_status": ev.status,
                "reversible": ev.reversible,
            }
            if ev.status != EventStatus.OK:
                step["note"] = f"оригинал завершился со статусом {ev.status}"
            if ev.reversible == 0 and not include_non_reversible:
                skipped.append({**step, "reason": "необратимо/чтение"})
                continue
            steps.append(step)
        return {
            "ok": True,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "scope": {
                "task_id": task_id, "session_id": session_id,
                "from_event": from_event, "to_event": to_event,
            },
            "steps_count": len(steps),
            "skipped_count": len(skipped),
            "steps": steps,
            "skipped": skipped[:50],
        }

    # ═════════════════════════════════════════════════════════
    # Исполнение
    # ═════════════════════════════════════════════════════════
    async def execute(
        self,
        plan: dict,
        mcp_manager,
        approval_handler=None,
        session_id: str = "",
        journal=None,
        stop_on_error: bool = True,
        max_steps: int = 500,
    ) -> dict:
        """Выполняет план через реальный MCPManager (с журналированием).

        Каждый шаг — обычный tool call: он попадает в журнал как replay-событие
        (meta.replay=true), approval gate продолжает работать.
        """
        if not plan or not plan.get("ok"):
            return {"ok": False, "error": "План не готов"}
        if mcp_manager is None:
            return {"ok": False,
                    "error": "MCPManager недоступен (execute только в основном процессе)"}

        results: list[dict] = []
        executed = failed = skipped = 0

        replay_ev = Event(
            kind=EventKind.REPLAY, action="replay_start",
            session_id=session_id,
            meta={"steps": plan.get("steps_count", 0),
                  "scope": plan.get("scope", {})},
        )
        if journal:
            journal.record(replay_ev)
        else:
            self.store.insert(replay_ev)

        for step in plan.get("steps", [])[:max_steps]:
            qualified = f"{step['server']}__{step['tool']}"
            try:
                result = await mcp_manager.call_tool(
                    qualified, step.get("args") or {},
                    approval_handler=approval_handler,
                )
                ok = not str(result).lstrip().startswith(("Ошибка", "⛔"))
                executed += 1 if ok else 0
                failed += 0 if ok else 1
                results.append({
                    "n": step["n"], "tool": qualified, "ok": ok,
                    "result": str(result)[:500],
                })
                if not ok and stop_on_error:
                    break
            except Exception as e:
                failed += 1
                results.append({"n": step["n"], "tool": qualified,
                                "ok": False, "error": str(e)})
                if stop_on_error:
                    break

        end_ev = Event(
            kind=EventKind.REPLAY, action="replay_end",
            session_id=session_id,
            status=EventStatus.OK if failed == 0 else EventStatus.ERROR,
            meta={"executed": executed, "failed": failed,
                  "skipped": plan.get("skipped_count", 0)},
        )
        if journal:
            journal.record(end_ev)
        else:
            self.store.insert(end_ev)

        return {
            "ok": failed == 0,
            "executed": executed, "failed": failed,
            "results": results[:100],
        }

    # ═════════════════════════════════════════════════════════
    # Экспорт сценария (автономный Python-скрипт)
    # ═════════════════════════════════════════════════════════
    def export_script(self, plan: dict, path: Any) -> str:
        """Генерирует автономный файл плана (для архива/CI)."""
        from .utils import write_text_safe
        header = (
            "Автосгенерированный план воспроизведения действий агентов.\n"
            f"Создан: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Шагов: {plan.get('steps_count', 0)}\n"
            "Запуск: python -m src.journal.cli replay-execute <файл>\n"
        )
        body = json.dumps(plan, ensure_ascii=False, indent=2)
        p = str(path)
        write_text_safe(p, "# " + header.replace("\n", "\n# ") + "\n" + body + "\n")
        return p
