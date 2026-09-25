"""Обработчики шагов loop."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def step_llm_call(
    step, context, state, sub_loop_runner,
) -> dict:
    """LLM call."""
    if context.llm_client is None:
        return {"error": "LLM not available", "_exit": True, "_success": False}

    messages = state.messages or [
        {"role": "system", "content": context.system_prompt},
        {"role": "user", "content": context.user_template.format(
            query=context.prompt, context=context.context_text or "—")},
    ]
    try:
        msg = await context.llm_client.chat(
            messages,
            tools=context.available_tools or None,
            model=context.model,
            on_token=context.on_token,
            on_reasoning=getattr(context, "on_reasoning", None),
            state_hash=context.extra.get("state_hash"),
        )
    except Exception as e:
        return {"error": str(e), "_exit": True, "_success": False}

    state.messages = messages + [msg]
    if msg.get("tool_calls"):
        return {"response": msg.get("content"), "has_tool_calls": True}
    return {"response": msg.get("content"), "_exit": True, "_success": True}


async def step_tool_calls(
    step, context, state, sub_loop_runner,
) -> dict:
    """Выполнение tool calls из последнего сообщения."""
    if not state.messages:
        return {}
    last = state.messages[-1]
    tool_calls = last.get("tool_calls") or []
    if not tool_calls:
        return {}

    if context.mcp_manager is None:
        return {"error": "MCP not available", "_exit": True, "_success": False}

    count = 0
    for tc in tool_calls:
        count += 1
        fn = tc.get("function", {})
        name = fn.get("name", "")
        import json as _json
        try:
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

        result = await context.mcp_manager.call_tool(
            name, args, approval_handler=context.approval_handler,
        )
        max_chars = context.extra.get("max_result_chars", 30000)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n...[обрезано]"
        state.messages.append({
            "role": "tool",
            "tool_call_id": tc.get("id", ""),
            "content": result,
        })

    return {"tool_calls_count": count}


async def step_check(step, context, state, sub_loop_runner) -> dict:
    """Проверка через checkers."""
    from src.loop.checks import run_check
    checker = step.params.get("checker", "")
    try:
        ok = await run_check(checker, step.params, context=context, state=state)
    except Exception as e:
        return {"error": str(e), "check_passed": False}
    return {"check_passed": ok, "checker": checker}


async def step_wait(step, context, state, sub_loop_runner) -> dict:
    """Пауза."""
    seconds = step.params.get("seconds", 1)
    await asyncio.sleep(seconds)
    return {"waited_seconds": seconds}


async def step_backoff(step, context, state, sub_loop_runner) -> dict:
    """Backoff с расписанием."""
    schedule = step.params.get("schedule", [1, 2, 5])
    idx = min(state.iterations, len(schedule) - 1)
    delay = schedule[idx]
    await asyncio.sleep(delay)
    return {"backoff_seconds": delay}


async def step_branch(step, context, state, sub_loop_runner) -> dict:
    """Ветвление — упрощённо: выбор по условию."""
    cases = step.params.get("cases", {})
    condition = step.params.get("condition", "")
    # Заглушка: выполняем первую ветку
    if condition and condition in cases:
        return {"branch_taken": condition}
    return {"branch_taken": None}


async def step_emit(step, context, state, sub_loop_runner) -> dict:
    """Отправка события в UI."""
    kind = step.params.get("kind", "info")
    message = step.params.get("message", "")
    if context.on_token and message:
        try:
            await context.on_token(message)
        except Exception:
            pass
    return {"emitted": kind, "message": message}
