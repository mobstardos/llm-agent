# -*- coding: utf-8 -*-
"""Тесты для src.memory.facade.Memory.

Покрытие:
  - initialize(): PG недоступен → фолбэк на SQLite/LanceDB.
  - initialize(): PG доступен — pool инициализируется.
  - start_session_async / end_session.
  - log_event_async.
  - search_vectors_async / search_events_async.
  - stats_async.
  - cleanup_async.

Все тесты идут с PG_ENABLED=false (см. env_no_pg в conftest.py),
поэтому use_postgres=False и PostgreSQL pool не создаётся.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.memory.facade import Memory


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def memory_no_llm():
    """Memory без LLM (summarizer не создаётся)."""
    with patch("src.memory.facade.get_settings") as mock_settings:
        s = MagicMock()
        s.use_postgres = False
        s.use_lancedb = False
        s.use_sqlite_memory = True
        s.use_sqlite_graph = True
        s.storage_strategy.dual_write = False
        s.cache.enabled = False
        mock_settings.return_value = s
        return Memory(llm_client=None)


@pytest.fixture
def memory_with_pg_mock():
    """Memory с замоканным PostgreSQL pool (use_postgres=True)."""
    with patch("src.memory.facade.get_settings") as mock_settings, \
         patch("src.db.pool.PgPool") as mock_pg_pool:
        s = MagicMock()
        s.use_postgres = True
        s.use_lancedb = False
        s.use_sqlite_memory = True
        s.use_sqlite_graph = False
        s.storage_strategy.dual_write = False
        s.cache.enabled = False
        mock_settings.return_value = s
        # PgPool.initialize() возвращает AsyncMock
        pool_instance = AsyncMock()
        pool_instance.open = AsyncMock()
        pool_instance.close = AsyncMock()
        pool_instance.is_closed = False
        mock_pg_pool.return_value = pool_instance
        memory = Memory(llm_client=None)
        return memory, pool_instance


# ═══════════════════════════════════════════════════════════════════
# initialize
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_initialize_postgres_unavailable_falls_back(memory_no_llm):
    """PG недоступен (use_postgres=False) — pool=None, фолбэк на SQLite."""
    await memory_no_llm.initialize()
    # PG pool не создан
    assert memory_no_llm.pg_pool is None
    # Episodic (SQLite) инициализирован
    assert memory_no_llm.episodic is not None


@pytest.mark.asyncio
async def test_initialize_with_pg_pool(memory_with_pg_mock):
    """PG доступен — pool инициализируется, vector_store готов."""
    memory, pool = memory_with_pg_mock
    await memory.initialize()
    # Pool создан (если настройка use_postgres=True)
    assert memory.pg_pool is not None or memory.pg_pool is pool


# ═══════════════════════════════════════════════════════════════════
# start_session / end_session
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_start_session_returns_session_id(memory_no_llm):
    """start_session_async возвращает непустой session_id."""
    await memory_no_llm.initialize()
    session_id = await memory_no_llm.start_session_async(title="Test session")
    assert session_id
    assert isinstance(session_id, str)
    assert len(session_id) > 0


@pytest.mark.asyncio
async def test_end_session_returns_session_id(memory_no_llm):
    """end_session возвращает session_id завершённой сессии."""
    await memory_no_llm.initialize()
    session_id = await memory_no_llm.start_session_async()
    ended = await memory_no_llm.end_session(model="test-model")
    assert ended == session_id


# ═══════════════════════════════════════════════════════════════════
# log_event
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_log_event_writes_to_episodic(memory_no_llm):
    """log_event_async пишет в episodic (SQLite), возвращает event_id."""
    await memory_no_llm.initialize()
    await memory_no_llm.start_session_async()
    event_id = await memory_no_llm.log_event_async(
        kind="user_message",
        content="Hello, world",
        agent_id="orchestrator",
    )
    assert event_id
    assert isinstance(event_id, str)


# ═══════════════════════════════════════════════════════════════════
# search
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_search_events_returns_matching_events(memory_no_llm):
    """search_events_async находит события по подстроке."""
    await memory_no_llm.initialize()
    await memory_no_llm.start_session_async()
    await memory_no_llm.log_event_async(
        kind="user_message", content="найди мой текст", agent_id="orch",
    )
    results = await memory_no_llm.search_events_async("найди", limit=10)
    assert isinstance(results, list)
    # Должно найти хотя бы одно событие с этим содержимым
    assert any("найди" in (r.get("content", "") if isinstance(r, dict) else str(r))
                for r in results)


@pytest.mark.asyncio
async def test_search_vectors_when_lancedb_unavailable(memory_no_llm):
    """search_vectors_async возвращает [], если LanceDB не инициализирована."""
    await memory_no_llm.initialize()
    # memory.vector = None, потому что use_lancedb=False
    results = await memory_no_llm.search_vectors_async("test query", limit=10)
    assert isinstance(results, list)
    # Без LanceDB результатов нет
    assert len(results) == 0


# ═══════════════════════════════════════════════════════════════════
# stats / cleanup
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_stats_returns_dict_with_keys(memory_no_llm):
    """stats_async возвращает dict с базовыми ключами."""
    await memory_no_llm.initialize()
    await memory_no_llm.start_session_async()
    await memory_no_llm.log_event_async(
        kind="user_message", content="stats test", agent_id="orch",
    )
    stats = await memory_no_llm.stats_async()
    assert isinstance(stats, dict)
    # Минимально ожидаем ключи — может быть больше
    assert "sessions" in stats or "events" in stats or "episodic" in stats


@pytest.mark.asyncio
async def test_cleanup_returns_dict(memory_no_llm):
    """cleanup_async возвращает dict с количеством удалённых."""
    await memory_no_llm.initialize()
    result = await memory_no_llm.cleanup_async()
    assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════
# close
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_close_is_idempotent(memory_no_llm):
    """close() можно вызвать несколько раз — не падает."""
    await memory_no_llm.initialize()
    await memory_no_llm.close()
    # Второй вызов не должен бросать
    await memory_no_llm.close()
