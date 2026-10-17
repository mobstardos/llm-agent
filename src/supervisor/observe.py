"""Наблюдение и синтез — mixin Supervisor'а (V2 §3.2).

Выделено из supervisor.py в рамках рефакторинга v2:
- _observe    — решение по контексту ответа агента (continue/replan/finish/ask_user);
- _synthesize — итоговое сообщение пользователю (1 шаг → ответ; больше → LLM).

Класс Supervisor наследует mixin; доступ к LLM-клиенту, константам
(DETAIL_CHARS) и _filter_known — через self.
"""
from __future__ import annotations

import logging

from src.llm_errors import friendly_llm_error
from src.prompts.supervisor import (
    SUPERVISOR_OBSERVE_SYSTEM,
    SUPERVISOR_OBSERVE_USER_TEMPLATE,
    SUPERVISOR_SYNTHESIS_SYSTEM,
    SUPERVISOR_SYNTHESIS_USER_TEMPLATE,
)
from src.supervisor.models import ObserveDecision, PlanStep, parse_observe

logger = logging.getLogger(__name__)


class ObservationMixin:
    """OBSERVE-фаза (решение после шага) и синтез итога плана."""

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
