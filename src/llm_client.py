"""OpenAI-совместимый клиент: tool calling, стриминг, кэш, фолбэк.

Task 24-c: модель из селекта чата может принадлежать другому провайдеру
(DeepSeek / Ollama / OpenAI) — endpoint резолвится по config/models.yaml
(src/llm_providers.resolve_endpoint) и запрос уходит на нужный base_url.
Модели с supports_tools: false (локальная 1.5b) не получают tools —
крошечные модели стабильно ломают схему вызовов.

Task 24-d:
  * reasoning-модели (DeepSeek-R1/reasoner, QwQ) шлют ход мыслей отдельным
    полем delta.reasoning_content — собираем в result["reasoning"] и
    отдаём наверх через on_reasoning (сворачиваемый блок в UI);
  * авто-фолбэк: если модель недоступна (связь/ключ/модель удалена) и
    в UI ещё не ушёл ни один токен — пробуем selection.fallbacks из
    models.yaml; в UI уходит подсказка «переключаюсь на …».
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from src.cache import ResponseCache
from src.config import get_settings
from src.llm_providers import (
    endpoint_supports_tools, fallback_chain, resolve_endpoint,
)

logger = logging.getLogger(__name__)

TokenCallback = Callable[[str], Awaitable[None]]


def _msg_to_dict(msg: Any) -> dict:
    d: dict[str, Any] = {"role": "assistant", "content": msg.content}
    # Task 24-d: reasoning-модели возвращают ход мыслей отдельным полем
    rc = getattr(msg, "reasoning_content", None)
    if rc:
        d["reasoning"] = rc
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id, "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]
    return d


class LLMClient:
    def __init__(self, cache: ResponseCache | None = None):
        s = get_settings().llm
        # OpenAI-клиент требует непустой ключ даже при локальном qwenproxy —
        # подставляем заглушку, если пользователь оставил поле пустым.
        api_key = (s.api_key or "").strip() or "llm-agent-local"
        self.default_base_url = s.base_url
        self.client = AsyncOpenAI(api_key=api_key, base_url=s.base_url)
        self.model = s.model
        self.temperature = s.temperature
        self.cache = cache
        # Task 24-c: клиенты других провайдеров (DeepSeek/Ollama/OpenAI),
        # ключ — (base_url, api_key); создаются лениво, живут до конца процесса
        self._extra_clients: dict[tuple[str, str], AsyncOpenAI] = {}

        st = get_settings().streaming
        self.streaming_enabled = st.enabled
        self.replay_delay = st.replay_delay
        self.replay_speed = get_settings().cache.replay_speed

    def _client_for(
        self, model: str,
    ) -> tuple[AsyncOpenAI, str, dict | None]:
        """Клиент + модель + meta для конкретного имени модели.

        meta — запись из models.yaml (supports_tools/local/…) или None,
        если модель неизвестна (дефолтный провайдер из .env).
        """
        ep = resolve_endpoint(model)
        if ep is None:
            return self.client, model, None
        base, key, meta = ep
        # Task 27: веб-чаты по кукам — не OpenAI-клиент, обрабатывает
        # self._chat_web (base "web://provider")
        if base.startswith("web://"):
            return None, model, meta
        if not base or base.rstrip("/") == str(self.default_base_url).rstrip("/"):
            return self.client, model, meta
        ck = (base, key or "")
        cli = self._extra_clients.get(ck)
        if cli is None:
            cli = AsyncOpenAI(
                api_key=key.strip() or "llm-agent-local", base_url=base,
            )
            self._extra_clients[ck] = cli
            logger.info(
                "LLM endpoint для '%s': %s (provider=%s%s)",
                model, base, meta.get("provider"),
                ", локальная" if meta.get("local") else "",
            )
        return cli, model, meta

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        model: str | None = None,
        on_token: TokenCallback | None = None,
        on_reasoning: TokenCallback | None = None,
        state_hash: str | None = None,
    ) -> dict:
        primary = model or self.model
        use_cache = (
            self.cache is not None and get_settings().cache.enabled
        )

        cache_key = None
        if use_cache:
            cache_key = ResponseCache.make_key(
                primary, messages, tools, state_hash,
            )
            cached, hit = self.cache.get(cache_key)
            if hit and cached is not None:
                logger.debug("Cache HIT %s", cache_key[:12])
                if on_token:
                    await self._replay_cached(cached, on_token)
                return cached

        # Task 24-d: цепочка авто-фолбэка (максимум 3 попытки)
        attempts: list[str] = [primary]
        for m in fallback_chain(primary):
            if m and m not in attempts:
                attempts.append(m)
        attempts = attempts[:3]

        # в UI уже ушёл токен стрима — фолбэк нельзя (будет дубль текста)
        stream_started = {"v": False}
        last_exc: BaseException | None = None

        for i, m in enumerate(attempts):
            try:
                result = await self._attempt(
                    m, messages, tools, tool_choice,
                    on_token, on_reasoning, state_hash, stream_started,
                )
                if i > 0:
                    result["fallback_from"] = primary
                    result["model_used"] = m
                    logger.warning(
                        "Фолбэк: '%s' → '%s' (%s)",
                        primary, m, last_exc,
                    )
                if use_cache:
                    ck = (cache_key if m == primary else
                          ResponseCache.make_key(m, messages, tools, state_hash))
                    self.cache.set(ck, result)
                return result
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — фолбэк-логика
                last_exc = e
                if not self._should_fallback(e, stream_started["v"]):
                    raise
                nxt = attempts[i + 1] if i + 1 < len(attempts) else None
                if nxt and on_token:
                    hint = (
                        f"⚠️ {m} недоступна ({type(e).__name__}) — "
                        f"переключаюсь на {nxt}\n\n"
                    )
                    try:
                        await on_token(hint)
                    except Exception:
                        pass
                continue

        assert last_exc is not None
        raise last_exc

    # ── Одна попытка вызова конкретной модели ────────────────────────
    async def _attempt(
        self, model: str, messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None, tool_choice: str,
        on_token: TokenCallback | None, on_reasoning: TokenCallback | None,
        state_hash: str | None, stream_started: dict,
    ) -> dict:
        client, model, meta = self._client_for(model)
        # Локальные крошки (supports_tools: false) не умеют tool calling —
        # молча убираем инструменты, иначе модель ломает схему вызовов.
        if tools and not endpoint_supports_tools(model):
            logger.debug("Модель '%s' без tools — вызываю без инструментов",
                         model)
            tools = None

        # Task 27: веб-чат по кукам (DeepSeek/Qwen) — свой транспорт
        if meta and meta.get("web_provider"):
            return await self._chat_web(
                meta["web_provider"], messages,
                on_token, stream_started,
            )

        if on_token is None:
            result = await self._chat_sync(
                client, messages, tools, tool_choice, model,
            )
        else:
            result = await self._chat_stream(
                client, messages, tools, tool_choice, model,
                on_token, on_reasoning, stream_started,
            )
        return result

    @staticmethod
    def _should_fallback(exc: BaseException, stream_started: bool) -> bool:
        """Можно ли честно повторить запрос другой моделью.

        Нет — если стрим уже отдавал токены в UI (частичный ответ) или
        ошибка про переполнение контекста (другая модель не спасёт без
        изменения запроса). Да — связь/ключ/модель/лимиты/5xx.
        """
        if stream_started:
            return False
        body = str(exc).lower()
        ctx_keys = (
            "context length", "maximum context", "context_length_exceeded",
            "too many tokens", "reduce the length", "prompt is too long",
            "input length exceeds", "max input tokens",
        )
        if any(k in body for k in ctx_keys):
            return False
        return True

    async def _chat_web(
        self, provider: str, messages: list[dict[str, Any]],
        on_token: TokenCallback | None,
        stream_started: dict | None,
    ) -> dict:
        """Веб-чат по кукам (Task 27): deepseek_web / qwen_web.

        Транспорт — src/web_chat.web_chat (SSE сайта); форма результата
        совпадает с _chat_sync/_chat_stream, чтобы не ломать кэш/фолбэк.
        """
        from src.web_chat import web_chat
        return await web_chat(
            provider, messages,
            on_token=on_token, stream_started=stream_started,
        )

    async def _chat_sync(
        self, client: AsyncOpenAI, messages: list[dict],
        tools: list[dict] | None, tool_choice: str, model: str,
    ) -> dict:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        response = await client.chat.completions.create(**kwargs)
        d = _msg_to_dict(response.choices[0].message)
        d["chunks"] = []
        d["streamed"] = False
        return d

    async def _chat_stream(
        self, client: AsyncOpenAI, messages: list[dict],
        tools: list[dict] | None, tool_choice: str, model: str,
        on_token: TokenCallback,
        on_reasoning: TokenCallback | None = None,
        stream_started: dict | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {
            "model": model, "messages": messages,
            "temperature": self.temperature, "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        stream = await client.chat.completions.create(**kwargs)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        chunks_log: list[dict] = []
        t_prev = time.monotonic()

        async def _emit(text: str) -> None:
            if stream_started is not None:
                stream_started["v"] = True
            try:
                await on_token(text)
            except Exception as e:
                logger.warning("on_token: %s", e)

        async def _emit_reasoning(text: str) -> None:
            if stream_started is not None:
                stream_started["v"] = True
            if on_reasoning:
                try:
                    await on_reasoning(text)
                except Exception as e:
                    logger.warning("on_reasoning: %s", e)

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # Task 24-d: ход мыслей reasoning-моделей (R1/QwQ/reasoner).
            # Некоторые провайдеры кладут его в delta.reasoning.
            rc = getattr(delta, "reasoning_content", None) \
                or getattr(delta, "reasoning", None)
            if isinstance(rc, str) and rc:
                reasoning_parts.append(rc)
                await _emit_reasoning(rc)

            if delta.content:
                now = time.monotonic()
                delay = now - t_prev
                t_prev = now
                content_parts.append(delta.content)
                chunks_log.append({"delay": round(delay, 4),
                                   "text": delta.content})
                await _emit(delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index if tc.index is not None else 0
                    acc = tool_calls_acc.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""},
                    )
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            acc["name"] = tc.function.name
                        if tc.function.arguments:
                            acc["arguments"] += tc.function.arguments

        result: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
            "chunks": chunks_log,
            "streamed": True,
        }
        if reasoning_parts:
            result["reasoning"] = "".join(reasoning_parts)
        if tool_calls_acc:
            result["tool_calls"] = [
                {
                    "id": v["id"] or f"call_{i}",
                    "type": "function",
                    "function": {"name": v["name"],
                                 "arguments": v["arguments"]},
                }
                for i, v in sorted(tool_calls_acc.items())
            ]
        return result

    async def _replay_cached(
        self, cached: dict, on_token: TokenCallback,
    ) -> None:
        chunks = cached.get("chunks") or []
        if chunks:
            for c in chunks:
                delay = max(
                    0.0,
                    float(c.get("delay", 0.0))
                    / max(self.replay_speed, 0.01),
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                text = c.get("text", "")
                if text:
                    try:
                        await on_token(text)
                    except Exception:
                        return
            return

        content = cached.get("content") or ""
        for i in range(0, len(content), 4):
            try:
                await on_token(content[i:i + 4])
            except Exception:
                return
            await asyncio.sleep(self.replay_delay)
