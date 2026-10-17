#!/usr/bin/env python3
"""Юнит-тест Orchestrator.handle() (Bug: метод отсутствовал → AttributeError в WS).

Покрывает:
  1. handle(agents=[...]) — route() НЕ вызывается, результаты агрегируются
  2. handle(agents=None)  — route() вызывается (fake LLM)
  3. Падение агента — error в записи, задача продолжается, success=False
  4. Неизвестный агент — запись с ошибкой «недоступен»
  5. _steps_summary из history
  6. journal-хуки без рекордера — no-op, не падают
Запуск: python scripts/test_orchestrator_handle.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.orchestrator import Orchestrator  # noqa: E402


class FakeResult:
    """Эмуляция LoopResult."""
    def __init__(self, response="ok", success=True, exit_reason="success",
                 tool_calls=2, error="", history=None):
        self.response = response
        self.success = success
        self.exit_reason = exit_reason
        self.tool_calls_count = tool_calls
        self.error = error
        self.history = history or []


class FakeAgent:
    def __init__(self, agent_id, result=None, exc=None):
        self.agent_id = agent_id
        self.result = result or FakeResult()
        self.exc = exc
        self.calls: list[dict] = []

    async def run(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        if self.exc:
            raise self.exc
        return self.result


class FakeRuntime:
    def __init__(self, agents: dict):
        self.agents = agents

    def get(self, agent_id):
        return self.agents.get(agent_id)

    def __len__(self):
        return len(self.agents)


class FakeLLM:
    def __init__(self, reply='{"agents": ["alpha"], "reason": "test"}'):
        self.reply = reply
        self.chat_calls = 0

    async def chat(self, messages, **kwargs):
        self.chat_calls += 1
        return {"content": self.reply}


PASS = "✓"
FAIL = "✗"


async def main() -> int:
    ok_count = 0
    fail_count = 0

    def check(name, cond):
        nonlocal ok_count, fail_count
        print(f"  {PASS if cond else FAIL} {name}")
        ok_count += bool(cond)
        fail_count += (not cond)

    hist = [
        {"tool_calls_count": 1, "response": None},
        {"tool_calls_count": 1, "response": "готово"},
    ]
    agent_a = FakeAgent("alpha", FakeResult(
        response="Ответ альфы", history=hist, tool_calls=2))
    agent_b = FakeAgent("beta", FakeResult(response="Ответ беты"))
    agent_bad = FakeAgent("bad", exc=RuntimeError("boom"))
    llm = FakeLLM()

    orch = Orchestrator(llm, FakeRuntime({
        "alpha": agent_a, "beta": agent_b, "bad": agent_bad,
    }))

    print("== Тест 1: handle с переданным списком агентов ==")
    res = await orch.handle("привет", agents=["alpha", "beta"],
                            model="m1", session_id="s1")
    check("route() не вызывался (LLM не тратится)", llm.chat_calls == 0)
    check("две записи results", len(res["results"]) == 2)
    r0 = res["results"][0]
    check("agent/content", r0["agent"] == "alpha"
          and r0["content"] == "Ответ альфы")
    check("success/exit_reason", r0["success"] and r0["exit_reason"] == "success")
    check("tool_calls", r0["tool_calls"] == 2)
    check("steps из history", len(r0["steps"]) == 2
          and r0["steps"][0]["tool_calls"] == 1
          and r0["steps"][1]["done"] is True)
    check("общий success=True", res["success"] is True)
    check("trace_id присутствует", bool(res["trace_id"]))
    check("query дошёл до агента", agent_a.calls[0]["query"] == "привет")
    check("model проброшен", agent_a.calls[0].get("model") == "m1")
    check("on_token/on_step проброшены", "on_token" in agent_a.calls[0])

    print("== Тест 2: handle без списка → route() через LLM ==")
    llm.chat_calls = 0
    res2 = await orch.handle("сделай сборку", model="m1")
    check("LLM вызван 1 раз", llm.chat_calls == 1)
    check("выбран alpha", res2["agents"] == ["alpha"])

    print("== Тест 3: падение агента не роняет задачу ==")
    llm.chat_calls = 0
    res3 = await orch.handle("x", agents=["bad", "beta"])
    rb = res3["results"][1]
    check("ошибка зафиксирована", "boom" in res3["results"][0]["error"])
    check("следующий агент выполнен", rb["content"] == "Ответ беты")
    check("общий success=False", res3["success"] is False)

    print("== Тест 4: неизвестный агент ==")
    res4 = await orch.handle("y", agents=["ghost"])
    check("запись с ошибкой", "недоступен" in res4["results"][0]["error"])
    check("success=False", res4["success"] is False)

    print("== Тест 5: агент вернул неуспех ==")
    agent_fail = FakeAgent("failing", FakeResult(
        response="", success=False, exit_reason="budget_tokens",
        error="лимит токенов"))
    orch5 = Orchestrator(FakeLLM(), FakeRuntime({"failing": agent_fail}))
    res5 = await orch5.handle("z", agents=["failing"])
    e = res5["results"][0]
    check("success=False, exit_reason=budget_tokens",
          e["success"] is False and e["exit_reason"] == "budget_tokens")

    print("== Тест 6: journal-хуки без рекордера (no-op) ==")
    # journal.integration импортируется, RECORDER=None → мягкие хуки
    from src.journal import integration as jint
    check("RECORDER=None по умолчанию", jint.get_recorder() is None)
    res6 = await orch.handle("ж", agents=["alpha"])
    check("handle работает без журнала", res6["success"] is True)

    print(f"\nИтого: {ok_count} OK, {fail_count} FAIL")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
