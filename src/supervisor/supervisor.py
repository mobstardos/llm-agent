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
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from typing import Any, Awaitable, Callable

from src import events
from src.llm_errors import friendly_llm_error
from src.prompts.supervisor import (
    SUPERVISOR_OBSERVE_SYSTEM,
    SUPERVISOR_OBSERVE_USER_TEMPLATE,
    SUPERVISOR_PLAN_SYSTEM,
    SUPERVISOR_PLAN_USER_TEMPLATE,
    SUPERVISOR_SYNTHESIS_SYSTEM,
    SUPERVISOR_SYNTHESIS_USER_TEMPLATE,
)
from src.supervisor.dag import (
    deps_satisfied,
    is_dag_plan,
    normalize_deps as dag_normalize,
    plan_waves,
)
from src.supervisor.models import (
    ObserveDecision,
    Plan,
    PlanStep,
    parse_observe,
    parse_plan,
)

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


EmitFn = Callable[[dict], Awaitable[None]]


class Supervisor:
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
                final_message = plan.reply or "Нет подходящего агента."
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
    # PLAN
    # ═══════════════════════════════════════════════════════
    async def _make_plan(
        self, query: str, *, model, llm, session, snapshot_prompt,
        suggested_agents, intent_source, intent_reason, memory=None,
    ) -> Plan | None:
        """План: детерминированный (@упоминание) или LLM-планировщик."""
        # Быстрый путь: явное @упоминание — план без LLM (V2 §3.3)
        if intent_source == "mention" and suggested_agents:
            agent_id = suggested_agents[0]
            if self.runtime.get(agent_id) is not None:
                # агенту уходит запрос без @префикса
                task_text = re.sub(r"@[a-zA-Z0-9_а-яё-]+", "", query).strip()
                return Plan(
                    intent="прямое обращение к агенту",
                    steps=[PlanStep(id="1", agent=agent_id,
                                    task=task_text or query,
                                    why=intent_reason or "явное @упоминание")],
                )

        directory = self._agent_directory()
        hint = ""
        if suggested_agents:
            hint = (f"агенты {', '.join(suggested_agents)}"
                    + (f" ({intent_reason})" if intent_reason else ""))
        elif intent_reason:
            hint = intent_reason

        user_content = SUPERVISOR_PLAN_USER_TEMPLATE.format(
            directory=directory,
            hint=hint or "—",
            history=(session.history_for_router() if session is not None
                     and hasattr(session, "history_for_router") else "") or "",
            plan_context=(session.plan_context_for_planner()
                          if session is not None
                          and hasattr(session, "plan_context_for_planner")
                          else "") or "",
            long_memory=await self._long_memory_context(query, memory),
            query=query,
        )
        messages = [
            # планировщику — его правила (формат плана); snapshot-промпт
            # роутера здесь не подходит (другой формат ответа)
            {"role": "system", "content": SUPERVISOR_PLAN_SYSTEM},
            {"role": "user", "content": user_content},
        ]
        try:
            msg = await llm.chat(messages, model=model)
            plan = parse_plan((msg.get("content") or ""))
            if plan is None:
                logger.warning("Supervisor: план не распарсен, деградация")
                return Plan(
                    intent=self.UNPARSED_INTENT,
                    reply=("Не смог сформировать план в нужном формате. "
                           "Попробуйте переформулировать запрос или обратиться "
                           "к агенту напрямую: @id_агента."))
            plan.steps = self._filter_known(plan.steps)[: self.MAX_STEPS]
            return plan
        except Exception as e:
            logger.warning("Supervisor planning failed: %s", e)
            return None

    def _agent_directory(self) -> str:
        """Каталог агентов для планировщика (id, режим, описание)."""
        lines: list[str] = []
        for aid in self.runtime.agents.keys():
            agent = self.runtime.get(aid)
            schema = getattr(agent, "schema", None)
            if schema is None:
                continue
            hints = getattr(schema, "routing_hints", None)
            desc = (getattr(hints, "description_for_router", "") or ""
                    ) or getattr(schema, "description", "")
            mode = getattr(getattr(schema, "mode", None), "value", "read")
            lines.append(f"- {aid} ({getattr(schema, 'title', aid)}, "
                         f"режим {mode}): {desc}")
        return "\n".join(lines) or "(нет активных агентов)"

    def _filter_known(self, steps: list[PlanStep]) -> list[PlanStep]:
        """Оставляет только шаги с известными активными агентами."""
        known = set(self.runtime.agents.keys())
        out: list[PlanStep] = []
        for s in steps:
            if s.agent in known:
                out.append(s)
            else:
                logger.warning("Supervisor: шаг %s с неизвестным агентом "
                               "'%s' отброшен", s.id, s.agent)
        return out

    def _has_destructive(self, plan: Plan) -> bool:
        """Эвристика: план трогает destructive-агента → нужен апрув."""
        for s in plan.steps:
            agent = self.runtime.get(s.agent)
            mode = getattr(getattr(agent, "schema", None), "mode", None)
            value = getattr(mode, "value", str(mode or ""))
            if str(value).lower() == "destructive":
                return True
        return False

    # ═══════════════════════════════════════════════════════
    # Реестр планов + долгая память (Этап 3)
    # ═══════════════════════════════════════════════════════
    @staticmethod
    def _reg_mode(dag_mode: bool) -> str:
        return "dag" if dag_mode else "sequential"

    def _reg_create(self, query: str, plan: Plan, session: Any,
                    needs_approval: bool, mode: str = "sequential") -> None:
        """Регистрирует план в реестре (мягко: реестр может отсутствовать)."""
        if self._reg is None or plan is None or not plan.steps:
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
        if self._reg is None:
            return
        try:
            self._reg.update(self._plan_id, **fields)
        except Exception:
            logger.debug("plan registry update failed (пропускаю)",
                         exc_info=True)

    def _reg_step(self, step_id: str, **fields) -> None:
        if self._reg is None:
            return
        try:
            self._reg.set_step(self._plan_id, step_id, **fields)
        except Exception:
            logger.debug("plan registry step update failed (пропускаю)",
                         exc_info=True)

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

    # ═══════════════════════════════════════════════════════
    # DAG-исполнение (Этап 3)
    # ═══════════════════════════════════════════════════════
    async def _run_dag(
        self, query: str, plan: Plan, *, results: list[dict],
        model, llm, session, mcp_manager, memory, file_state,
        send: EmitFn, approval_handler, trace_id: str,
    ) -> tuple[int, bool, str]:
        """Исполнение плана волнами: независимые шаги — параллельно.

        Возвращает (replans, aborted, final_message). Результаты шагов
        дописывает в results (в порядке выполнения); наблюдение — по
        контексту ответа, как и в последовательном режиме: провал шага
        (или блокировка по упавшей зависимости) отдаётся наблюдателю.
        Решения наблюдателя:
        - replan → очередь оставшихся шагов заменяется новым планом
          (как в последовательном режиме), волны пересчитываются;
        - finish → план остановлен (aborted=True);
        - ask_user → план остановлен, вопрос — в final_message.
        """
        replans = 0
        t0 = time.monotonic()
        results_by_step: dict[str, dict] = {}
        sem = asyncio.Semaphore(max(1, self.MAX_CONCURRENT))
        waves: list[list[PlanStep]] = plan_waves(list(plan.steps))
        deps_map = dag_normalize(list(plan.steps))
        wave_counter = 0

        while waves:
            if time.monotonic() - t0 > self.MAX_SECONDS:
                return replans, True, (
                    "Остановлено: превышен лимит времени плана "
                    f"({self.MAX_SECONDS:.0f} с).")
            if len(results) >= self.MAX_STEPS:
                return replans, True, (
                    f"Остановлено: достигнут лимит шагов плана "
                    f"({self.MAX_STEPS}).")

            wave = waves.pop(0)
            wave_counter += 1
            wave_num = wave_counter

            # ── блокировка шагов с невыполненными зависимостями ──
            runnable: list[PlanStep] = []
            wave_entries: list[dict] = []
            for step in wave:
                if deps_satisfied(step, results_by_step, deps_map):
                    runnable.append(step)
                    continue
                missing = [d for d in deps_map.get(step.id, [])
                           if not (results_by_step.get(d) or {}).get("success")]
                entry = self._blocked_entry(step, missing)
                view = self._step_view(step)
                await send({
                    "type": "step_start", "step_id": step.id,
                    "agent": step.agent, "task": step.task,
                    "title": view["title"], "icon": view["icon"],
                    "wave": wave_num, "skipped": True,
                })
                await send({
                    "type": "step_done", "step_id": step.id,
                    "agent": step.agent, "ok": False,
                    "summary": "", "tool_calls": 0, "duration_ms": 0,
                    "error": entry["error"],
                })
                results.append(entry)
                results_by_step[step.id] = entry
                if session is not None:
                    session.record_step_result(step.id, entry)
                self._reg_step(step.id, status="error", error=entry["error"])
                await events.publish("step.finished", {
                    "plan_id": trace_id, "step_id": step.id,
                    "agent": step.agent, "success": False, "skipped": True,
                })
                wave_entries.append(entry)

            # ── параллельный запуск готовых шагов волны ──
            if runnable:
                await send({
                    "type": "status",
                    "text": (f"Волна {wave_num}: выполняю "
                             f"{len(runnable)} шаг(ов) параллельно..."),
                })

                async def bounded(stp: PlanStep, wnum: int) -> dict:
                    async with sem:
                        return await self._execute_step(
                            stp, done=results, model=model, llm=llm,
                            session=session, mcp_manager=mcp_manager,
                            memory=memory, file_state=file_state, send=send,
                            approval_handler=approval_handler,
                            trace_id=trace_id,
                            results_by_step=results_by_step, wave=wnum,
                        )

                entries = await asyncio.gather(
                    *(bounded(s, wave_num) for s in runnable))
                for entry in entries:
                    results.append(entry)
                    results_by_step[entry.get("step_id", "")] = entry
                wave_entries.extend(entries)

            # ── OBSERVE: по каждому провалу волны, в порядке шагов ──
            remaining = [s for w in waves for s in w]
            for entry in wave_entries:
                if entry.get("success"):
                    continue
                step = next((s for s in wave
                             if s.id == entry.get("step_id")), None)
                if step is None:
                    continue
                decision = await self._observe(
                    query, step, entry, queue=remaining, results=results,
                    llm=llm,
                )
                if decision.action == "continue":
                    continue
                act, new_steps, msg = await self._apply_decision(
                    decision, replans, session, send, trace_id,
                    failed_step=step, failed_error=entry.get("error", ""),
                )
                if act == "replan":
                    replans += 1
                    waves = plan_waves(new_steps)
                    deps_map = dag_normalize(new_steps)
                    break
                if act == "ask_user":
                    return replans, True, msg
                return replans, True, msg   # finish / бюджет re-plan
        return replans, False, ""

    async def _apply_decision(
        self, decision: ObserveDecision, replans: int, session: Any,
        send: EmitFn, trace_id: str, failed_step: PlanStep | None = None,
        failed_error: str = "",
    ) -> tuple[str, list[PlanStep], str]:
        """Применяет решение наблюдателя в DAG-режиме.

        Возвращает (act, new_steps, message): act — "continue" (ничего),
        "replan" (new_steps — новая очередь), "ask_user" или "stop".
        """
        if decision.action == "continue":
            return "continue", [], ""
        if decision.action == "replan" and decision.updated_plan \
                and decision.updated_plan.steps:
            if replans >= self.MAX_REPLANS:
                return ("stop", [], (
                    "Остановлено: достигнут лимит перепланировок "
                    f"({self.MAX_REPLANS}). Последняя ошибка: "
                    f"{failed_error or 'шаг не выполнен'}"))
            new_steps = self._filter_known(decision.updated_plan.steps)
            for s in new_steps:
                s.id = f"r{replans + 1}-{s.id}"
            self._reg_update(replans=replans + 1)
            reason = decision.message_to_user or (
                f"шаг {failed_step.id if failed_step else '?'} упал — "
                "строю новый план")
            await events.publish("plan.replan", {
                "plan_id": trace_id, "attempt": replans + 1,
                "reason": decision.message_to_user or "",
            })
            await send({
                "type": "replan", "attempt": replans + 1,
                "reason": reason,
                "steps": [self._step_view(s) for s in new_steps],
            })
            if session is not None and session.active_plan:
                for s in new_steps:
                    session.active_plan["steps"].append(
                        {"id": s.id, "agent": s.agent, "task": s.task,
                         "status": "pending"})
            return "replan", new_steps, ""
        if decision.action == "ask_user":
            return "ask_user", [], (decision.message_to_user
                                    or "Нужно уточнение: продолжить?")
        # finish
        return "stop", [], (decision.message_to_user
                            or failed_error or "Шаг не выполнен.")

    def _blocked_entry(self, step: PlanStep, missing: list[str]) -> dict:
        """Запись о шаге, заблокированном упавшей зависимостью."""
        return {
            "agent": step.agent,
            "success": False,
            "error": ("Шаг пропущен: зависимость не выполнена ("
                      + ", ".join(missing) + ")"),
            "content": "",
            "tool_calls": 0,
            "duration_ms": 0,
            "step_id": step.id,
            "task": step.task,
            "skipped": True,
        }

    def _step_context(self, step: PlanStep,
                      results_by_step: dict[str, dict]) -> str:
        """Контекст шага в DAG-режиме: сначала результаты его зависимостей
        (полноценно), затем остальные выполненные шаги (сводно)."""
        parts: list[str] = []
        covered: set[str] = set()
        for dep in step.depends_on or []:
            r = results_by_step.get(dep)
            if r is None:
                continue
            covered.add(dep)
            if r.get("success"):
                parts.append(
                    f"Результат шага {dep} ({r['agent']}) — зависимость, "
                    f"выполнено:\n{(r.get('content', '') or '')[: self.DETAIL_CHARS]}")
            else:
                parts.append(
                    f"Шаг {dep} ({r['agent']}) — ОШИБКА зависимости: "
                    f"{(r.get('error', '') or '')[:300]}")
        rest = [r for sid, r in results_by_step.items() if sid not in covered]
        if rest:
            parts.append(self._pipeline_context(rest))
        if not parts:
            return ""
        return ("\n\n".join(parts))

    # ═══════════════════════════════════════════════════════
    # EXECUTE
    # ═══════════════════════════════════════════════════════
    async def _execute_step(
        self, step: PlanStep, *, done: list[dict], model, llm, session,
        mcp_manager, memory, file_state, send: EmitFn, approval_handler,
        trace_id: str,
        results_by_step: dict[str, dict] | None = None,
        wave: int = 0,
    ) -> dict:
        """Запуск одного шага: BaseAgent.run() с контекстом прошлых шагов.

        В DAG-режиме (results_by_step передан) контекст строится по
        зависимостям шага (_step_context), в последовательном — как в
        Этапе 2 (все предыдущие результаты).
        """
        view = self._step_view(step)
        self._reg_step(step.id, status="running")
        await events.publish("step.started", {
            "plan_id": trace_id, "step_id": step.id,
            "agent": step.agent, "wave": wave,
        })
        await send({"type": "step_start", "step_id": step.id,
                    "agent": step.agent, "task": step.task,
                    "title": view["title"], "icon": view["icon"],
                    "step_num": len(done) + 1,
                    "wave": wave})

        async def step_token(tok: str) -> None:
            try:
                await send({"type": "token", "step_id": step.id,
                            "token": tok})
            except Exception:
                pass

        async def step_reasoning(text: str) -> None:
            # Task 24-d: ход мыслей reasoning-модели — в сворачиваемый
            # блок дорожки шага
            try:
                await send({"type": "reasoning", "step_id": step.id,
                            "text": text})
            except Exception:
                pass

        if results_by_step is not None:
            context_text = self._step_context(step, results_by_step)
        else:
            context_text = self._pipeline_context(done)
        entry = await self.orch.run_agent(
            step.agent, step.task,
            model=model, context_text=context_text,
            llm_client=llm, mcp_manager=mcp_manager,
            memory=memory, file_state=file_state,
            on_token=step_token,          # agent-внутренние шаги не светим:
            on_reasoning=step_reasoning,  # у шага свой стрим в дорожке
            on_step=None,
            approval_handler=approval_handler,
            trace_id=trace_id,
            session_id=(getattr(session, "id", "")
                        if session is not None else ""),
        )
        entry = dict(entry)
        entry["step_id"] = step.id
        entry["task"] = step.task

        ok = bool(entry.get("success"))
        await send({
            "type": "step_done", "step_id": step.id, "agent": step.agent,
            "ok": ok,
            "summary": (entry.get("content", "") or "")[:200],
            "tool_calls": int(entry.get("tool_calls", 0) or 0),
            "duration_ms": int(entry.get("duration_ms", 0) or 0),
            "error": (entry.get("error", "") or "")[:300],
        })
        await events.publish("step.finished", {
            "plan_id": trace_id, "step_id": step.id,
            "agent": step.agent, "success": ok,
            "duration_ms": int(entry.get("duration_ms", 0) or 0),
        })
        if session is not None:
            session.record_step_result(step.id, entry)
        self._reg_step(
            step.id, status="done" if ok else "error",
            summary=(entry.get("content", "") or "")[:300],
            error=(entry.get("error", "") or "")[:300],
            duration_ms=int(entry.get("duration_ms", 0) or 0),
            tool_calls=int(entry.get("tool_calls", 0) or 0),
        )
        return entry

    def _pipeline_context(self, done: list[dict]) -> str:
        """Результаты предыдущих шагов — вход следующего (pipeline)."""
        if not done:
            return ""
        parts: list[str] = []
        for r in done:
            if r.get("success"):
                parts.append(
                    f"Шаг {r.get('step_id')} ({r['agent']}) — выполнено:\n"
                    f"{(r.get('content', '') or '')[: self.DETAIL_CHARS]}")
            else:
                parts.append(
                    f"Шаг {r.get('step_id')} ({r['agent']}) — ОШИБКА: "
                    f"{(r.get('error', '') or '')[:300]}")
        return ("Результаты предыдущих шагов плана (используй их, "
                "не повторяй ту же работу):\n\n" + "\n\n".join(parts))

    # ═══════════════════════════════════════════════════════
    # OBSERVE
    # ═══════════════════════════════════════════════════════
    async def _observe(
        self, query: str, step: PlanStep, entry: dict,
        queue: list[PlanStep], results: list[dict], *, llm,
    ) -> ObserveDecision:
        """Решение по контексту ответа агента (V2 §3.2).

        Успех (детерминированно) → continue. Провал → LLM-наблюдатель:
        continue / replan / finish / ask_user. LLM недоступен → finish.
        """
        if entry.get("success"):
            return ObserveDecision(action="continue")

        done_txt = "\n".join(
            f"- шаг {r.get('step_id')} ({r['agent']}): "
            + ("ok" if r.get("success")
               else f"УПАЛ: {(r.get('error') or '')[:150]}")
            for r in results) or "(это первый шаг)"
        remaining = "\n".join(
            f"- {s.id} ({s.agent}): {s.task}" for s in queue) or "(шагов нет)"
        details = ""
        content = (entry.get("content", "") or "")[:600]
        if content:
            details += f"Ответ агента: {content}\n"
        if entry.get("error"):
            details += f"Ошибка: {entry['error'][:400]}\n"
        details += f"tool_calls: {entry.get('tool_calls', 0)}"

        messages = [
            {"role": "system", "content": SUPERVISOR_OBSERVE_SYSTEM},
            {"role": "user", "content": SUPERVISOR_OBSERVE_USER_TEMPLATE.format(
                query=query, done_steps=done_txt, step_id=step.id,
                agent=step.agent, task=step.task,
                status="УСПЕХ" if entry.get("success") else "ОШИБКА",
                details=details.strip(), remaining=remaining)},
        ]
        try:
            msg = await llm.chat(messages)
            decision = parse_observe(msg.get("content") or "")
            if decision is None:
                return ObserveDecision(
                    action="finish",
                    message_to_user=(entry.get("error")
                                     or "Шаг не выполнен."))
            # фильтруем агентов в updated_plan
            if decision.updated_plan is not None:
                decision.updated_plan.steps = self._filter_known(
                    decision.updated_plan.steps)
            return decision
        except Exception as e:
            logger.warning("Supervisor observe failed: %s", e)
            hint = friendly_llm_error(e)
            reason = f"Ошибка шага: {entry.get('error') or 'не выполнен'}"
            if hint:
                # hint самодостаточен (см. llm_errors.describe) — без префикса
                reason += f". {hint}"
            return ObserveDecision(action="finish", message_to_user=reason)

    # ═══════════════════════════════════════════════════════
    # СИНТЕЗ
    # ═══════════════════════════════════════════════════════
    async def _synthesize(self, query: str, results: list[dict], *, llm) -> str:
        """Итоговое сообщение: 1 шаг → его ответ; больше → LLM-синтез."""
        ok_results = [r for r in results if r.get("success")]
        if not ok_results:
            errs = "; ".join(
                f"{r['agent']}: {(r.get('error') or 'без деталей')[:200]}"
                for r in results)
            return f"Не удалось выполнить ни один шаг плана. {errs}"
        if len(results) == 1 and results[0].get("success"):
            return results[0].get("content", "") or "Готово."

        results_txt = "\n\n".join(
            f"Шаг {r.get('step_id')} ({r['agent']}) — "
            + ("успех" if r.get("success") else "ОШИБКА") + ":\n"
            + ((r.get("content", "") or "")[: self.DETAIL_CHARS]
               or (r.get("error", "") or "")[:300])
            for r in results)
        messages = [
            {"role": "system", "content": SUPERVISOR_SYNTHESIS_SYSTEM},
            {"role": "user", "content":
             SUPERVISOR_SYNTHESIS_USER_TEMPLATE.format(
                 query=query, results=results_txt)},
        ]
        try:
            msg = await llm.chat(messages)
            text = (msg.get("content") or "").strip()
            if text:
                return text
        except Exception as e:
            logger.warning("Supervisor synthesis failed: %s", e)
        # fallback: склейка ответов шагов
        return "\n\n".join(
            f"[{r['agent']}] {(r.get('content', '') or '').strip()}"
            for r in ok_results) or "Готово."

    # ═══════════════════════════════════════════════════════
    # Helpers
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
