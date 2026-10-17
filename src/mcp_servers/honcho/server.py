"""MCP-сервер: Honcho — межсессионная память агентов (Plastic Labs).

Прокси инструментов Honcho (workspace → peers → sessions → conclusions;
representations, peer cards, dreams) к официальному MCP-эндпоинту Honcho:
облако https://mcp.honcho.dev или self-hosted (mcp/ пакет репозитория
plastic-labs/honcho: HTTP `bun run http` → :3000 или Docker honcho-mcp
рядом с api:8000). Honcho — open-source «AI memory for agents»
(AGPL-3.0, SOTA на LongMemEval): помнит пользователя между диалогами.

Транспорт: MCP Streamable HTTP С СОСТОЯНИЕМ СЕАНСА (по образцу onec_qa):
  1. POST initialize → ответ содержит заголовок Mcp-Session-Id. Если
     upstream работает без сеансов (stateless) — заголовка может не быть,
     работаем без него (адаптация Honcho: hosted-эндпоинт может быть
     stateless, в отличие от контейнера qa_mcp).
  2. POST notifications/initialized (ответ 202 игнорируется).
  3. POST tools/call; ответ — application/json или SSE-поток
     (text/event-stream) — парсим data:-строки.
  4. 404/потеря сеанса → re-initialize и ОДИН ретрай вызова.

Авторизация: Authorization: Bearer HONCHO_API_KEY на КАЖДЫЙ запрос,
включая initialize (ключ формата hch-… выдаётся на app.honcho.dev).
Если задан HONCHO_WORKSPACE_ID — каждый запрос сопровождается заголовком
X-Honcho-Workspace-ID; иначе workspace_id передаётся в аргументах.

Таймауты: 5 с подключение/initialize, 30 с обычные операции,
120 с chat/workspace_chat (живой reasoning Honcho — ответ приходит за
5+ секунд и дольше при высоком reasoning_level).

list_tools: динамический passthrough — tools/list запрашивается из
upstream (с TTL-кэшем); если upstream недоступен (нет сети/ключа) —
статический фолбэк-каталог: 20 инструментов официального MCP Honcho +
honcho_tools_list (работает офлайн). tools/call — чистый passthrough
(имя + аргументы), валидация аргументов на стороне upstream.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from itertools import count

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("honcho-mcp")

CONNECT_TIMEOUT = 5.0    # подключение к upstream должно падать быстро
INIT_TIMEOUT = 5.0       # initialize (вместе с connect — до 10 с worst case)
DEFAULT_TIMEOUT = 30.0   # обычные операции (context, search, lists)
CHAT_TIMEOUT = 120.0     # chat/workspace_chat — живой reasoning Honcho
MAX_TEXT = 28000         # верхняя обрезка результата (лимит llm-agent — 30000)

# TTL-кэш динамического каталога: положительный — час, отрицательный — минута
_TOOLS_TTL_OK = 3600.0
_TOOLS_TTL_NEG = 60.0

_HONCHO_DOCS = "https://honcho.dev"
_HONCHO_REPO = "https://github.com/plastic-labs/honcho"
_HONCHO_APP = "https://app.honcho.dev"

# ═══════════════════════════════════════════════════════════════════════
# Каталог инструментов (статический фолбэк, по официальной доке Honcho и
# instructions.md их MCP-сервера).
#   danger="external" — меняет данные/состояние (approval gate);
#   slow=True — живой reasoning, отвечать может 5+ секунд;
#   mode — официальный режим MCP Honcho: "recall" (только чтение),
#          "memory" (Memory store — запись), "admin" (служебное).
# Схемы минимальны: точные сигнатуры отдаёт upstream через динамический
# tools/list; tools/call проксируется без валидации — upstream сам
# валидирует аргументы.
# ═══════════════════════════════════════════════════════════════════════

TOOLS_DOC = [
    # ── Recall: только чтение ─────────────────────────────────────────
    dict(name="list_workspaces", danger="read", mode="recall",
         description="Список workspaces Honcho (корневые пространства: "
                     "workspace → peers → sessions). Если HONCHO_WORKSPACE_ID "
                     "задан, операции идут с ним по умолчанию. Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="list_peers", danger="read", mode="recall",
         description="Список пиров в workspace — люди и агенты (у каждого "
                     "пира своё representation, peer card и выводы). "
                     "Безопасно.",
         schema={"type": "object", "properties": {
             "workspace_id": {"type": "string",
                              "description": "иначе HONCHO_WORKSPACE_ID"}}}),
    dict(name="get_peer_card", danger="read", mode="recall",
         description="Peer card пира — биографические факты (имя, роль, "
                     "стабильные предпочтения). Дешёвое чтение, хороший "
                     "первый шаг recall. Безопасно.",
         schema={"type": "object", "properties": {
             "peer_id": {"type": "string"},
             "workspace_id": {"type": "string"}}}),
    dict(name="list_sessions", danger="read", mode="recall",
         description="Список сессий workspace («корзины» контекста; одна "
                     "сессия на тред/проект — переиспользуйте session_id). "
                     "Безопасно.",
         schema={"type": "object", "properties": {
             "workspace_id": {"type": "string"}}}),
    dict(name="get_session_context", danger="read", mode="recall",
         description="Контекст сессии: сводка диалога с точки зрения пира. "
                     "Дешёвое чтение. Безопасно.",
         schema={"type": "object", "properties": {
             "session_id": {"type": "string"},
             "peer_id": {"type": "string"},
             "token_limit": {"type": "integer"},
             "workspace_id": {"type": "string"}}}),
    dict(name="get_peer_context", danger="read", mode="recall",
         description="Всё, что Honcho знает о пире (в сессии или workspace). "
                     "Дешёвое чтение — вызывай ДО chat. Безопасно.",
         schema={"type": "object", "properties": {
             "peer_id": {"type": "string"},
             "session_id": {"type": "string"},
             "token_limit": {"type": "integer"},
             "workspace_id": {"type": "string"}}}),
    dict(name="get_representation", danger="read", mode="recall",
         description="Текстовая сводка-представление пира (что система о нём "
                     "запомнила). Дешёвое чтение. «No personalization "
                     "insights found» — норма для новых пиров. Безопасно.",
         schema={"type": "object", "properties": {
             "peer_id": {"type": "string"},
             "session_id": {"type": "string"},
             "target_peer_id": {"type": "string",
                                "description": "представление пира с точки "
                                               "зрения другого пира"},
             "workspace_id": {"type": "string"}}}),
    dict(name="chat", danger="read", mode="recall", slow=True,
         description="Разговор с представлением пира: reasoned-ответ на "
                     "вопрос о пользователе («что ты знаешь обо мне», «каков "
                     "мой стиль общения»). Живой reasoning — обычно 5+ "
                     "секунд (прокси ждёт до 120 с). Сначала дешёвые чтения "
                     "(context/representation/search), chat — только когда "
                     "нужен осмысленный вывод. Безопасно, но медленно.",
         schema={"type": "object", "properties": {
             "peer_id": {"type": "string"},
             "session_id": {"type": "string"},
             "statements": {"type": ["array", "string"],
                            "description": "вопрос/утверждения для reasoning"},
             "reasoning_level": {"type": "string",
                                 "enum": ["minimal", "low", "medium", "high",
                                          "max"],
                                 "description": "по умолчанию low"},
             "workspace_id": {"type": "string"}}}),
    dict(name="workspace_chat", danger="read", mode="recall", slow=True,
         description="Как chat, но на уровне workspace (без привязки к "
                     "конкретной сессии). Живой reasoning — до 120 с. "
                     "Безопасно, но медленно.",
         schema={"type": "object", "properties": {
             "peer_id": {"type": "string"},
             "statements": {"type": ["array", "string"]},
             "reasoning_level": {"type": "string",
                                 "enum": ["minimal", "low", "medium", "high",
                                          "max"]},
             "workspace_id": {"type": "string"}}}),
    dict(name="search", danger="read", mode="recall",
         description="Семантический поиск по памяти Honcho (сообщения и "
                     "выводы пира/сессии/workspace). Дешёвое чтение. "
                     "Безопасно.",
         schema={"type": "object", "properties": {
             "query": {"type": "string"},
             "peer_id": {"type": "string"},
             "session_id": {"type": "string"},
             "limit": {"type": "integer"},
             "workspace_id": {"type": "string"}}}),
    dict(name="list_conclusions", danger="read", mode="recall",
         description="Список выводов о пире (conclusions; уровень: "
                     "explicit/deductive/inductive/contradiction, source_ids, "
                     "times_derived). Безопасно.",
         schema={"type": "object", "properties": {
             "peer_id": {"type": "string"},
             "session_id": {"type": "string"},
             "workspace_id": {"type": "string"}}}),
    dict(name="get_conclusions", danger="read", mode="recall",
         description="Выводы пира с деревом источников (source_ids): перед "
                     "исправлением факта пройди по дереву ВНИЗ к исходным "
                     "сообщениям. Безопасно.",
         schema={"type": "object", "properties": {
             "peer_id": {"type": "string"},
             "workspace_id": {"type": "string"}}}),
    dict(name="get_derived_conclusions", danger="read", mode="recall",
         description="Выводы, выведенные ИЗ указанного вывода: перед "
                     "удалением факта пройди по дереву ВВЕРХ (что от него "
                     "зависит). Безопасно.",
         schema={"type": "object", "properties": {
             "peer_id": {"type": "string"},
             "conclusion_id": {"type": "string"},
             "workspace_id": {"type": "string"}}}),

    # ── Memory store: запись ──────────────────────────────────────────
    dict(name="create_workspace", danger="external", mode="memory",
         description="Создать workspace (корневое пространство памяти). "
                     "ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "workspace_id": {"type": "string"}}}),
    dict(name="create_peer", danger="external", mode="memory",
         description="Создать пира (человека или агента). Один стабильный "
                     "peer_id на человека во всех каналах. ВНЕШНЕЕ ДЕЙСТВИЕ — "
                     "ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "peer_id": {"type": "string"},
             "workspace_id": {"type": "string"}}}),
    dict(name="set_peer_card", danger="external", mode="memory",
         description="Перезаписать peer card пира (биографические факты). "
                     "ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "peer_id": {"type": "string"},
             "card": {"type": "string"},
             "workspace_id": {"type": "string"}}}),
    dict(name="create_session", danger="external", mode="memory",
         description="Создать сессию — «корзину» сообщений (одна сессия на "
                     "тред/проект, переиспользуй session_id). ВНЕШНЕЕ "
                     "ДЕЙСТВИЕ — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "session_id": {"type": "string"},
             "workspace_id": {"type": "string"}}}),
    dict(name="add_peers_to_session", danger="external", mode="memory",
         description="Добавить пиров в сессию (observe_me — слушать ли "
                     "сообщения этого пира; observe_others — видеть ли "
                     "чужие; для детерминированных ботов ставь "
                     "observe_me: false). ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ "
                     "ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "session_id": {"type": "string"},
             "peer_ids": {"type": "array", "items": {"type": "string"}},
             "observe_me": {"type": "boolean"},
             "observe_others": {"type": "boolean"},
             "workspace_id": {"type": "string"}}}),
    dict(name="add_messages_to_session", danger="external", mode="memory",
         description="ЯДРО record-цикла: записать сообщения в сессию — ОБЕ "
                     "стороны диалога (сообщения пользователя И ответы "
                     "агента), с указанием автора. ВНЕШНЕЕ ДЕЙСТВИЕ — "
                     "ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "session_id": {"type": "string"},
             "messages": {"type": "array",
                          "description": "строки или объекты "
                                         "{peer_id, content}"},
             "workspace_id": {"type": "string"}}}),

    # ── Служебное / фоновая консолидация ──────────────────────────────
    dict(name="schedule_dream", danger="external", mode="admin",
         description="Запланировать «сон» (dreams) — фоновую консолидацию "
                     "памяти workspace: Honcho сам переосмыслит наблюдения "
                     "и обновит выводы. Не жди мгновенного результата. "
                     "ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "workspace_id": {"type": "string"}}}),

    # ── Обнаружение возможностей ──────────────────────────────────────
    dict(name="honcho_tools_list", danger="read", mode="admin",
         description="Каталог инструментов Honcho с RU-описаниями, пометками "
                     "read/mutating/«долго», режимами recall/memory-store и "
                     "состоянием конфигурации (ключ/endpoint). Работает без "
                     "обращения к upstream. Безопасно.",
         schema={"type": "object", "properties": {}}),
]

EXTERNAL_TOOLS = [t["name"] for t in TOOLS_DOC if t["danger"] == "external"]
SLOW_TOOLS = [t["name"] for t in TOOLS_DOC if t.get("slow")]
RECALL_TOOLS = [t["name"] for t in TOOLS_DOC if t.get("mode") == "recall"]
MEMORY_TOOLS = [t["name"] for t in TOOLS_DOC if t.get("mode") == "memory"]


def _env(name: str, default: str = "") -> str:
    """Переменная окружения; значение вида "${VAR}" из settings.yaml = не задано."""
    v = os.environ.get(name, "")
    if not v or v.startswith("${"):
        return default
    return v


def _base_url() -> str:
    return _env("HONCHO_MCP_URL", "https://mcp.honcho.dev").rstrip("/")


def _api_key() -> str:
    return _env("HONCHO_API_KEY")


def _workspace_id() -> str:
    return _env("HONCHO_WORKSPACE_ID")


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:MAX_TEXT]
    except Exception:
        return str(data)[:MAX_TEXT]


# ═══════════════════════════════════════════════════════════════════════
# MCP Streamable HTTP (JSON-RPC 2.0) с состоянием сеанса
# ═══════════════════════════════════════════════════════════════════════

_session_lock = threading.Lock()
_session = {"id": None, "initialized": False}
_rpc_ids = count(1)  # id запросов к upstream


class HonchoAuthError(RuntimeError):
    """401/403 — upstream отклонил API-ключ."""


def _headers(session_id: str | None = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    token = _api_key()
    if token:
        # Bearer на КАЖДЫЙ запрос, включая initialize
        h["Authorization"] = "Bearer %s" % token
    ws = _workspace_id()
    if ws:
        h["X-Honcho-Workspace-ID"] = ws
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def _http_post(url: str, body: bytes, headers: dict,
               read_timeout: float) -> tuple[int, dict, bytes]:
    """POST с раздельными таймаутами: 5 с подключение / read_timeout операция."""
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme == "https"
    conn_cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    port = parts.port or (443 if secure else 80)
    conn = conn_cls(parts.hostname, port, timeout=CONNECT_TIMEOUT)
    try:
        if conn.sock is None:
            conn.connect()                      # connect_timeout = 5 с
        conn.sock.settimeout(read_timeout)      # дальше — таймаут операции
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, hdrs, raw
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _parse_sse(raw: bytes) -> list:
    """Парсинг SSE-ответа: события data:-строк → JSON-сообщения."""
    messages = []
    text = raw.decode("utf-8", errors="replace")
    for chunk in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        data_lines = [ln[5:].lstrip() for ln in chunk.split("\n")
                      if ln.startswith("data:")]
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        try:
            messages.append(json.loads(payload))
        except Exception:
            continue
    return messages


def _pick_response(messages: list, req_id) -> dict | None:
    """Выбрать JSON-RPC-ответ с нашим id (или последний с result/error)."""
    fallback = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if "result" in msg or "error" in msg:
            fallback = msg
            if msg.get("id") == req_id:
                return msg
    return fallback


def _parse_payload(status: int, hdrs: dict, raw: bytes, req_id):
    """application/json или SSE → JSON-RPC-сообщение (или None)."""
    ctype = hdrs.get("content-type", "")
    if "text/event-stream" in ctype:
        return _pick_response(_parse_sse(raw), req_id)
    if raw.strip():
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _setup_hint() -> dict:
    return {
        "hint": [
            "1. Проверьте HONCHO_MCP_URL (сейчас %s): hosted — "
            "https://mcp.honcho.dev; self-hosted MCP из репозитория "
            "plastic-labs/honcho — bun run http в каталоге mcp/ "
            "(тогда http://127.0.0.1:3000/mcp) или Docker honcho-mcp "
            "рядом с api:8000." % _base_url(),
            "2. Проверьте HONCHO_API_KEY: ключ формата hch-… из %s; "
            "передаётся как Authorization: Bearer на КАЖДЫЙ запрос, "
            "включая initialize. HTTP 401 — ключ неверный/просрочен."
            % _HONCHO_APP,
            "3. Сам Honcho: облако (запускать нечего) или self-hosted — "
            "git clone %s; docker compose up (api + deriver + Postgres + "
            "Redis) или ./honcho start; затем поднимите MCP-сервер из mcp/."
            % _HONCHO_REPO,
            "4. Если задан HONCHO_WORKSPACE_ID — запросы идут с заголовком "
            "X-Honcho-Workspace-ID; иначе передавайте workspace_id в "
            "аргументах инструментов.",
            "5. Затем: honcho_tools_list → recall (search/get_representation) "
            "или memory-store (create_session → add_messages_to_session).",
        ],
    }


def _upstream_error(text: str) -> dict:
    """Ошибка upstream + setup-hint (конфигурация/запуск)."""
    result = {"ok": False, "error": text}
    result.update(_setup_hint())
    return result


def _missing_key_error() -> dict:
    result = {
        "ok": False,
        "error": "HONCHO_API_KEY не задан — вызов Honcho невозможен. "
                 "Каталог инструментов (honcho_tools_list) доступен офлайн.",
    }
    result.update({
        "hint": [
            "1. Получите API-ключ: %s → Sign in → API Keys (формат hch-…)."
            % _HONCHO_APP,
            "2. Задайте HONCHO_API_KEY в .env / окружении (или "
            "config/settings.yaml → mcp_servers.honcho.env).",
            "3. При необходимости HONCHO_MCP_URL (по умолчанию "
            "https://mcp.honcho.dev) и HONCHO_WORKSPACE_ID (заголовок "
            "X-Honcho-Workspace-ID).",
        ],
    })
    return result


def _auth_error(status: int) -> dict:
    return {
        "ok": False,
        "error": "HTTP %s: Honcho отклонил авторизацию — HONCHO_API_KEY не "
                 "принят. Ключ формата hch-… выдаётся в консоли %s и "
                 "передаётся как Authorization: Bearer на КАЖДЫЙ запрос, "
                 "включая initialize. Для self-hosted — ключ вашего "
                 "инстанса." % (status, _HONCHO_APP),
    }


def _augment_known_errors(text: str) -> str:
    """К известным ошибкам Honcho добавить диагностическую подсказку."""
    low = (text or "").lower()
    extra = None
    if "no personalization insights" in low:
        extra = ("Это норма для новых пиров: Honcho ещё не вывел "
                 "персонализированные выводы. Запишите наблюдения через "
                 "add_messages_to_session, дайте фоновому reasoning время "
                 "(он асинхронный — не ждите и не поллите) и повторите "
                 "list_conclusions/get_representation позже.")
    elif "unauthorized" in low or "api key" in low or "invalid key" in low:
        extra = ("Ключ не принят: HONCHO_API_KEY должен быть hch-… из %s; "
                 "Bearer передаётся на каждый запрос, включая initialize."
                 % _HONCHO_APP)
    elif "reasoning_level" in low:
        extra = ("reasoning_level — minimal/low/medium/high/max "
                 "(по умолчанию low).")
    if extra:
        return "%s\nПодсказка: %s" % (text, extra)
    return text


def _extract_text(data: dict) -> str:
    """Текст из result.content[] (как у sibling-прокси onec_qa)."""
    result = data.get("result") if isinstance(data, dict) else None
    text = None
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list) and content:
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, dict) and "text" in item:
                    parts.append(str(item["text"]))
            text = "\n".join(parts) if parts else json.dumps(
                content, ensure_ascii=False)
        elif "text" in result:
            text = str(result["text"])
        elif result.get("isError"):
            text = json.dumps(result, ensure_ascii=False)
    if text is None:
        text = json.dumps(data, ensure_ascii=False)
    return _augment_known_errors(text[:MAX_TEXT])


def _is_session_lost(status: int, data) -> bool:
    """404 или JSON-RPC-ошибка вида «session not found» → re-initialize."""
    if status == 404:
        return True
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        err = data["error"]
        code = err.get("code")
        msg = str(err.get("message", "")).lower()
        if code in (-32001, -32000) and "session" in msg:
            return True
        if "session" in msg and ("not found" in msg or "истёк" in msg
                                 or "unknown" in msg):
            return True
    return False


def _conn_error(where: str, e: Exception) -> dict:
    """Ошибка соединения (у Honcho нет /healthz — ошибки по HTTP-кодам/JSON-RPC)."""
    return _upstream_error("%s (%s) недоступен: %s"
                           % (where, _base_url(), e))


def _initialize_session() -> str | None:
    """initialize → Mcp-Session-Id; затем notifications/initialized (202).

    Если upstream не выдаёт Mcp-Session-Id (stateless-режим) — вернём None
    и будем работать без заголовка сеанса (адаптация под Honcho).
    """
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "llm-agent-honcho-proxy", "version": "1.0.0"},
        },
    }, ensure_ascii=False).encode("utf-8")
    status, hdrs, raw = _http_post(_base_url(), body, _headers(None),
                                   INIT_TIMEOUT)
    if status in (401, 403):
        raise HonchoAuthError("initialize: HTTP %s" % status)
    if status != 200:
        raise RuntimeError("initialize: HTTP %s %s"
                           % (status, raw[:200].decode("utf-8", errors="replace")))
    session_id = hdrs.get("mcp-session-id")
    if not session_id:
        logger.info("honcho: upstream не выдал Mcp-Session-Id — "
                    "stateless-режим, работаем без сеанса")
    # notifications/initialized — ответ 202 игнорируем
    try:
        nb = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"},
                        ensure_ascii=False).encode("utf-8")
        _http_post(_base_url(), nb, _headers(session_id), INIT_TIMEOUT)
    except Exception:
        pass
    return session_id


def _get_session(force: bool = False) -> str | None:
    """Ленивый initialize с блокировкой; кэш сеанса в переменной модуля."""
    with _session_lock:
        if _session["initialized"] and not force:
            return _session["id"]
        _session["id"] = _initialize_session()
        _session["initialized"] = True
        logger.info("honcho: MCP-сеанс инициализирован (Mcp-Session-Id: %s)",
                    _session["id"] or "stateless")
        return _session["id"]


def _drop_session() -> None:
    with _session_lock:
        _session["id"] = None
        _session["initialized"] = False


def _call_timeout(tool: str) -> float:
    if tool in ("chat", "workspace_chat"):
        return CHAT_TIMEOUT
    return DEFAULT_TIMEOUT


def _rpc_call_sync(tool: str, arguments: dict) -> dict:
    """tools/call через Streamable HTTP; 404/потеря сеанса → re-init + 1 ретрай.
    Возвращает {"ok":…, "text"/"error":…}. Чистый passthrough: валидация
    аргументов на стороне upstream."""
    if not _api_key():
        return _missing_key_error()
    payload = {
        "jsonrpc": "2.0",
        "id": next(_rpc_ids),
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timeout = _call_timeout(tool)

    last_status, last_raw = 0, b""
    for attempt in (1, 2):
        try:
            session_id = _get_session(force=(attempt == 2))
        except HonchoAuthError:
            return _auth_error(401)
        except Exception as e:
            return _conn_error("Honcho MCP недоступен при инициализации сеанса", e)
        try:
            status, hdrs, raw = _http_post(_base_url(), body,
                                           _headers(session_id), timeout)
        except Exception as e:
            return _conn_error("Honcho MCP-эндпоинт", e)

        data = _parse_payload(status, hdrs, raw, payload["id"])

        if _is_session_lost(status, data):
            last_status, last_raw = status, raw
            _drop_session()
            if attempt == 1:
                logger.info("honcho: сеанс потерян (HTTP %s) — re-initialize "
                            "и ретрай %s", status, tool)
                continue
        if status in (401, 403):
            return _auth_error(status)
        if status != 200:
            detail = raw[:300].decode("utf-8", errors="replace")
            return _upstream_error("HTTP %s от Honcho MCP (%s): %s"
                                   % (status, _base_url(), detail))

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = "JSON-RPC %s: %s" % (err.get("code"), err.get("message"))
            if _is_session_lost(status, data) and attempt == 1:
                _drop_session()
                continue
            return {"ok": False, "error": _augment_known_errors(msg)}

        if data is not None:
            return {"ok": True, "text": _extract_text(data)}

        # 202/пусто или нераспознанный ответ — покажем что есть
        return {"ok": True,
                "text": raw[:MAX_TEXT].decode("utf-8", errors="replace")
                        or "пустой ответ Honcho MCP (HTTP %s)" % status}

    return _upstream_error("сеанс Honcho MCP теряется при каждом вызове "
                           "(последний ответ HTTP %s: %s)"
                           % (last_status,
                              last_raw[:200].decode("utf-8", errors="replace")))


# ═══════════════════════════════════════════════════════════════════════
# tools/list: динамический passthrough из upstream + статический фолбэк
# ═══════════════════════════════════════════════════════════════════════

_tools_lock = threading.Lock()
_tools_cache = {"tools": None, "at": 0.0}


def _fetch_upstream_tools_sync() -> list | None:
    """Динамический tools/list из upstream. None — upstream недоступен."""
    try:
        session_id = _get_session()
        payload = {"jsonrpc": "2.0", "id": next(_rpc_ids),
                   "method": "tools/list", "params": {}}
        status, hdrs, raw = _http_post(
            _base_url(), json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            _headers(session_id), DEFAULT_TIMEOUT)
        data = _parse_payload(status, hdrs, raw, payload["id"])
        if status in (401, 403):
            raise HonchoAuthError("tools/list: HTTP %s" % status)
        if status != 200:
            raise RuntimeError("tools/list: HTTP %s" % status)
        if not isinstance(data, dict) or "result" not in data:
            raise RuntimeError("tools/list: нет result в ответе")
        out = []
        for t in (data["result"] or {}).get("tools") or []:
            if isinstance(t, dict) and t.get("name"):
                out.append({
                    "name": str(t["name"]),
                    "description": str(t.get("description", "")),
                    "inputSchema": t.get("inputSchema") or {"type": "object"},
                })
        if not out:
            raise RuntimeError("tools/list: пустой список инструментов")
        logger.info("honcho: динамический tools/list получен (%d инструментов "
                    "от upstream)", len(out))
        return out
    except Exception as e:
        logger.info("honcho: динамический tools/list недоступен (%s) — "
                    "статический фолбэк-каталог", e)
        return None


def _assemble_tools(upstream_tools: list | None) -> list:
    tools = []
    if upstream_tools:
        for t in upstream_tools:
            if t["name"] == "honcho_tools_list":
                continue
            tools.append(Tool(name=t["name"], description=t["description"],
                              inputSchema=t["inputSchema"]))
    else:
        for spec in TOOLS_DOC:
            if spec["name"] == "honcho_tools_list":
                continue
            note = " [ДОЛГО: живой reasoning — до 120 с]" if spec.get("slow") else ""
            tools.append(Tool(name=spec["name"],
                              description=spec["description"] + note,
                              inputSchema=spec["schema"]))
    tools.append(Tool(
        name="honcho_tools_list",
        description=TOOLS_DOC[-1]["description"],
        inputSchema=TOOLS_DOC[-1]["schema"]))
    return tools


def _build_tools() -> list:
    now = time.monotonic()
    upstream = None
    need_fetch = False
    with _tools_lock:
        if _tools_cache["tools"] is not None:
            upstream = _tools_cache["tools"]               # кэш — час
        elif now - _tools_cache["at"] < _TOOLS_TTL_NEG:
            upstream = None                                # недавняя неудача
        else:
            need_fetch = True
    if need_fetch:
        fetched = _fetch_upstream_tools_sync()
        with _tools_lock:
            _tools_cache["tools"] = fetched
            _tools_cache["at"] = time.monotonic()
        upstream = fetched
    return _assemble_tools(upstream)


# ═══════════════════════════════════════════════════════════════════════
# MCP-сервер (stdio)
# ═══════════════════════════════════════════════════════════════════════

app = Server("honcho")


@app.list_tools()
async def list_tools() -> list:
    return await asyncio.to_thread(_build_tools)


def _tools_catalog() -> dict:
    return {
        "source": "Honcho — межсессионная память агентов (Plastic Labs; "
                  "open-source, AGPL-3.0; SOTA на LongMemEval)",
        "docs": _HONCHO_DOCS,
        "repo": _HONCHO_REPO,
        "endpoint": _base_url(),
        "config": {
            "api_key_set": bool(_api_key()),
            "workspace_id": _workspace_id() or None,
            "headers": ["Authorization: Bearer hch-… (каждый запрос, "
                        "включая initialize)"]
            + (["X-Honcho-Workspace-ID (HONCHO_WORKSPACE_ID задан)"]
               if _workspace_id() else
               ["X-Honcho-Workspace-ID (не задан — workspace_id в аргументах)"]),
        },
        "modes": {
            "recall": {
                "ru": "Recall — только чтение (режим по умолчанию)",
                "tools": RECALL_TOOLS,
            },
            "memory_store": {
                "ru": "Memory store — запись (если пользователь попросил "
                      "запоминать): create_session → create_peer(ы) → "
                      "add_peers_to_session → add_messages_to_session "
                      "(обе стороны диалога)",
                "tools": MEMORY_TOOLS,
            },
        },
        "tools": {t["name"]: {
            "description": t["description"],
            "danger": t["danger"],
            "mode": t.get("mode"),
            "slow": bool(t.get("slow")),
        } for t in TOOLS_DOC if t["name"] != "honcho_tools_list"},
        "mutating": EXTERNAL_TOOLS,
        "slow": SLOW_TOOLS,
        "timeouts": {"connect": CONNECT_TIMEOUT, "default": DEFAULT_TIMEOUT,
                     "chat": CHAT_TIMEOUT},
        "concepts": {
            "workspace": "корневое пространство (workspace → peers → sessions)",
            "peers": "люди и агенты; один стабильный peer_id на человека",
            "sessions": "корзины сообщений (одна сессия на тред/проект)",
            "conclusions": "выводы: explicit/deductive/inductive/contradiction, "
                           "source_ids, times_derived",
            "representation": "текстовая сводка пира",
            "peer_card": "биографические факты пира",
            "dreams": "фоновая консолидация памяти",
            "reasoning": "асинхронный — не ждать и не поллить",
        },
        "known_notes": {
            "no personalization insights found":
                "норма для новых пиров — выводы появятся после наблюдений "
                "(add_messages_to_session) и фонового reasoning",
            "цикл": "recall → respond → record",
        },
        "catalog_mode": "динамический (upstream)" if _tools_cache["tools"]
                        else "статический фолбэк (upstream недоступен: точные "
                             "сигнатуры аргументов отдаёт upstream через "
                             "tools/list; tools/call валидирует upstream)",
        "setup_hint": _setup_hint()["hint"],
    }


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    try:
        if name == "honcho_tools_list":
            return [TextContent(type="text", text=_json_result(_tools_catalog()))]

        # Чистый passthrough: имя + аргументы без валидации (upstream сам
        # валидирует; динамический каталог upstream может быть шире фолбэка).
        res = await asyncio.to_thread(_rpc_call_sync, name, dict(arguments or {}))
        return [TextContent(type="text", text=_json_result(res))]
    except Exception as e:
        logger.exception("honcho tool %s failed", name)
        return [TextContent(type="text", text="Ошибка: %s" % e)]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
