"""Планирование — mixin Supervisor'а (V2 §3.2–3.3).

Выделено из supervisor.py в рамках рефакторинга v2: построение плана
(детерминированный @упоминание-путь + LLM-планировщик), каталог агентов,
фильтрация шагов по известным агентам и эвристика destructive-апрува.

Класс Supervisor наследует mixin; доступ к runtime/константам — через self.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from src.prompts.supervisor import (
    SUPERVISOR_PLAN_SYSTEM,
    SUPERVISOR_PLAN_USER_TEMPLATE,
)
from src.supervisor.models import Plan, PlanStep, parse_plan

logger = logging.getLogger(__name__)


class PlanningMixin:
    """Построение и валидация плана (PLAN-фаза Supervisor)."""

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
            history=(session.history_for_router(exclude_last=True)
                     if session is not None
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
