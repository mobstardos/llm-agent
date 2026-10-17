"""Реестр планов + долгая память — mixin Supervisor'а (Этап 3, V2 §3.4).

Выделено из supervisor.py в рамках рефакторинга v2: методы работают с
PlanRegistry (REST /api/plans) и Memory (log_event_async / search_events_async)
исключительно через self — класс Supervisor наследует mixin и сохраняет
прежний публичный API.

Все обращения к реестру «мягкие»: отсутствие реестра или любая ошибка
не должны ломать исполнение плана.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PlanRegistryMixin:
    """Хуки реестра планов + контекст долгой памяти для планировщика."""

    # ═══════════════════════════════════════════════════════
    # Реестр планов (Этап 3)
    # ═══════════════════════════════════════════════════════
    @staticmethod
    def _reg_mode(dag_mode: bool) -> str:
        return "dag" if dag_mode else "sequential"

    def _reg_create(self, query: str, plan: Any, session: Any,
                    needs_approval: bool, mode: str = "sequential") -> None:
        """Регистрирует план в реестре (мягко: реестр может отсутствовать)."""
        if getattr(self, "_reg", None) is None or plan is None or not plan.steps:
            return
        try:
            self._reg.create(
                self._plan_id,
                session_id=(getattr(session, "id", "")
                            if session is not None else ""),
                query=query, intent=plan.intent, mode=mode,
                needs_approval=needs_approval,
                steps=[{
                    "id": s.id, "agent": s.agent, "task": s.task,
                    "status": "pending", "depends_on": list(s.depends_on),
                } for s in plan.steps],
            )
        except Exception:
            logger.debug("plan registry create failed (пропускаю)",
                         exc_info=True)

    def _reg_update(self, **fields) -> None:
        if getattr(self, "_reg", None) is None:
            return
        try:
            self._reg.update(self._plan_id, **fields)
        except Exception:
            logger.debug("plan registry update failed (пропускаю)",
                         exc_info=True)

    def _reg_step(self, step_id: str, **fields) -> None:
        if getattr(self, "_reg", None) is None:
            return
        try:
            self._reg.set_step(self._plan_id, step_id, **fields)
        except Exception:
            logger.debug("plan registry step update failed (пропускаю)",
                         exc_info=True)

    # ═══════════════════════════════════════════════════════
    # Долгая память (Этап 3, V2 §3.4)
    # ═══════════════════════════════════════════════════════
    async def _long_memory_context(self, query: str, memory: Any) -> str:
        """Релевантные события из долгой памяти — вход планировщика (V2 §3.4).

        Любая ошибка/отсутствие памяти → пустая строка (планирование
        продолжает работать без неё).
        """
        if memory is None or not query:
            return ""
        if not getattr(memory, "enabled", False):
            return ""
        search = getattr(memory, "search_events_async", None)
        if search is None:
            return ""
        try:
            found = await search(query, limit=3, days_back=30)
        except Exception:
            return ""
        lines: list[str] = []
        for ev in found or []:
            summary = str(ev.get("summary", "") if isinstance(ev, dict)
                          else getattr(ev, "summary", "")).strip()
            if summary:
                lines.append(f"- {summary[:200]}")
        if not lines:
            return ""
        return ("Релевантные события из прошлых сессий (долгая память):\n"
                + "\n".join(lines))
