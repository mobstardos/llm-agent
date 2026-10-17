# -*- coding: utf-8 -*-
"""Тесты для src.orchestrator.Orchestrator.

Покрытие:
  - route(): empty runtime, valid JSON, markdown-fenced JSON, fallback search,
    unknown agent filter, LLM error, history parameter.
  - handle(): explicit agents, no agents (route first), aggregated result.

Покрытие кода по строкам: ~70% src/orchestrator.py (без journal хуков —
они мягкие и не вызываются в fake_runtime).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.orchestrator import Orchestrator
from tests.conftest import FakeLLM, FakeRuntime, make_route_json


# ═══════════════════════════════════════════════════════════════════
# route() — LLM-роутинг
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_route_empty_runtime_returns_no_agents(fake_llm: FakeLLM):
    """Если реестр пустой — route() не вызывает LLM и возвращает {agents: []}."""
    runtime = FakeRuntime(agent_ids=[])
    orch = Orchestrator(fake_llm, runtime)

    result = await orch.route("создай файл")

    assert result == {"agents": [], "reason": "Нет активных агентов"}
    assert len(fake_llm.call_log) == 0, "LLM не должен вызываться"


@pytest.mark.asyncio
async def test_route_with_valid_json_response(fake_llm: FakeLLM, fake_runtime: FakeRuntime):
    """LLM возвращает чистый JSON — route() парсит его и фильтрует unknown."""
    fake_llm.add_response(
        messages_substr="",
        content=make_route_json(["file", "git"], reason="файл+git"),
    )
    orch = Orchestrator(fake_llm, fake_runtime)

    result = await orch.route("сделай git commit после правки файла")

    assert result["agents"] == ["file", "git"]
    assert result["reason"] == "файл+git"


@pytest.mark.asyncio
async def test_route_with_markdown_fenced_json(fake_llm: FakeLLM, fake_runtime: FakeRuntime):
    """LLM заворачивает ответ в ```json ограждение — route() срезает его."""
    raw = "```json\n" + make_route_json(["file"], reason="ок") + "\n```"
    fake_llm.add_response(messages_substr="", content=raw)
    orch = Orchestrator(fake_llm, fake_runtime)

    result = await orch.route("прочитай файл")

    assert result["agents"] == ["file"]
    assert result["reason"] == "ок"


@pytest.mark.asyncio
async def test_route_with_fallback_json_search(fake_llm: FakeLLM, fake_runtime: FakeRuntime):
    """LLM пишет пояснительный текст + JSON — route() находит JSON через regex."""
    raw = (
        "Я считаю, что для этой задачи подойдёт файловый агент.\n"
        "Вот мой ответ: " + make_route_json(["file"], reason="файл") + "\n"
        "Надеюсь, это поможет."
    )
    fake_llm.add_response(messages_substr="", content=raw)
    orch = Orchestrator(fake_llm, fake_runtime)

    result = await orch.route("открой файл")

    assert result["agents"] == ["file"]


@pytest.mark.asyncio
async def test_route_filters_unknown_agents(fake_llm: FakeLLM, fake_runtime: FakeRuntime):
    """LLM назвал неизвестного агента — route() его отбрасывает."""
    fake_llm.add_response(
        messages_substr="",
        content=make_route_json(["file", "nonexistent_agent", "git"], reason=""),
    )
    orch = Orchestrator(fake_llm, fake_runtime)

    result = await orch.route("сделай что-нибудь")

    assert result["agents"] == ["file", "git"]
    assert "nonexistent_agent" not in result["agents"]


@pytest.mark.asyncio
async def test_route_when_llm_returns_no_known_agents(fake_llm: FakeLLM, fake_runtime: FakeRuntime):
    """LLM назвал только unknown — agents=[], reason=дефолтное сообщение."""
    fake_llm.add_response(
        messages_substr="",
        content=make_route_json(["unknown1", "unknown2"], reason=""),
    )
    orch = Orchestrator(fake_llm, fake_runtime)

    result = await orch.route("задача")

    assert result["agents"] == []
    assert "не выбрал" in result["reason"]


@pytest.mark.asyncio
async def test_route_handles_llm_error(fake_llm: FakeLLM, fake_runtime: FakeRuntime):
    """LLM бросает исключение — route() ловит и возвращает понятное сообщение."""
    fake_llm.add_error("", ConnectionError("Connection refused"))
    orch = Orchestrator(fake_llm, fake_runtime)

    result = await orch.route("задача")

    assert result["agents"] == []
    assert "Ошибка роутинга" in result["reason"]


@pytest.mark.asyncio
async def test_route_with_history_parameter(fake_llm: FakeLLM, fake_runtime: FakeRuntime):
    """history передаётся в user_content — LLM видит контекст диалога."""
    fake_llm.add_response(messages_substr="", content=make_route_json(["file"], reason=""))
    orch = Orchestrator(fake_llm, fake_runtime)

    await orch.route("теперь сделай git commit", history="предыдущий диалог: ...")

    # Проверяем, что history попала в messages
    assert len(fake_llm.call_log) == 1
    user_msg = fake_llm.call_log[0]["messages"][1]
    assert "предыдущий диалог" in user_msg["content"]


# ═══════════════════════════════════════════════════════════════════
# handle() — выполнение задачи
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_handle_with_explicit_agents_runs_each(
    fake_llm: FakeLLM, fake_runtime: FakeRuntime,
):
    """Если agents=[] передан — route() не вызывается, каждый агент выполняется."""
    orch = Orchestrator(fake_llm, fake_runtime)

    result = await orch.handle(
        "прочитай README.md",
        agents=["file", "git"],
        llm_client=fake_llm,
    )

    assert result["agents"] == ["file", "git"]
    assert fake_runtime.agents["file"].run_call_count == 1
    assert fake_runtime.agents["git"].run_call_count == 1
    # LLM не вызывался (агенты переданы явно)
    assert len(fake_llm.call_log) == 0


@pytest.mark.asyncio
async def test_handle_no_agents_routes_first(
    fake_llm: FakeLLM, fake_runtime: FakeRuntime,
):
    """agents=None — handle() сначала вызывает route(), потом выполняет."""
    fake_llm.add_response(messages_substr="", content=make_route_json(["file"], reason=""))
    orch = Orchestrator(fake_llm, fake_runtime)

    result = await orch.handle("прочитай файл", llm_client=fake_llm)

    assert result["agents"] == ["file"]
    assert fake_runtime.agents["file"].run_call_count == 1
    assert len(fake_llm.call_log) == 1


@pytest.mark.asyncio
async def test_handle_aggregated_result_contains_each_agent_output(
    fake_llm: FakeLLM, fake_runtime: FakeRuntime,
):
    """handle() агрегирует результаты — каждый агент в results[]."""
    fake_runtime.agents["file"].next_result = {"success": True, "content": "File OK"}
    fake_runtime.agents["git"].next_result = {"success": True, "content": "Git OK"}
    orch = Orchestrator(fake_llm, fake_runtime)

    result = await orch.handle(
        "task", agents=["file", "git"], llm_client=fake_llm,
    )

    contents = [r.get("content") for r in result.get("results", [])]
    assert "File OK" in contents
    assert "Git OK" in contents


@pytest.mark.asyncio
async def test_handle_agent_failure_does_not_block_others(
    fake_llm: FakeLLM, fake_runtime: FakeRuntime,
):
    """Один агент упал — остальные всё равно выполняются."""
    fake_runtime.agents["file"].next_result = {"success": False, "error": "boom"}
    fake_runtime.agents["git"].next_result = {"success": True, "content": "Git OK"}
    orch = Orchestrator(fake_llm, fake_runtime)

    result = await orch.handle(
        "task", agents=["file", "git"], llm_client=fake_llm,
    )

    # Оба агента отработали
    assert fake_runtime.agents["file"].run_call_count == 1
    assert fake_runtime.agents["git"].run_call_count == 1
    # Итоговый статус — ошибка (не все success)
    assert result.get("status") == "error"
