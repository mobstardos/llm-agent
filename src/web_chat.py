"""Веб-чаты DeepSeek/Qwen по кукам из расширения Bridge — Task 27.

Куки собирает расширение «LLM Agent Bridge» (Task 24-a) → POST
/api/bridge/cookies → env (DEEPSEEK_*, QWEN_WEB_*). Когда они есть,
в списке моделей появляются провайдеры deepseek_web / qwen_web
(llm_providers.web_cookie_providers), а LLMClient маршрутизирует
запросы сюда (meta["web_provider"]).

DeepSeek: официальный приватный API веб-чата — тот же, что у MCP-сервера
src/mcp_servers/deepseek/server.py (chat_session/create → chat/completion,
SSE). Разговор сворачивается в один промпт (у веб-чата нет messages-массива).

Qwen: chat.qwen.ai — Bearer-токен из куки token; эндпоинт /api/chat/completion
(SSE). Формат парсится устойчиво (несколько известных форм дельты) — при
ошибке наверх уходит исключение и срабатывает штатный фолбэк LLMClient.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

TokenCallback = Callable[[str], Awaitable[None]]

DEEPSEEK_BASE = "https://chat.deepseek.com"
QWEN_BASE = "https://chat.qwen.ai"
THUMBCACHE_NAME = ".thumbcache_6b2e5483f9d858d7c661c5e276b6a6ae"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ── Публичный статус (для списка моделей) ─────────────────────────────

DEEPSEEK_ENV = ("DEEPSEEK_AUTH_TOKEN", "DEEPSEEK_DS_SESSION_ID",
                "DEEPSEEK_SMIDV2")
QWEN_ENV = ("QWEN_WEB_TOKEN", "QWEN_WEB_COOKIES")


def web_cookie_available(provider: str) -> bool:
    """Есть ли куки провайдера в окружении (без чтения значений)."""
    envs = DEEPSEEK_ENV if provider == "deepseek" else QWEN_ENV
    return any(os.getenv(e, "").strip() for e in envs)


def _cookie_header(provider: str) -> str:
    """Cookie-строка из env (qwen — целиком из QWEN_WEB_COOKIES)."""
    if provider == "deepseek":
        parts: list[str] = []
        if v := os.getenv("DEEPSEEK_DS_SESSION_ID", "").strip():
            parts.append(f"ds_session_id={v}")
        if v := os.getenv("DEEPSEEK_SMIDV2", "").strip():
            parts.append(f"smidV2={v}")
        if v := os.getenv("DEEPSEEK_THUMBCACHE", "").strip():
            parts.append(f"{THUMBCACHE_NAME}={v}")
        return "; ".join(parts)
    return os.getenv("QWEN_WEB_COOKIES", "").strip()


def _bearer(provider: str) -> str:
    if provider == "deepseek":
        return (os.getenv("DEEPSEEK_AUTH_TOKEN", "").strip()
                or os.getenv("DEEPSEEK_SMIDV2", "").strip())
    return (os.getenv("QWEN_WEB_TOKEN", "").strip()
            or _cookie_token_from_header())


def _cookie_token_from_header() -> str:
    """token=... внутри QWEN_WEB_COOKIES."""
    for pair in os.getenv("QWEN_WEB_COOKIES", "").split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() == "token":
                return v.strip()
    return ""


def _base_headers(provider: str) -> dict[str, str]:
    base = DEEPSEEK_BASE if provider == "deepseek" else QWEN_BASE
    h = {
        "accept": "*/*",
        "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": base,
        "referer": f"{base}/",
        "user-agent": _UA,
    }
    if provider == "deepseek":
        h["x-app-version"] = "20241129.1"
        h["x-client-platform"] = "web"
        h["x-client-version"] = "1.0.0-always"
    ck = _cookie_header(provider)
    if ck:
        h["cookie"] = ck
    tok = _bearer(provider)
    if tok:
        h["authorization"] = f"Bearer {tok}"
    return h


# ── Сериализация диалога в один промпт ────────────────────────────────

def _flatten_prompt(messages: list[dict[str, Any]]) -> str:
    """Веб-чат принимает один текст: историю приклеиваем к вопросу."""
    sys_parts = [str(m.get("content") or "") for m in messages
                 if m.get("role") == "system"]
    convo = [m for m in messages if m.get("role") in ("user", "assistant")]
    convo = [m for m in convo if (m.get("content") or "").strip()]
    last_user = ""
    if convo and convo[-1].get("role") == "user":
        last_user = str(convo[-1].get("content") or "")
        convo = convo[:-1]

    parts: list[str] = []
    if sys_parts:
        parts.append("\n".join(p for p in sys_parts if p.strip()))
    if convo:
        lines = []
        for m in convo[-10:]:
            who = "Пользователь" if m.get("role") == "user" else "Ассистент"
            text = str(m.get("content") or "").replace("\n", " ")[:400]
            lines.append(f"{who}: {text}")
        parts.append("История диалога:\n" + "\n".join(lines))
    parts.append(last_user or "Продолжай.")
    return "\n\n".join(p for p in parts if p.strip())


# ── Парсер SSE-дельт (устойчивый к форматам) ──────────────────────────

def _extract_piece(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    for key in ("content", "text"):
        v = obj.get(key)
        if isinstance(v, str) and v:
            return v
    v = obj.get("v")
    if isinstance(v, dict):
        for key in ("response", "content", "text", "delta"):
            val = v.get(key)
            if isinstance(val, str) and val:
                return val
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
        c = delta.get("content")
        if isinstance(c, str) and c:
            return c
    return ""


async def _stream_sse(
    client: httpx.AsyncClient, method: str, url: str, headers: dict,
    body: dict | None, on_token: TokenCallback | None,
    stream_started: dict | None, timeout: float = 180.0,
    label: str = "",
) -> str:
    """POST с SSE-ответом; текст по кусочно уходит в on_token."""
    chunks: list[str] = []
    async with client.stream(
        method, url, headers=headers, json=body, timeout=timeout,
    ) as resp:
        if resp.status_code >= 400:
            err = await resp.aread()
            text = err.decode("utf-8", "replace")[:300]
            if resp.status_code in (401, 403):
                raise RuntimeError(
                    f"{label} HTTP {resp.status_code}: куки/токен отклонены "
                    f"— протухли или не те. Обновите: расширение Bridge → "
                    f"ключ (DeepSeek/Qwen) → войдите в чат заново; статус: "
                    f"вкладка «Мост» (/api/bridge/cookies/status). "
                    f"Сайт ответил: {text}"
                )
            raise RuntimeError(f"{label} HTTP {resp.status_code}: {text}")
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            piece = _extract_piece(obj)
            if piece:
                chunks.append(piece)
                if on_token:
                    if stream_started is not None:
                        stream_started["v"] = True
                    try:
                        await on_token(piece)
                    except Exception as e:
                        logger.warning("on_token: %s", e)
    return "".join(chunks).strip()


# ── Провайдеры ────────────────────────────────────────────────────────

async def _deepseek_chat(
    messages: list[dict[str, Any]], on_token: TokenCallback | None,
    stream_started: dict | None, thinking: bool = False,
) -> str:
    prompt = _flatten_prompt(messages)
    headers = _base_headers("deepseek")
    async with httpx.AsyncClient(
        timeout=180.0, follow_redirects=True,
    ) as client:
        r = await client.post(
            f"{DEEPSEEK_BASE}/api/v0/chat_session/create",
            headers=headers, json={},
        )
        if r.status_code >= 400:
            if r.status_code in (401, 403):
                raise RuntimeError(
                    f"DeepSeek HTTP {r.status_code}: куки/токен отклонены "
                    f"— протухли или не те. Обновите: расширение Bridge → "
                    f"ключ DeepSeek → войдите в чат заново; статус: "
                    f"вкладка «Мост» (/api/bridge/cookies/status). "
                    f"Сайт ответил: {r.text[:300]}"
                )
            raise RuntimeError(
                f"DeepSeek HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"DeepSeek API error: {data}")
        sid = (data.get("data", {}).get("biz_data", {})
               .get("chat_session", {}).get("id"))
        if not sid:
            raise RuntimeError("DeepSeek: нет chat_session id")
        body = {
            "chat_session_id": sid,
            "parent_message_id": None,
            "prompt": prompt,
            "ref_file_ids": [],
            "thinking_enabled": bool(thinking),
            "search_enabled": False,
        }
        return await _stream_sse(
            client, "POST", f"{DEEPSEEK_BASE}/api/v0/chat/completion",
            headers, body, on_token, stream_started, label="deepseek",
        )


def _qwen_model() -> str:
    """Модель Qwen web (env QWEN_WEB_MODEL; дефолт — как в веб-клиенте)."""
    return os.getenv("QWEN_WEB_MODEL", "").strip() or "qwen3-max"


async def _qwen_chat(
    messages: list[dict[str, Any]], on_token: TokenCallback | None,
    stream_started: dict | None,
) -> str:
    prompt = _flatten_prompt(messages)
    headers = _base_headers("qwen")
    body = {
        "fid": uuid.uuid4().hex,
        "gid": "",
        "model": _qwen_model(),
        "messages": [{"role": "user", "content": prompt}],
        "chat_type": "t2t",
        "timestamp": int(time.time() * 1000),
    }
    async with httpx.AsyncClient(
        timeout=180.0, follow_redirects=True,
    ) as client:
        return await _stream_sse(
            client, "POST", f"{QWEN_BASE}/api/chat/completion",
            headers, body, on_token, stream_started, label="qwen",
        )


async def web_chat(
    provider: str, messages: list[dict[str, Any]],
    on_token: TokenCallback | None = None,
    stream_started: dict | None = None,
    thinking: bool = False,
) -> dict[str, Any]:
    """Ответ веб-чата; форма результата совпадает с LLMClient.chat()."""
    if provider == "deepseek":
        if not web_cookie_available("deepseek"):
            raise RuntimeError(
                "Нет кук DeepSeek — откройте страницу чата в расширении Bridge")
        text = await _deepseek_chat(
            messages, on_token, stream_started, thinking)
    elif provider == "qwen":
        if not web_cookie_available("qwen"):
            raise RuntimeError(
                "Нет кук Qwen — откройте страницу чата в расширении Bridge")
        text = await _qwen_chat(messages, on_token, stream_started)
    else:
        raise RuntimeError(f"Неизвестный веб-провайдер: {provider}")
    return {
        "role": "assistant",
        "content": text or "(пустой ответ веб-чата)",
        "chunks": [],
        "streamed": bool(on_token is not None),
        "web_provider": provider,
    }
