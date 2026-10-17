"""Исполнение плана — mixin Supervisor'а (Этапы 2–3, V2 §3.2/§3.10).

Выделено из supervisor.py в рамках рефакторинга v2:
- _execute_step    — запуск одного шага через Orchestrator.run_agent();
- _pipeline_context / _step_context — контекст предыдущих результатов
  (последовательный и DAG-режим);
- _run_dag         — исполнение плана волнами (независимые шаги параллельно);
- _apply_decision  — применение решения наблюдателя в DAG-режиме;
- _blocked_entry   — запись о шаге, заблокированном упавшей зависимостью.

Класс Supervisor наследует mixin; доступ к оркестратору (self.orch),
бюджетам (MAX_*), наблюдателю (_observe), фильтру агентов (_filter_known)
и реестру (_reg_*) — через self.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src import events
from src.supervisor.dag import (
    deps_satisfied,
    normalize_deps as dag_normalize,
    plan_waves,
)
from src.supervisor.models import EmitFn, ObserveDecision, Plan, PlanStep

logger = logging.getLogger(__name__)


class ExecutionMixin:
    """EXECUTE-фаза: шаги, волны DAG, контекст зависимостей."""

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
