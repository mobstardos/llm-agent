# -*- coding: utf-8 -*-
"""Безопасность v2: API-key авторизация + WS origin-check.

Единая точка проверки доступа для HTTP-мидлвари (src/app.py) и
WebSocket-хендлера (src/ws/chat.py).

Режимы работы:
- LLM_AGENT_API_KEY не задан → открытый доступ (локальная разработка).
  При старте сервер пишет предупреждение в лог (см. src/app.py).
- LLM_AGENT_API_KEY задан → все /api/* и /ws требуют ключ. Ключ можно
  передать любым способом:
    * заголовок X-API-Key: <key>
    * заголовок Authorization: Bearer <key>
    * query-параметр ?api_key=<key>  (удобно для первого входа из браузера)
    * cookie llm_agent_api_key (ставится автоматически при ?api_key=)

Открытые (не защищаемые) пути: корень UI, статика, документация, health.
Для WebSocket дополнительно проверяется Origin (LLM_AGENT_ALLOWED_ORIGINS,
по умолчанию разрешены localhost/127.0.0.1 — защита от cross-site WS hijack).
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

API_KEY = os.getenv("LLM_AGENT_API_KEY", "").strip()
COOKIE_NAME = "llm_agent_api_key"

#: Пути, которые не защищаем ключом (UI/статика/документация/health)
PUBLIC_PATHS = {
    "/", "/index.html", "/health", "/healthz", "/api/health",
    "/docs", "/redoc", "/openapi.json", "/favicon.ico",
}
PUBLIC_PREFIXES = ("/static", "/features", "/assets")

#: Разрешённые Origin для WS (через запятую в env). Пусто → localhost/127.0.0.1
_env_origins = os.getenv("LLM_AGENT_ALLOWED_ORIGINS", "").replace(";", ",")
ALLOWED_ORIGINS = {
    o.strip().rstrip("/") for o in _env_origins.split(",") if o.strip()
}
DEFAULT_LOCAL_ORIGINS = {
    "http://localhost", "https://localhost",
    "http://127.0.0.1", "https://127.0.0.1",
}


def is_public_path(path: str) -> bool:
    """UI/статика/документация/health — без ключа."""
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def extract_api_key(*, headers: dict | None = None,
                    query_params: dict | None = None,
                    cookies: dict | None = None) -> str:
    """Достаёт ключ из заголовков / query / cookie (первый непустой)."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    key = (h.get("x-api-key") or "").strip()
    if key:
        return key
    auth = (h.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
        if key:
            return key
    key = ((query_params or {}).get("api_key") or "").strip()
    if key:
        return key
    return ((cookies or {}).get(COOKIE_NAME) or "").strip()


def http_authorized(path: str, *, headers: dict | None = None,
                    query_params: dict | None = None,
                    cookies: dict | None = None) -> bool:
    """Проверка доступа к HTTP-пути. Без настроенного ключа → True."""
    if not API_KEY or is_public_path(path):
        return True
    provided = extract_api_key(headers=headers, query_params=query_params,
                               cookies=cookies)
    return bool(provided) and provided == API_KEY


def origin_allowed(origin: str | None, host: str | None = None) -> bool:
    """WS origin-check: нет Origin (нативный клиент) → разрешаем.

    Браузерный Origin должен быть localhost/127.0.0.1 либо входить в
    LLM_AGENT_ALLOWED_ORIGINS. Защита от cross-site WebSocket hijack.
    """
    if not origin:
        return True                      # нативные WS-клиенты без Origin
    origin = origin.rstrip("/")
    if origin in DEFAULT_LOCAL_ORIGINS or origin in ALLOWED_ORIGINS:
        return True
    parsed = urlparse(origin)
    hostname = (parsed.hostname or "").lower()
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    if host and hostname == host.lower().split(":")[0]:
        return True                      # same-host origin
    return False


def ws_authorized(*, headers: dict | None = None,
                  query_params: dict | None = None,
                  cookies: dict | None = None) -> tuple[bool, str]:
    """Проверка доступа к WS. Возвращает (ok, reason).

    Проверяет и API-key (если настроен), и Origin.
    """
    origin = (headers or {}).get("origin")
    if not origin_allowed(origin):
        return False, f"origin not allowed: {origin}"
    if not API_KEY:
        return True, ""
    provided = extract_api_key(headers=headers, query_params=query_params,
                               cookies=cookies)
    if provided == API_KEY:
        return True, ""
    return False, "invalid or missing API key (X-API-Key / ?api_key=)"
