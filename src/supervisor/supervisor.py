"""Supervisor — Plan → Execute → Observe → Re-plan (Этапы 2–3, V2 §3.2).

Ключевое отличие от Orchestrator.handle(): решение о следующем действии
принимается по контексту ответа агента (успех/ошибка/содержимое) и по
контексту диалога (история сессии), а не одним статическим выбором.

Переиспользует существующее ядро: BaseAgent.run() через публичный
Orchestrator.run_agent() (бюджеты из agent.yaml, journal-хуки,
изоляция падений) и карточки агентов из реестра.

Все WS-события — аддитивные (V2 §3.8):
  plan / step_start / token(step_id) / step_done / replan / plan_done
  и plan_approval (через обработчик в main.py).

Этап 3 (V2 §3.10 «Параллелизм + память»):
- DAG-режим: шаги с явными depends_on выполняются волнами, независимые
  шаги волны — параллельно (asyncio.gather + семафор). Планы без
  depends_on исполняются последовательно, как в Этапе 2 (регрессия);
- долгая память: итоги планов уходят в Memory (log_event_async),
  планировщик получает релевантные события прошлых сессий;
- реестр планов (PlanRegistry): состояние пишется в data/plans/*.json
  и доступно через REST /api/plans после переподключения WS;
- события публикуются в шину src/events.py (Этап 4, §2.4).

Бюджеты поверх агентских (V2 §3.7): max_steps, max_replans, wall-clock.

РЕФАКТОРИНГ v2 (декомпозиция монолита):
  supervisor.py (этот файл) — главный цикл run() и общие helpers;
  planning.py               — PlanningMixin:     PLAN (_make_plan и др.);
  registry.py               — PlanRegistryMixin: реестр планов + память;
  execution.py              — ExecutionMixin:    EXECUTE/DAG (_run_dag...);
  observe.py                — ObservationMixin:  OBSERVE + синтез.
Публичный API не изменился: from src.supervisor.supervisor import Supervisor.
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Any

from src import events
from src.supervisor.dag import is_dag_plan
from src.supervisor.models import (
    EmitFn,
    ObserveDecision,      # noqa: F401 — реэкспорт для совместимости
    Plan,
    PlanStep,             # noqa: F401 — реэкспорт для совместимости
    parse_observe,        # noqa: F401 — реэкспорт для совместимости
    parse_plan,           # noqa: F401 — реэкспорт для совместимости
)
from src.supervisor.planning import PlanningMixin
from src.supervisor.registry import PlanRegistryMixin
from src.supervisor.observe import ObservationMixin
from src.supervisor.execution import ExecutionMixin

logger = logging.getLogger(__name__)

# мягкий journal-хук — тот же, что в handle()
from src.orchestrator import _journal  # noqa: E402


def _analytics():
    """Мягкий хук route-аналитики (Этап 5): None — если модуль недоступен."""
    try:
        from src import route_analytics
        return route_analytics
    except Exception:
        return None


class Supervisor(PlanningMixin, PlanRegistryMixin, ObservationMixin,
                 ExecutionMixin):
    """Оркестратор плана: составляет, исполняет, наблюдает, перепланирует."""

    #: маркер плана-деградации (LLM вернул мусор вместо JSON)
    UNPARSED_INTENT = "__unparsed__"

    #: максимум шагов одного плана
    MAX_STEPS = int(os.getenv("SUPERVISOR_MAX_STEPS", "12"))
    #: максимум перепланировок на один запрос
    MAX_REPLANS = int(os.getenv("SUPERVISOR_MAX_REPLANS", "2"))
    #: стенка на весь план (секунды), поверх агентских таймаутов
    MAX_SECONDS = float(os.getenv("SUPERVISOR_MAX_SECONDS", "1800"))
    #: обрезка результатов шага в промптах
    DETAIL_CHARS = 1200
    #: максимум шагов, выполняемых одновременно в волне DAG-режима (Этап 3)
    MAX_CONCURRENT = int(os.getenv("SUPERVISOR_MAX_CONCURRENT", "3"))

    def __init__(self, orchestrator: Any):
        self.orch = orchestrator
        self.llm = orchestrator.llm
        self.runtime = orchestrator.runtime
        self.events: list[dict] = []   # собирается, если emit не передан
        # контекст текущего прогона (Этап 3): реестр планов + id плана
        self._reg: Any = None
        self._plan_id: str = ""

    # ═══════════════════════════════════════════════════════
    # Главный цикл
    # ═══════════════════════════════════════════════════════
    async def run(
        self,
        query: str,
        *,
        model: str | None = None,
        session: Any = None,
        snapshot_prompt: str | None = None,
        suggested_agents: list[str] | None = None,
        intent_source: str = "low_confidence",
        intent_reason: str = "",
        llm_client: Any = None,
        mcp_manager: Any = None,
        memory: Any = None,
        file_state: Any = None,
        emit: EmitFn | None = None,
        approval_handler: Any = None,
        plan_approval_handler: Any = None,
        plan_registry: Any = None,
        fallback_answer: Any = None,
    ) -> dict:
        """Полный цикл Supervisor для одного сообщения пользователя.

        Возвращает {success, plan, results, message, replans, trace_id,
        plan_id, agents} — для bookkeeping'а сессии в main.py.
        """
        self.events.clear()
        self._reg = plan_registry          # Этап 3: состояние планов для REST
        send = emit or self._collect
        trace_id = uuid.uuid4().hex[:16]
        self._plan_id = trace_id
        llm = llm_client or self.llm

        # ── journal: начало задачи (мягкий хук) ──
        jint = _journal()
        task = {"task_id": trace_id, "trace_id": trace_id}
        if jint is not None:
            try:
                task = await jint.journal_task_start(
                    query=query, agents=list(suggested_agents or []),
                    model=model or "", session_id=getattr(session, "id", ""),
                    trace_id=trace_id,
                )
            except Exception:
                logger.exception("journal task_start (пропускаю)")

        replans = 0
        results: list[dict] = []
        plan: Plan | None = None
        final_message = ""
        success = False
        aborted = False   # остановка по бюджету (шаги/время/re-plan)
        t0 = time.monotonic()
        try:
            # ── PLAN ────────────────────────────────────────
            await send({"type": "status", "text": "Составляю план..."})
            plan = await self._make_plan(
                query, model=model, llm=llm, session=session,
                snapshot_prompt=snapshot_prompt,
                suggested_agents=suggested_agents,
                intent_source=intent_source, intent_reason=intent_reason,
                memory=memory,
            )

            if plan is None:
                # планировщик недоступен (LLM упал) — понятное сообщение
                final_message = (
                    "Не удалось составить план: провайдер LLM недоступен. "
                    "Проверьте настройку: python first_run.py --check-llm"
                )
                await send({"type": "message", "role": "assistant",
                            "content": final_message})
                await send({"type": "plan_done", "success": False,
                            "replans": 0, "steps_done": 0, "steps_total": 0})
                return self._result(False, None, [], final_message, 0,
                                    trace_id, [])
            # события: план + совместимый route
            step_views = [self._step_view(s) for s in plan.steps]
            await send({
                "type": "plan", "plan_id": trace_id, "intent": plan.intent,
                "needs_approval": plan.needs_approval, "steps": step_views,
            })
            await send({
                "type": "route",
                "agents": list(dict.fromkeys(s.agent for s in plan.steps)),
                "reason": f"[Supervisor] план из {len(plan.steps)} шагов"
                          + (f" · {plan.intent}" if plan.intent else ""),
            })

            # Этап 5: аналитика — реальное решение планировщика
            _ra = _analytics()
            if _ra is not None:
                try:
                    _ra.log_decision(
                        query=query, source="supervisor.plan",
                        agents=list(dict.fromkeys(s.agent for s in plan.steps)),
                        reason=(f"план из {len(plan.steps)} шагов"
                                + (f" · {plan.intent}" if plan.intent else "")),
                        session_id=getattr(session, "id", "") or "",
                        suggested=list(suggested_agents or []),
                    )
                except Exception:
                    pass

            # пустой план → прямой ответ (болтовня/вопросы без агентов)
            if not plan.steps:
                final_message = plan.reply or ""
                if not final_message and fallback_answer is not None:
                    try:
                        final_message = await fallback_answer(query)
                    except Exception:
                        logger.exception("fallback_answer failed")
                if not final_message:
                    final_message = "Нет подходящего агента."
                # деградация (мусор вместо JSON) — честный failure
                ok_empty = plan.intent != self.UNPARSED_INTENT
                await send({"type": "message", "role": "assistant",
                            "content": final_message})
                await send({"type": "plan_done", "success": ok_empty,
                            "replans": 0, "steps_done": 0, "steps_total": 0})
                if session is not None:
                    session.add_assistant(final_message)
                if memory is not None and ok_empty:
                    try:
                        memory.record_assistant_message(final_message, tokens=0)
                    except Exception:
                        pass
                return self._result(ok_empty, plan, [], final_message, 0,
                                    trace_id, [])

            # утверждение плана (V2 §3.7: пользовательский контроль)
            needs_approval = plan.needs_approval or self._has_destructive(plan)
            if needs_approval and plan_approval_handler is not None:
                self._reg_create(query, plan, session, needs_approval=True)
                await events.publish("plan.created", {
                    "plan_id": trace_id, "intent": plan.intent,
                    "steps": len(plan.steps),
                    "status": "awaiting_approval",
                })
                await send({"type": "status", "text": "Жду утверждения плана..."})
                approved = False
                try:
                    approved = bool(await plan_approval_handler({
                        "plan_id": trace_id, "intent": plan.intent,
                        "steps": step_views,
                    }))
                except Exception:
                    logger.exception("plan_approval_handler (считаю отказом)")
                if not approved:
                    final_message = ("Выполнение остановлено: план не "
                                     "утверждён.")
                    self._reg_update(status="cancelled", success=False,
                                     message=final_message)
                    await events.publish("plan.finished", {
                        "plan_id": trace_id, "success": False,
                        "status": "cancelled",
                    })
                    await send({"type": "message", "role": "assistant",
                                "content": final_message})
                    await send({"type": "plan_done", "success": False,
                                "replans": 0, "steps_done": 0,
                                "steps_total": len(plan.steps)})
                    return self._result(False, plan, [], final_message, 0,
                                        trace_id, [])

            # фиксирование плана в сессии
            if session is not None:
                session.set_active_plan({
                    "intent": plan.intent,
                    "steps": [{"id": s.id, "agent": s.agent, "task": s.task,
                               "status": "pending"} for s in plan.steps],
                })

            # Этап 3: реестр планов (состояние для REST /api/plans)
            dag_mode = is_dag_plan(plan.steps)
            self._reg_create(query, plan, session, needs_approval=False,
                             mode="dag" if dag_mode else "sequential")
            await events.publish("plan.created", {
                "plan_id": trace_id, "intent": plan.intent,
                "steps": len(plan.steps), "mode": self._reg_mode(dag_mode),
            })

            # ── EXECUTE + OBSERVE ───────────────────────────
            replans = 0
            results: list[dict] = []
            aborted = False   # остановка по бюджету/решению наблюдателя
            if dag_mode:
                replans, aborted, final_message = await self._run_dag(
                    query, plan, results=results, model=model, llm=llm,
                    session=session, mcp_manager=mcp_manager, memory=memory,
                    file_state=file_state, send=send,
                    approval_handler=approval_handler, trace_id=trace_id,
                )
            else:
                queue: list[PlanStep] = list(plan.steps)
                while queue:
                    # бюджет стенки
                    if time.monotonic() - t0 > self.MAX_SECONDS:
                        final_message = ("Остановлено: превышен лимит времени "
                                         f"плана ({self.MAX_SECONDS:.0f} с).")
                        aborted = True
                        break
                    # бюджет шагов (после re-plan очередь может расти)
                    if len(results) >= self.MAX_STEPS:
                        final_message = (f"Остановлено: достигнут лимит шагов "
                                         f"плана ({self.MAX_STEPS}).")
                        aborted = True
                        break

                    step = queue.pop(0)
                    entry = await self._execute_step(
                        step, done=results, model=model, llm=llm,
                        session=session, mcp_manager=mcp_manager,
                        memory=memory, file_state=file_state, send=send,
                        approval_handler=approval_handler, trace_id=trace_id,
                    )
                    results.append(entry)

                    # ── OBSERVE: решение по контексту ответа агента ──
                    decision = await self._observe(
                        query, step, entry, queue, results, llm=llm,
                    )
                    if decision.action == "continue":
                        continue

                    if decision.action == "replan" and decision.updated_plan \
                            and decision.updated_plan.steps:
                        if replans >= self.MAX_REPLANS:
                            final_message = (
                                "Остановлено: достигнут лимит перепланировок "
                                f"({self.MAX_REPLANS}). Последняя ошибка: "
                                f"{entry.get('error') or 'шаг не выполнен'}"
                            )
                            aborted = True
                            break
                        replans += 1
                        new_steps = self._filter_known(decision.updated_plan.steps)
                        # уникальные id шагов re-plan: не конфликтуют с упавшими
                        for s in new_steps:
                            s.id = f"r{replans}-{s.id}"
                        self._reg_update(replans=replans)
                        await events.publish("plan.replan", {
                            "plan_id": trace_id, "attempt": replans,
                            "reason": decision.message_to_user or "",
                        })
                        await send({
                            "type": "replan", "attempt": replans,
                            "reason": decision.message_to_user
                                      or f"шаг {step.id} упал — строю новый план",
                            "steps": [self._step_view(s) for s in new_steps],
                        })
                        queue = new_steps
                        if session is not None and session.active_plan:
                            for s in new_steps:
                                session.active_plan["steps"].append(
                                    {"id": s.id, "agent": s.agent, "task": s.task,
                                     "status": "pending"})
                        continue

                    if decision.action == "ask_user":
                        final_message = decision.message_to_user or \
                            "Нужно уточнение: продолжить?"
                        await send({"type": "message", "role": "assistant",
                                    "content": final_message})
                        await send({"type": "plan_done", "success": False,
                                    "replans": replans,
                                    "steps_done": sum(r["success"] for r in results),
                                    "steps_total": len(results)})
                        self._reg_update(status="error", success=False,
                                         message=final_message, replans=replans)
                        return self._result(False, plan, results, final_message,
                                            replans, trace_id,
                                            self._agents_of(results))

                    # finish
                    final_message = decision.message_to_user or (
                        entry.get("error") or "Шаг не выполнен."
                    )
                    break

            # ── СИНТЕЗ ИТОГА ────────────────────────────────
            success = self._overall_success(results, aborted)
            if not final_message:
                if not results:
                    if fallback_answer is not None:
                        try:
                            final_message = await fallback_answer(query)
                        except Exception:
                            logger.exception("fallback_answer failed")
                    if not final_message:
                        final_message = "Нет подходящего агента."
                else:
                    final_message = await self._synthesize(
                        query, results, llm=llm,
                    )
            await send({"type": "message", "role": "assistant",
                        "content": final_message})
            await send({"type": "plan_done",
                        "success": success,
                        "replans": replans,
                        "steps_done": sum(r["success"] for r in results),
                        "steps_total": len(results)})

            # Этап 3: финальное состояние в реестре планов + событие
            self._reg_update(status="done" if success else "error",
                             success=success, message=final_message,
                             replans=replans)
            await events.publish("plan.finished", {
                "plan_id": trace_id, "success": success,
                "steps_done": sum(r["success"] for r in results),
                "steps_total": len(results), "replans": replans,
            })

            # Этап 5: аналитика — итог выполнения плана
            _ra = _analytics()
            if _ra is not None:
                try:
                    _ra.log_outcome(
                        session_id=getattr(session, "id", "") or "",
                        plan_id=trace_id,
                        agents=[{"agent": r.get("agent", ""),
                                 "success": bool(r.get("success")),
                                 **({"error": r.get("error")}
                                    if r.get("error") else {})}
                                for r in results],
                        success=success, replans=replans,
                    )
                except Exception:
                    pass

            # ── сессия: итог в историю ──
            if session is not None:
                session.add_assistant(
                    final_message, agent=self._last_agent(results) or "")
                session.set_last_agents(self._agents_of(results))

            # Этап 3: долгая память — итог плана в Memory (мягкий хук)
            if memory is not None:
                try:
                    await memory.log_event_async(
                        "plan_finished",
                        summary=(final_message or (plan.intent if plan else ""))[:300],
                        session_id=getattr(session, "id", "") if session else "",
                        trace_id=trace_id,
                        details={
                            "intent": plan.intent if plan else "",
                            "steps_total": len(results),
                            "steps_done": sum(1 for r in results
                                              if r.get("success")),
                            "replans": replans,
                        },
                        success=success,
                    )
                except Exception:
                    logger.debug("memory.log_event(plan) failed (пропускаю)",
                                 exc_info=True)

            return self._result(success, plan, results, final_message,
                                replans, trace_id, self._agents_of(results))
        finally:
            if jint is not None:
                ok = bool(results) and all(r.get("success") for r in results)
                errors = "; ".join(r["error"] for r in results
                                   if r.get("error"))[:500]
                try:
                    await jint.journal_task_end(
                        task["task_id"],
                        status="ok" if ok and success else
                               ("error" if results else "no_steps"),
                        error=errors,
                        meta={"mode": "supervisor",
                              "replans": replans,
                              "steps": [{"step_id": r.get("step_id"),
                                         "agent": r["agent"],
                                         "success": r["success"]}
                                        for r in results]},
                    )
                except Exception:
                    logger.exception("journal task_end (пропускаю)")

    # ═══════════════════════════════════════════════════════
    # Helpers (UI-представление шага + итоговые сводки)
    # ═══════════════════════════════════════════════════════
    def _step_view(self, step: PlanStep) -> dict:
        """Представление шага для UI (заголовок/иконка из ui.hints)."""
        agent = self.runtime.get(step.agent)
        schema = getattr(agent, "schema", None)
        ui = getattr(schema, "ui", None)
        return {
            "id": step.id, "agent": step.agent, "task": step.task,
            "title": (getattr(schema, "title", "") or step.agent)
            if schema else step.agent,
            "icon": (getattr(ui, "icon", "") or "🤖") if ui else "🤖",
            "depends_on": list(step.depends_on),
        }

    @staticmethod
    def _overall_success(results: list[dict], aborted: bool) -> bool:
        """Успех плана: нет остановки по бюджету, и каждый упавший шаг
        был восстановлен последующим успешным (re-plan сработал)."""
        if aborted or not results:
            return False
        for i, r in enumerate(results):
            if not r.get("success") and not any(
                    x.get("success") for x in results[i + 1:]):
                return False
        return True

    @staticmethod
    def _agents_of(results: list[dict]) -> list[str]:
        return list(dict.fromkeys(r["agent"] for r in results if r.get("agent")))

    @staticmethod
    def _last_agent(results: list[dict]) -> str:
        for r in reversed(results):
            if r.get("success") and r.get("agent"):
                return r["agent"]
        return ""

    async def _collect(self, event: dict) -> None:
        """Приёмник событий по умолчанию (тесты/отладка)."""
        self.events.append(event)

    @staticmethod
    def _result(success: bool, plan: Plan | None, results: list[dict],
                message: str, replans: int, trace_id: str,
                agents: list[str]) -> dict:
        return {
            "success": success,
            "plan": plan.model_dump() if plan is not None else None,
            "results": results,
            "message": message,
            "replans": replans,
            "trace_id": trace_id,
            "agents": agents,
        }
