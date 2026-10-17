"""Loop Controller — исполняет LoopSpec.

Основные свойства:
  - Декларативное тело (steps)
  - Бюджеты (iterations, tokens, time)
  - Exit conditions (success, failure, timeout)
  - Вложенность (sub_loop)
  - Telemetry на каждом шаге
  - Fault injection
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from src.core.schema import LoopSpec, LoopType
from src.loop.context import LoopContext
from src.loop.faults import FaultInjector
from src.loop.state import LoopResult, LoopState
from src.loop.telemetry import LoopTelemetry

logger = logging.getLogger(__name__)


class LoopController:
    """Исполняет любой loop по его спецификации."""

    def __init__(self, telemetry: LoopTelemetry | None = None):
        self.telemetry = telemetry
        # Регистрируем обработчики шагов
        self._handlers: dict[str, Any] = {}
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        """По умолчанию — базовые шаги."""
        from src.loop.steps import (
            step_llm_call,
            step_tool_calls,
            step_check,
            step_wait,
            step_backoff,
            step_branch,
            step_emit,
        )
        self._handlers = {
            "llm_call": step_llm_call,
            "tool_calls": step_tool_calls,
            "check": step_check,
            "wait": step_wait,
            "backoff": step_backoff,
            "branch": step_branch,
            "emit": step_emit,
        }

    def register_handler(self, kind: str, fn) -> None:
        self._handlers[kind] = fn

    # ═══════════════════════════════════════════════════════
    # Main
    # ═══════════════════════════════════════════════════════
    async def run(
        self,
        spec: LoopSpec,
        context: LoopContext,
        sub_loop_runner: "LoopController | None" = None,
    ) -> LoopResult:
        trace_id = context.trace_id or uuid.uuid4().hex[:16]

        # Telemetry: start
        run_id = 0
        if self.telemetry:
            run_id = self.telemetry.start_run(
                trace_id=trace_id,
                loop_id=spec.id,
                parent_loop_id=context.parent_loop_id,
                agent_id=context.agent_id,
                session_id=context.session_id,
                scenario_id=context.extra.get("scenario_id"),
            )

        state = LoopState()
        t_start = time.time()

        try:
            result = await self._run_loop(
                spec=spec,
                context=context,
                state=state,
                sub_loop_runner=sub_loop_runner or self,
            )
        except asyncio.CancelledError:
            result = LoopResult(
                loop_id=spec.id,
                exit_reason="cancelled",
                success=False,
                iterations=state.iterations,
                tokens_input=state.tokens_input,
                tokens_output=state.tokens_output,
                tool_calls_count=state.tool_calls_count,
                duration_ms=(time.time() - t_start) * 1000,
                error="cancelled",
                history=state.history,
            )
        except Exception as e:
            logger.exception("Loop %s упал: %s", spec.id, e)
            result = LoopResult(
                loop_id=spec.id,
                exit_reason="exception",
                success=False,
                iterations=state.iterations,
                tokens_input=state.tokens_input,
                tokens_output=state.tokens_output,
                tool_calls_count=state.tool_calls_count,
                duration_ms=(time.time() - t_start) * 1000,
                error=str(e),
                history=state.history,
            )

        # Telemetry: end
        if self.telemetry and run_id:
            self.telemetry.end_run(run_id, result)

        return result

    # ═══════════════════════════════════════════════════════
    # Loop body
    # ═══════════════════════════════════════════════════════
    async def _run_loop(
        self,
        spec: LoopSpec,
        context: LoopContext,
        state: LoopState,
        sub_loop_runner: "LoopController",
    ) -> LoopResult:
        t_start = time.time()
        spec_type = spec.type if hasattr(spec.type, "value") else spec.type

        while True:
            # ─── Бюджеты ─────────────────────────────────
            if state.iterations >= spec.budget.max_iterations:
                return self._result(
                    spec, state, t_start,
                    exit_reason="budget_iterations", success=False,
                )
            if state.tokens_total >= spec.budget.max_tokens:
                return self._result(
                    spec, state, t_start,
                    exit_reason="budget_tokens", success=False,
                )
            if state.duration_seconds >= spec.budget.max_duration_seconds:
                return self._result(
                    spec, state, t_start,
                    exit_reason="budget_time", success=False,
                )

            # ─── Обработчик для каждого типа loop ───────
            if spec_type == LoopType.REASONING.value:
                done, success, step_result = await self._run_reasoning_iteration(
                    spec, context, state, sub_loop_runner,
                )
            elif spec_type == LoopType.VERIFICATION.value:
                done, success, step_result = await self._run_verification_iteration(
                    spec, context, state,
                )
            elif spec_type == LoopType.RETRY.value:
                done, success, step_result = await self._run_retry_iteration(
                    spec, context, state,
                )
            elif spec_type == LoopType.REFLECTION.value:
                done, success, step_result = await self._run_single_shot(
                    spec, context, state,
                )
            elif spec_type == LoopType.ESCALATION.value:
                done, success, step_result = await self._run_single_shot(
                    spec, context, state,
                )
            else:
                # Универсальный обход steps
                done, success, step_result = await self._run_steps(
                    spec, context, state, sub_loop_runner,
                )

            if step_result:
                state.apply(step_result)
                if self.telemetry and context.extra.get("run_id"):
                    self.telemetry.record_iteration(
                        context.extra["run_id"], state.iterations, step_result,
                    )

            state.iterations += 1

            if done:
                return self._result(
                    spec, state, t_start,
                    exit_reason="success" if success else "failure",
                    success=success,
                )

    # ═══════════════════════════════════════════════════════
    # Reasoning iteration
    # ═══════════════════════════════════════════════════════
    async def _run_reasoning_iteration(
        self,
        spec: LoopSpec,
        context: LoopContext,
        state: LoopState,
        sub_loop_runner: "LoopController",
    ) -> tuple[bool, bool, dict | None]:
        if context.llm_client is None:
            return True, False, {"error": "LLM client not available"}

        # Init messages on first iteration
        if not state.messages:
            state.messages = [
                {"role": "system", "content": context.system_prompt},
            ]
            user_content = context.user_template.format(
                query=context.prompt,
                context=context.context_text or "—",
            )
            state.messages.append({"role": "user", "content": user_content})

        # Fault injection
        if context.faults:
            rule = context.faults.check_llm(state.iterations, 0)
            if rule:
                await context.faults.apply_llm_fault(rule)

        # LLM call
        try:
            msg = await context.llm_client.chat(
                state.messages,
                tools=context.available_tools or None,
                model=context.model,
                on_token=context.on_token,
                on_reasoning=getattr(context, "on_reasoning", None),
                state_hash=context.extra.get("state_hash"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("LLM call failed")
            return True, False, {"error": str(e)}

        tokens_in = 0
        tokens_out = 0  # LLMClient может не возвращать; считаем приблизительно
        state.messages.append(msg)

        tool_calls = msg.get("tool_calls") or []

        # Нет tool calls — выходим с ответом
        if not tool_calls:
            response = msg.get("content") or ""
            return True, True, {
                "response": response,
                "tokens_input": tokens_in,
                "tokens_output": tokens_out,
                "messages": state.messages,
            }

        # Tool calls — выполняем
        step_result = await self._execute_tool_calls(
            tool_calls, context, state, spec,
        )
        return False, False, step_result

    async def _execute_tool_calls(
        self,
        tool_calls: list[dict],
        context: LoopContext,
        state: LoopState,
        spec: LoopSpec,
    ) -> dict:
        tool_calls_count = 0

        if context.mcp_manager is None:
            return {"error": "MCP manager not available", "tool_calls_count": 0}

        for tc in tool_calls:
            tool_calls_count += 1
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                import json as _json
                args = _json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}

            # Fault injection
            if context.faults:
                server = name.split("__", 1)[0] if "__" in name else ""
                rule = context.faults.check_mcp(server, name)
                if rule:
                    try:
                        await context.faults.apply_mcp_fault(rule)
                    except Exception as e:
                        state.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": f"Ошибка (injected): {e}",
                        })
                        continue

            # Approval
            requires_approval = name in set(spec.params if hasattr(spec, "params") else [])
            # Approval через MCPManager (там своя логика)

            # Call
            result = await context.mcp_manager.call_tool(
                name, args,
                approval_handler=context.approval_handler,
            )

            # Обрезаем по лимиту
            max_chars = context.extra.get("max_result_chars", 30000)
            if len(result) > max_chars:
                result = result[:max_chars] + "\n...[обрезано]"

            state.messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result,
            })

        return {
            "tool_calls_count": tool_calls_count,
            "messages": state.messages,
        }

    # ═══════════════════════════════════════════════════════
    # Verification iteration
    # ═══════════════════════════════════════════════════════
    async def _run_verification_iteration(
        self,
        spec: LoopSpec,
        context: LoopContext,
        state: LoopState,
    ) -> tuple[bool, bool, dict | None]:
        from src.loop.checks import run_check

        results: list[dict] = []
        all_passed = True
        critical_failed = False

        for step in spec.body:
            if step.kind != "check":
                continue
            params = step.params
            checker = params.get("checker", "")
            try:
                ok = await run_check(
                    checker, params, context=context, state=state,
                )
            except Exception as e:
                ok = False
                logger.warning("Check %s упал: %s", checker, e)

            results.append({"checker": checker, "passed": ok})
            if not ok:
                all_passed = False
                if params.get("critical", False):
                    critical_failed = True

        step_result = {
            "checks": results,
            "response": f"Verification: {sum(1 for r in results if r['passed'])}/{len(results)} passed",
        }
        return True, all_passed and not critical_failed, step_result

    # ═══════════════════════════════════════════════════════
    # Retry iteration
    # ═══════════════════════════════════════════════════════
    async def _run_retry_iteration(
        self,
        spec: LoopSpec,
        context: LoopContext,
        state: LoopState,
    ) -> tuple[bool, bool, dict | None]:
        # Backoff
        for step in spec.body:
            if step.kind == "backoff":
                schedule = step.params.get("schedule", [1, 2, 5])
                idx = min(state.iterations, len(schedule) - 1)
                delay = schedule[idx]
                logger.info("Retry backoff: %ss", delay)
                await asyncio.sleep(delay)

        # Sub-loop reasoning
        for step in spec.body:
            if step.kind == "sub_loop":
                loop_id = step.params.get("loop", "reasoning")
                ctx_extra = step.params.get("context_extra", "")
                child_ctx = context.child()
                if ctx_extra:
                    child_ctx.extra["retry_context"] = ctx_extra

                from src.loop.spec import LoopSpecLoader
                loader = LoopSpecLoader()
                child_spec = loader.get(loop_id)
                if not child_spec:
                    return True, False, {"error": f"Loop '{loop_id}' not found"}

                # Запускаем контроллер для sub-loop
                controller = LoopController(telemetry=self.telemetry)
                result = await controller.run(child_spec, child_ctx)

                if result.success:
                    return True, True, {"response": result.response}
                return False, False, {
                    "error": result.error or "retry failed",
                    "response": result.response,
                }

        return True, False, {"error": "no sub_loop in retry spec"}

    # ═══════════════════════════════════════════════════════
    # Single-shot (reflection, escalation)
    # ═══════════════════════════════════════════════════════
    async def _run_single_shot(
        self,
        spec: LoopSpec,
        context: LoopContext,
        state: LoopState,
    ) -> tuple[bool, bool, dict | None]:
        if context.llm_client is None:
            return True, False, {"error": "LLM client not available"}

        # Выполняем один llm_call шаг
        for step in spec.body:
            if step.kind == "llm_call":
                prompt_template = step.params.get("prompt_template", "")
                prompt = prompt_template.format(
                    task=context.prompt,
                    response=context.extra.get("last_response", ""),
                    error=context.extra.get("last_error", ""),
                )
                messages = [
                    {"role": "system", "content": context.system_prompt},
                    {"role": "user", "content": prompt},
                ]
                try:
                    msg = await context.llm_client.chat(
                        messages, model=context.model,
                        on_token=context.on_token,
                        on_reasoning=getattr(context, "on_reasoning", None),
                    )
                    return True, True, {
                        "response": msg.get("content") or "",
                    }
                except Exception as e:
                    return True, False, {"error": str(e)}

        return True, True, {"response": "(no llm_call step)"}

    # ═══════════════════════════════════════════════════════
    # Generic steps
    # ═══════════════════════════════════════════════════════
    async def _run_steps(
        self,
        spec: LoopSpec,
        context: LoopContext,
        state: LoopState,
        sub_loop_runner: "LoopController",
    ) -> tuple[bool, bool, dict | None]:
        for step in spec.body:
            handler = self._handlers.get(step.kind)
            if handler is None:
                logger.warning("Неизвестный шаг: %s", step.kind)
                continue
            try:
                result = await handler(
                    step=step, context=context, state=state,
                    sub_loop_runner=sub_loop_runner,
                )
                if result.get("_exit"):
                    return True, result.get("_success", True), result
            except Exception as e:
                logger.exception("Шаг %s упал: %s", step.kind, e)
                return True, False, {"error": str(e)}
        return False, False, None

    # ═══════════════════════════════════════════════════════
    # Result
    # ═══════════════════════════════════════════════════════
    def _result(
        self,
        spec: LoopSpec,
        state: LoopState,
        t_start: float,
        exit_reason: str,
        success: bool,
    ) -> LoopResult:
        return LoopResult(
            loop_id=spec.id,
            exit_reason=exit_reason,
            success=success,
            iterations=state.iterations,
            tokens_input=state.tokens_input,
            tokens_output=state.tokens_output,
            tool_calls_count=state.tool_calls_count,
            duration_ms=(time.time() - t_start) * 1000,
            response=state.last_response,
            error=state.last_error,
            history=state.history,
        )
