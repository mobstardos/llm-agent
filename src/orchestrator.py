"""Оркестратор: LLM-роутинг между агентами + выполнение задачи."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import nullcontext
from typing import Any, Awaitable, Callable

from src.agents.runtime import AgentRuntime
from src.llm_client import LLMClient
from src.llm_errors import friendly_llm_error
from src.prompts.orchestrator import (
    ORCHESTRATOR_FALLBACK,
    ORCHESTRATOR_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)

TokenCallback = Callable[[str], Awaitable[None]]
ApprovalHandler = Callable[[dict], Awaitable[bool]]
StepCallback = Callable[[str, int], Awaitable[None]]

# journal-хуки мягкие: модуль ищется ОДИН раз и тихо (без traceback-шума),
# если стек журнала недоступен (например, не установлен psycopg).
_journal_module: Any = None
_journal_checked = False


def _journal() -> Any:
    """Возвращает модуль src.journal.integration или None (тихо, один раз)."""
    global _journal_module, _journal_checked
    if not _journal_checked:
        _journal_checked = True
        try:
            from src.journal import integration as _mod
            _journal_module = _mod
        except Exception as e:  # noqa: BLE001 — мягкий хук по замыслу
            logger.info("Journal недоступен (хуки отключены): %s", e)
    return _journal_module


class Orchestrator:
    def __init__(self, llm: LLMClient, runtime: AgentRuntime):
        self.llm = llm
        self.runtime = runtime

    async def route(self, query: str, model: str | None = None,
                    prompt: str | None = None,
                    history: str | None = None) -> dict:
        if len(self.runtime) == 0:
            return {"agents": [], "reason": "Нет активных агентов"}

        _t0 = time.perf_counter()

        def _analytics(agents: list[str], reason: str, error: str = "") -> None:
            """Этап 5: журнал решений LLM-роутера (мягко, никогда не бросает)."""
            try:
                from src import route_analytics as _ra
                _ra.log_decision(
                    query=query, source="llm.route", agents=agents,
                    reason=error or reason,
                    duration_ms=(time.perf_counter() - _t0) * 1000,
                )
            except Exception:
                pass

        system_prompt = prompt or ORCHESTRATOR_FALLBACK
        user_content = ORCHESTRATOR_USER_TEMPLATE.format(query=query)
        # Этап 1: роутер видит последние сообщения диалога (V2 §3.4)
        if history:
            user_content = (
                f"История диалога (для контекста):\n{history}\n\n"
                + user_content
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            msg = await self.llm.chat(messages, model=model)
            raw = (msg.get("content") or "").strip()
            # LLM может завернуть ответ в markdown-ограждение — срезаем его
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw).strip()

            data = None
            try:
                data = json.loads(raw)
            except Exception:
                # Fallback: ищем первый JSON-объект в тексте
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(0))
                    except Exception:
                        data = None

            agents: list[str] = []
            reason = ""
            if isinstance(data, dict):
                raw_agents = data.get("agents", [])
                if isinstance(raw_agents, list):
                    agents = [str(a) for a in raw_agents if a]
                reason = str(data.get("reason", "") or "")

            # Оставляем только известных активных агентов
            known = set(self.runtime.agents.keys())
            agents = [a for a in agents if a in known]
            if not agents:
                reason = reason or "LLM не выбрал подходящих агентов"
            _analytics(agents, reason)
            return {"agents": agents, "reason": reason}
        except Exception as e:
            logger.warning("LLM routing failed: %s", e)
            hint = friendly_llm_error(e)
            if hint:
                # Понятная подсказка вместо сырого «Error code: 403 - {...}»;
                # техническая деталь остаётся в журнале (logger.warning выше)
                _analytics([], f"Ошибка роутинга: {hint}", error=str(e))
                return {"agents": [], "reason": f"Ошибка роутинга: {hint}"}
            _analytics([], f"Ошибка роутинга: {e}", error=str(e))
            return {"agents": [], "reason": f"Ошибка роутинга: {e}"}

    # ═══════════════════════════════════════════════════════
    # Выполнение задачи (route + запуск агентов + агрегация)
    # ═══════════════════════════════════════════════════════
    async def handle(
        self,
        query: str,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
        agents: list[str] | None = None,
        context_text: str = "",
        session_id: str = "",
        llm_client: Any = None,
        mcp_manager: Any = None,
        memory: Any = None,
        file_state: Any = None,
        on_token: TokenCallback | None = None,
        on_reasoning: TokenCallback | None = None,  # Task 24-d
        on_step: StepCallback | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> dict:
        """Выполняет запрос пользователя выбранными агентами.

        Если `agents` не передан — маршрутизирует через route() (LLM).
        Каждый агент запускается через BaseAgent.run() (LoopController,
        бюджеты из agent.yaml). Результат — агрегированный словарь для WS.

        journal-хуки (task_start / agent_context / task_end) — мягкие:
        если журнал не инициализирован, вызовы проходят сквозь без затрат.
        """
        trace_id = uuid.uuid4().hex[:16]

        # ── Маршрутизация, если список агентов не задан извне ──
        if not agents:
            routed = await self.route(query, model=model, prompt=system_prompt)
            agents = routed.get("agents", [])

        # ── journal: начало задачи ──
        task = {"task_id": trace_id, "trace_id": trace_id}
        jint = _journal()
        if jint is not None:
            try:
                task = await jint.journal_task_start(
                    query=query, agents=agents, model=model or "",
                    session_id=session_id, trace_id=trace_id,
                )
            except Exception:
                logger.exception("journal task_start (пропускаю)")

        results: list[dict] = []
        try:
            for agent_id in agents:
                entry = await self._run_one(
                    agent_id, query,
                    model=model, context_text=context_text,
                    llm_client=llm_client or self.llm,
                    mcp_manager=mcp_manager, memory=memory,
                    file_state=file_state,
                    on_token=on_token, on_reasoning=on_reasoning,
                    on_step=on_step,
                    approval_handler=approval_handler,
                    trace_id=task["trace_id"], session_id=session_id,
                )
                results.append(entry)
        finally:
            ok = bool(results) and all(r.get("success") for r in results)
            status = "ok" if ok else ("error" if results else "no_agents")
            errors = "; ".join(
                r["error"] for r in results if r.get("error")
            )[:500]
            try:
                if jint is not None:
                    await jint.journal_task_end(
                        task["task_id"], status=status, error=errors,
                        meta={"agents": agents, "results": [
                            {"agent": r["agent"], "success": r["success"],
                             "exit_reason": r["exit_reason"]}
                            for r in results]},
                    )
            except Exception:
                logger.exception("journal task_end (пропускаю)")

        return {
            "results": results,
            "success": ok,
            "trace_id": trace_id,
            "agents": list(agents),
        }

    async def run_agent(
        self,
        agent_id: str,
        query: str,
        *,
        model: str | None = None,
        context_text: str = "",
        llm_client: Any = None,
        mcp_manager: Any = None,
        memory: Any = None,
        file_state: Any = None,
        on_token: TokenCallback | None = None,
        on_reasoning: TokenCallback | None = None,  # Task 24-d
        on_step: StepCallback | None = None,
        approval_handler: ApprovalHandler | None = None,
        trace_id: str | None = None,
        session_id: str = "",
    ) -> dict:
        """Публичный запуск одного агента (V2 §3.9 — точка для Supervisor).

        Обёртка над _run_one: та же изоляция падений, journal-хуки
        agent_context, бюджет из декларации агента. Возвращает entry:
        {agent, content, steps, tool_calls, success, exit_reason,
        duration_ms, error}.
        """
        return await self._run_one(
            agent_id, query,
            model=model, context_text=context_text,
            llm_client=llm_client or self.llm,
            mcp_manager=mcp_manager, memory=memory,
            file_state=file_state,
            on_token=on_token, on_reasoning=on_reasoning,
            on_step=on_step,
            approval_handler=approval_handler,
            trace_id=trace_id or uuid.uuid4().hex[:16],
            session_id=session_id,
        )

    async def _run_one(
        self,
        agent_id: str,
        query: str,
        *,
        model: str | None,
        context_text: str,
        llm_client: Any,
        mcp_manager: Any,
        memory: Any,
        file_state: Any,
        on_token: TokenCallback | None,
        on_reasoning: TokenCallback | None,
        on_step: StepCallback | None,
        approval_handler: ApprovalHandler | None,
        trace_id: str,
        session_id: str,
    ) -> dict:
        """Запуск одного агента с журналированием и защитой от падений."""
        entry: dict = {
            "agent": agent_id, "content": "", "steps": [],
            "tool_calls": 0, "success": False,
            "exit_reason": "", "duration_ms": 0, "error": "",
        }
        agent = self.runtime.get(agent_id)
        if agent is None:
            entry["error"] = f"Агент '{agent_id}' недоступен"
            return entry

        async def _step_cb(step_name: str, step_num: int) -> None:
            entry["steps"].append({"step": step_num, "name": step_name})
            if on_step:
                await on_step(agent_id, step_num)

        # journal: события внутри — с меткой агента (если журнал включён)
        agent_ctx = nullcontext()
        jint = _journal()
        if jint is not None:
            try:
                agent_ctx = jint.journal_agent_context(agent_id)
            except Exception:
                pass

        t0 = time.time()
        try:
            async with agent_ctx:
                res = await agent.run(
                    query, model=model, context_text=context_text,
                    llm_client=llm_client, mcp_manager=mcp_manager,
                    memory=memory, file_state=file_state,
                    on_token=on_token, on_reasoning=on_reasoning,
                    on_step=_step_cb,
                    approval_handler=approval_handler,
                    trace_id=trace_id,
                    session_id=session_id or None,
                )
            entry.update(
                content=(getattr(res, "response", "") or ""),
                tool_calls=int(getattr(res, "tool_calls_count", 0) or 0),
                success=bool(getattr(res, "success", False)),
                exit_reason=(getattr(res, "exit_reason", "") or ""),
                error=(getattr(res, "error", "") or "")[:500],
                steps=self._steps_summary(getattr(res, "history", None)),
            )
        except asyncio.CancelledError:
            entry["exit_reason"] = "cancelled"
            entry["error"] = "Отменено"
            entry["duration_ms"] = round((time.time() - t0) * 1000)
            raise
        except Exception as e:
            logger.exception("Agent %s failed", agent_id)
            entry["error"] = str(e)[:500]
        entry["duration_ms"] = round((time.time() - t0) * 1000)
        return entry

    @staticmethod
    def _steps_summary(history: list[dict] | None) -> list[dict]:
        """Компактная сводка итераций loop для UI (без сообщений)."""
        out: list[dict] = []
        for i, h in enumerate(history or [], 1):
            if not isinstance(h, dict):
                continue
            out.append({
                "step": i,
                "tool_calls": int(h.get("tool_calls_count", 0) or 0),
                "done": bool(h.get("response")),
            })
        return out