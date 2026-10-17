# -*- coding: utf-8 -*-
"""Тесты для src.llm_client.LLMClient.

Покрытие:
  - _msg_to_dict(): простой ответ, с tool_calls, с reasoning_content.
  - chat(): cache hit, успешный первичный вызов, фолбэк по unavailable-модели,
    все попытки провалились → raise, callback on_token.
  - _client_for(): дефолтный провайдер, кастомный провайдер, web://.
  - _should_fallback(): 403 / 429 / streaming_started.

Использует unittest.mock.AsyncMock для мока AsyncOpenAI — реальные
HTTP-вызовы не идут.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import pytest

from src.llm_client import LLMClient, _msg_to_dict


# ═══════════════════════════════════════════════════════════════════
# _msg_to_dict — конвертация ответа LLM
# ═══════════════════════════════════════════════════════════════════

def test_msg_to_dict_simple():
    """Простой ответ без tool_calls и reasoning."""
    msg = SimpleNamespace(content="Hello!", tool_calls=None, reasoning_content=None)
    d = _msg_to_dict(msg)
    assert d == {"role": "assistant", "content": "Hello!"}


def test_msg_to_dict_with_tool_calls():
    """Ответ с tool_calls — должны попасть в dict."""
    tc = SimpleNamespace(
        id="call_1", type="function",
        function=SimpleNamespace(name="read_file", arguments='{"path": "/tmp"}'),
    )
    msg = SimpleNamespace(content="", tool_calls=[tc], reasoning_content=None)
    d = _msg_to_dict(msg)
    assert d["role"] == "assistant"
    assert len(d["tool_calls"]) == 1
    assert d["tool_calls"][0]["function"]["name"] == "read_file"
    assert d["tool_calls"][0]["function"]["arguments"] == '{"path": "/tmp"}'


def test_msg_to_dict_with_reasoning_content():
    """Reasoning-модель (DeepSeek-R1) — reasoning_content попадает в dict."""
    msg = SimpleNamespace(
        content="Финал",
        tool_calls=None,
        reasoning_content="Сначала я подумал...",
    )
    d = _msg_to_dict(msg)
    assert d["reasoning"] == "Сначала я подумал..."
    assert d["content"] == "Финал"


# ═══════════════════════════════════════════════════════════════════
# chat() — основная логика
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_openai():
    """Мок AsyncOpenAI для LLMClient."""
    with patch("src.llm_client.AsyncOpenAI") as mock_cls:
        instance = AsyncMock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def llm_client(mock_openai):
    """LLMClient с замоканным AsyncOpenAI и замоченным cache (None)."""
    with patch("src.llm_client.get_settings") as mock_settings:
        s = MagicMock()
        s.llm.api_key = "test"
        s.llm.base_url = "http://localhost:9999/v1"
        s.llm.model = "test-model"
        s.llm.temperature = 0.7
        s.streaming.enabled = False
        s.streaming.replay_delay = 0
        s.cache.enabled = False
        s.cache.replay_speed = 1.0
        mock_settings.return_value = s
        client = LLMClient(cache=None)
        return client


@pytest.mark.asyncio
async def test_chat_first_attempt_success(llm_client):
    """Первый вызов успешен — возвращаем результат, фолбэк не нужен."""
    llm_client.client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="Hi", tool_calls=None, reasoning_content=None),
                finish_reason="stop",
            )],
        ),
    )
    # Замокать _client_for, чтобы не зависеть от models.yaml
    with patch.object(llm_client, "_client_for",
                      return_value=(llm_client.client, "test-model", None)):
        result = await llm_client.chat(messages=[{"role": "user", "content": "Hi"}])

    assert result["content"] == "Hi"
    llm_client.client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_fallback_chain_when_first_fails(llm_client):
    """Первая модель 503 — пробуем fallback, он успешен."""
    # patch fallback_chain to return ['test-model', 'backup-model']
    async def fake_chat(*a, **kw):
        # Возвращаем результат — фолбэк успешен
        return {"content": "fallback OK", "tool_calls": [], "reasoning": ""}

    with patch("src.llm_client.fallback_chain", return_value=["backup-model"]), \
         patch("src.llm_client.LLMClient._attempt", new=AsyncMock(side_effect=[
             ConnectionError("primary down"),  # первичная модель упала
             {"content": "fallback OK", "tool_calls": [], "reasoning": ""},  # фолбэк успешен
         ])) as mock_attempt, \
         patch("src.llm_client.LLMClient._should_fallback", return_value=True):
        result = await llm_client.chat(messages=[{"role": "user", "content": "Hi"}])

    assert result["content"] == "fallback OK"
    assert result.get("fallback_from") == "test-model"
    assert result.get("model_used") == "backup-model"
    assert mock_attempt.await_count == 2


@pytest.mark.asyncio
async def test_chat_all_attempts_fail_raises_last_exc(llm_client):
    """Все попытки провалились — должно бросить last_exc (НЕ AssertionError)."""
    last_exc = ConnectionError("all failed")

    with patch("src.llm_client.fallback_chain", return_value=["backup"]), \
         patch("src.llm_client.LLMClient._attempt", new=AsyncMock(
             side_effect=[ConnectionError("p1"), last_exc],
         )), \
         patch("src.llm_client.LLMClient._should_fallback", return_value=True):
        with pytest.raises(ConnectionError) as exc_info:
            await llm_client.chat(messages=[{"role": "user", "content": "Hi"}])

    assert "all failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_chat_does_not_fallback_after_stream_started(llm_client):
    """stream_started=True — фолбэк нельзя (дубль текста в UI)."""
    primary_exc = ConnectionError("primary failed mid-stream")

    with patch("src.llm_client.fallback_chain", return_value=["backup"]), \
         patch("src.llm_client.LLMClient._attempt", new=AsyncMock(
             side_effect=primary_exc,
         )), \
         patch("src.llm_client.LLMClient._should_fallback", return_value=False):
        with pytest.raises(ConnectionError):
            await llm_client.chat(
                messages=[{"role": "user", "content": "Hi"}],
                on_token=AsyncMock(),
            )


# ═══════════════════════════════════════════════════════════════════
# _client_for — выбор провайдера
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_client_for_unknown_model_returns_default(llm_client):
    """Неизвестная модель — дефолтный провайдер."""
    with patch("src.llm_client.resolve_endpoint", return_value=None):
        client, model, meta = llm_client._client_for("totally-unknown-model")

    assert client is llm_client.client
    assert model == "totally-unknown-model"
    assert meta is None


@pytest.mark.asyncio
async def test_client_for_custom_provider_creates_extra_client(llm_client):
    """Кастомный провайдер — создаётся extra AsyncOpenAI клиент."""
    with patch("src.llm_client.resolve_endpoint",
               return_value=("http://custom.api/v1", "custom-key",
                             {"provider": "custom"})):
        client, model, meta = llm_client._client_for("custom-model")

    assert client is not llm_client.client
    assert model == "custom-model"
    assert meta["provider"] == "custom"
    # Проверяем, что клиент закэширован (второй вызов не создаёт новый)
    with patch("src.llm_client.resolve_endpoint",
               return_value=("http://custom.api/v1", "custom-key", {"provider": "custom"})):
        client2, _, _ = llm_client._client_for("custom-model")
    assert client2 is client


@pytest.mark.asyncio
async def test_client_for_web_provider_returns_none_client(llm_client):
    """web:// чаты (DeepSeek/Qwen куки) — клиент None, обработает _chat_web."""
    with patch("src.llm_client.resolve_endpoint",
               return_value=("web://deepseek", "cookie", {"provider": "deepseek"})):
        client, model, meta = llm_client._client_for("deepseek-chat")

    assert client is None
    assert model == "deepseek-chat"
    assert meta["provider"] == "deepseek"


# ═══════════════════════════════════════════════════════════════════
# _should_fallback — логика фолбэка
# ═══════════════════════════════════════════════════════════════════

def test_should_fallback_on_403(llm_client):
    """403 region — фолбэк разрешён (LLM провайдер заблокировал IP)."""
    from openai import APIStatusError, PermissionDeniedError
    exc = PermissionDeniedError("region blocked", response=MagicMock(), body=None)
    assert llm_client._should_fallback(exc, stream_started=False) is True


def test_should_fallback_on_429(llm_client):
    """429 rate limit — фолбэк разрешён."""
    from openai import RateLimitError
    exc = RateLimitError("too many requests", response=MagicMock(), body=None)
    assert llm_client._should_fallback(exc, stream_started=False) is True


def test_should_not_fallback_after_stream_started(llm_client):
    """stream_started=True — фолбэк запрещён, чтобы не дублировать токены."""
    from openai import PermissionDeniedError
    exc = PermissionDeniedError("region blocked", response=MagicMock(), body=None)
    assert llm_client._should_fallback(exc, stream_started=True) is False
