# -*- coding: utf-8 -*-
"""Общие фикстуры для тестов llm-agent.

Эти фикстуры делают тесты изолированными от реальных подсистем:
- env_no_pg — отключает PostgreSQL (тесты идут на SQLite/LanceDB)
- tmp_data_dir — временный каталог для данных (очищается после теста)
- FakeLLM — заглушка LLMClient, возвращает предзаписанные ответы
- FakeRuntime — заглушка AgentRuntime с заданным набором агентов
- FakeMCP — заглушка MCPManager
- fake_settings — предзаполненный AppSettings без реальных ключей
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Окружение — отключаем все внешние подсистемы ─────────────────────
@pytest.fixture(autouse=True)
def env_no_pg(monkeypatch, tmp_path):
    """Отключает PostgreSQL, Ollama, AGENT_MEMORY в тестах.

    Все тесты должны проходить без реальных БД, LLM-API, Ollama, Kafka.
    Авто-применяется ко всем тестам (autouse=True).
    """
    monkeypatch.setenv("PG_ENABLED", "false")
    monkeypatch.setenv("PG_REPLICATE", "0")
    monkeypatch.setenv("AGENT_MEMORY", "0")
    monkeypatch.setenv("BACKUP_ENABLED", "false")
    # Корень проекта — временный каталог (чтобы не писать в /home/...)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    # DeepSeek/OpenAI ключи — заглушки (реальных вызовов не будет)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    # Базовый URL — локальный mock
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    # Отключаем LanceDB (если нет колёс — тесты всё равно пройдут)
    monkeypatch.setenv("PRIMARY_VECTOR_STORE", "sqlite")
    # Кэш — в temp
    monkeypatch.setenv("CACHE_PATH", str(tmp_path / "cache.sqlite"))
    # Журнал — в temp
    monkeypatch.setenv("JOURNAL_DB_PATH", str(tmp_path / "journal.sqlite"))
    # Политики — в temp
    monkeypatch.setenv("POLICIES_PATH", str(tmp_path / "policies.sqlite"))
    yield


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    """Алиас tmp_path — так зовут тесты test_cookies_24a и др."""
    return tmp_path


@pytest.fixture
def tmp_data_dir(tmp_path) -> Path:
    """Создаёт временную директорию data/ с подкаталогами."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "profiles").mkdir(exist_ok=True)
    (data / "sessions").mkdir(exist_ok=True)
    (data / "sessions" / "dump").mkdir(exist_ok=True)
    return data


# ─── FakeLLM — заглушка src.llm_client.LLMClient ─────────────────────
class FakeLLM:
    """Лёгкая заглушка LLMClient для тестов orchestrator/handle.

    Возвращает предзаписанные ответы по ключу (model, messages_hash).
    Если ответ не найден — возвращает fallback или бросает заданное исключение.

    Использование:
        fake = FakeLLM()
        fake.add_response(messages_substr="Hello", content="Hi there")
        orch = Orchestrator(fake, runtime)
    """

    def __init__(self):
        self.responses: list[tuple[str, dict]] = []
        # (messages_substr, response_dict)
        self.errors: list[tuple[str, Exception]] = []
        self.call_log: list[dict] = []

    def add_response(self, messages_substr: str = "", **kwargs):
        """Добавить ответ, который вернётся при совпадении messages.

        kwargs формируют dict: content=..., tool_calls=..., reasoning=...
        """
        self.responses.append((messages_substr, kwargs))

    def add_error(self, messages_substr: str, exc: Exception):
        self.errors.append((messages_substr, exc))

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        model: str | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning: Callable[[str], Awaitable[None]] | None = None,
        state_hash: str | None = None,
    ) -> dict:
        self.call_log.append({
            "messages": messages, "tools": tools, "model": model,
        })
        user_content = ""
        for m in messages:
            if m.get("role") == "user":
                user_content = m.get("content", "")
                break

        # Сначала проверяем errors
        for substr, exc in self.errors:
            if substr in user_content:
                raise exc

        # Потом responses — первое совпадение
        for substr, resp in self.responses:
            if substr == "" or substr in user_content:
                return dict(resp)

        # Дефолтный ответ
        return {"content": "DEFAULT", "tool_calls": [], "reasoning": ""}

    def _client_for(self, model: str):
        return None, model, None


# ─── FakeRuntime — заглушка src.agents.runtime.AgentRuntime ──────────
class FakeRuntime:
    """Заглушка AgentRuntime: содержит только список agent_id.

    Не запускает реальные loop'ы. Используется для тестов route().
    """

    def __init__(self, agent_ids: list[str] | None = None):
        # agents — dict[str, FakeAgent]
        self.agents = {aid: FakeAgent(aid) for aid in (agent_ids or [])}

    def get(self, agent_id: str):
        return self.agents.get(agent_id)

    def list_ids(self):
        return sorted(self.agents.keys())

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self.agents

    def __len__(self) -> int:
        return len(self.agents)


class FakeAgent:
    """Заглушка BaseAgent для тестов handle()."""

    def __init__(self, agent_id: str):
        self.id = agent_id
        self.run_call_count = 0
        self.run_call_args: list[dict] = []
        self.next_result: dict = {"success": True, "content": f"Result from {agent_id}"}

    async def run(self, query: str, **kwargs) -> dict:
        self.run_call_count += 1
        self.run_call_args.append({"query": query, **kwargs})
        return dict(self.next_result)


# ─── FakeMCP — заглушка MCPManager ──────────────────────────────────
class FakeMCP:
    """Заглушка MCPManager.call_tool — возвращает заглушку результата."""

    def __init__(self):
        self.calls: list[dict] = []

    async def call_tool(self, server: str, tool: str, args: dict, **kwargs):
        self.calls.append({"server": server, "tool": tool, "args": args})
        return {"ok": True, "result": f"fake-{server}.{tool}"}

    async def start(self, _):
        pass

    async def stop(self):
        pass


# ─── Pytest fixtures registry ────────────────────────────────────────
@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def fake_runtime():
    return FakeRuntime(agent_ids=["file", "git", "shell"])


@pytest.fixture
def fake_mcp():
    return FakeMCP()


@pytest.fixture
def event_loop():
    """Переопределённый event_loop, чтобы избежать DeprecationWarning.

    pytest-asyncio 0.24+ требует явной политики. На всякий случай.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ─── Helpers ─────────────────────────────────────────────────────────
def make_chat_response(content: str = "", tool_calls: list | None = None,
                       reasoning: str = "") -> dict:
    """Сформировать dict-ответ LLM (как возвращает AsyncOpenAI.chat.completions)."""
    return {
        "content": content,
        "tool_calls": tool_calls or [],
        "reasoning": reasoning,
    }


def make_route_json(agents: list[str], reason: str = "") -> str:
    """Сформировать JSON-ответ роутера (как LLM возвращает в content)."""
    return json.dumps({"agents": agents, "reason": reason})
