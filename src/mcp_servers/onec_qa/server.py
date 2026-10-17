"""MCP-сервер: MCP QA — ИИ-тестирование управляемых форм 1С.

Прокси инструментов MCP QA (Docker-образ comol/qa_mcp, автор comol — тот же,
что у Конструктора MCP серверов) к Streamable HTTP MCP-эндпоинту контейнера.
ИИ управляет тестовой базой 1С через логическую модель форм: читает окна и
поля, находит элементы, вводит значения, нажимает кнопки, проверяет результат.

Транспорт: MCP Streamable HTTP С СОСТОЯНИЕМ СЕАНСА.
  1. POST initialize → ответ содержит заголовок Mcp-Session-Id (сеанс выдаёт
     контейнер; хранится в переменной модуля, инициализация ленивая, под
     блокировкой).
  2. POST notifications/initialized (ответ 202 игнорируется).
  3. POST tools/call c заголовком Mcp-Session-Id. Ответ может прийти как
     application/json, так и SSE-потоком (text/event-stream) — парсим
     data:-строки.
  4. 404/потеря сеанса → re-initialize и ОДИН ретрай вызова.
Адрес ONEC_QA_URL (по умолчанию http://127.0.0.1:8020/mcp), опциональный
Bearer ONEC_QA_HTTP_TOKEN (это НЕ лицензия; лицензия — LICENSE_KEY_QA в
config.env контейнера).

Таймауты: 5 с на подключение, до 900 с на операцию (ui_wait; прочие — 120 с,
как MCP_QA_COMMAND_TIMEOUT по умолчанию в контейнере).

Если контейнер не запущен/недоступен — возвращается читаемая ошибка с
подсказкой по установке (docker run, проверка /healthz, запуск тест-клиента
1cv8c … /TestClient -TPort1538). Инструменты, недоступные в Docker-контейнере
(executor_capability: ui_screenshot, ui_eval, qa_run_script, qa_setup,
qa_install_client), в прокси НЕ включены — см. docs/ONEC_QA_INTEGRATION.md.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import sys
import threading
import urllib.parse
from itertools import count

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("onec-qa-mcp")

CONNECT_TIMEOUT = 5.0    # подключение к контейнеру должно падать быстро
DEFAULT_TIMEOUT = 120.0  # MCP_QA_COMMAND_TIMEOUT контейнера по умолчанию
UI_WAIT_TIMEOUT = 900.0  # ui_wait умеет ждать долго
MAX_TEXT = 28000         # верхняя обрезка результата (лимит llm-agent — 30000)

_DOCKER_RUN = ("docker run -d --name qa-mcp -p 8020:8020 --env-file config.env "
               "comol/qa_mcp:latest  (в config.env: LICENSE_KEY_QA=…, "
               "MCP_QA_TESTCLIENT=host.docker.internal:1538)")

# ═══════════════════════════════════════════════════════════════════════
# Каталог инструментов (статичный, из документации MCP QA docs.onerpa.ru)
# hook=True  — требует расширение MCPQAClient.cfe в тестовой базе;
# danger="external" — инструмент меняет данные/состояние (approval gate).
# ═══════════════════════════════════════════════════════════════════════

TOOLS_DOC = [
    # ── Жизненный цикл сеанса (qa_*) ──────────────────────────────────
    dict(name="qa_status", danger="read",
         description="Состояние сеанса MCP QA: executor, link (связь с "
                     "тест-клиентом), platform_version, client_hook.available, "
                     "busy (не встаёт в очередь — busy: true, если другой "
                     "инструмент ещё работает). Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="qa_start", danger="external",
         description="Подключиться к УЖЕ запущенному тест-клиенту 1С: "
                     "connection (профиль из qa_profiles), testclient "
                     "(адрес host:port), port. user/password/client_kind "
                     "игнорируются. Контейнер держит ОДИН сеанс: новый "
                     "qa_start сбрасывает предыдущий. ВНЕШНЕЕ ДЕЙСТВИЕ — "
                     "ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "connection": {"type": "string",
                            "description": "профиль подключения (qa_profiles)"},
             "testclient": {"type": "string",
                            "description": "адрес тест-клиента host:port "
                                           "(по умолчанию host.docker.internal:1538)"},
             "port": {"type": "integer",
                      "description": "порт TestClient (-TPort)"}}}),
    dict(name="qa_stop", danger="external",
         description="Отключиться от тест-клиента, не закрывая его. "
                     "ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {}}),
    dict(name="qa_reconnect", danger="external",
         description="Переподключиться к тест-клиенту (force — принудительно). "
                     "Обязательный шаг после обрыва связи: сначала "
                     "qa_command_status, затем qa_reconnect(force=True), затем "
                     "прочитать окно — никогда не повторять действие вслепую. "
                     "ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "force": {"type": "boolean"}}}),
    dict(name="qa_command_status", danger="read",
         description="Судьба команды, потерянной при обрыве связи "
                     "(channel=\"client\", wait_seconds). Исход потерянного "
                     "действия неизвестен — уточни его здесь, прежде чем "
                     "что-либо повторять.",
         schema={"type": "object", "properties": {
             "channel": {"type": "string"},
             "wait_seconds": {"type": "number"}}}),
    dict(name="qa_doctor", danger="read",
         description="Диагностика адреса тест-клиента: проверяет, отвечает ли "
                     "порт TestClient (помогает при «Отсутствует подходящий "
                     "клиент тестирования»). Безопасно.",
         schema={"type": "object", "properties": {
             "testclient": {"type": "string"}}}),
    dict(name="qa_profiles", danger="read",
         description="Профили подключения контейнера (ключ testclient). "
                     "Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="qa_data_candidates", danger="read", hook=True,
         description="Значения из базы для подстановки в поля (чтобы не "
                     "угадывать точные наименования). Требует расширение "
                     "MCPQAClient в тестовой базе. Безопасно.",
         schema={"type": "object", "properties": {}}),

    # ── Окна и формы (ui_*) ───────────────────────────────────────────
    dict(name="ui_active_window", danger="read",
         description="Какое окно сейчас активно в тест-клиенте 1С (заголовок, "
                     "вид, форма). Первый шаг чтения экрана. Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_window_tree", danger="read",
         description="Дерево окон и элементов формы (detail: \"lite\"|\"full\"). "
                     "Начинай с lite. Лимиты объёма: max_nodes ≤ 5000, "
                     "max_depth ≤ 20. Безопасно.",
         schema={"type": "object", "properties": {
             "detail": {"type": "string", "enum": ["lite", "full"]},
             "max_nodes": {"type": "integer"},
             "max_depth": {"type": "integer"}}}),
    dict(name="ui_window_changes", danger="read",
         description="Что изменилось в окнах с предыдущей проверки (быстрый "
                     "способ увидеть реакцию формы на действие). Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_inspect", danger="read",
         description="Инспекция элемента формы: свойства, состояние, "
                     "доступность. Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_open", danger="external",
         description="Открыть форму по виду и имени (kind, metadata_name, "
                     "form_name); понимает русские имена видов: Справочник, "
                     "Документы… Неосновные формы по имени — только с "
                     "расширением MCPQAClient. ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ "
                     "ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "kind": {"type": "string",
                      "description": "вид объекта: Справочник, Документы…"},
             "metadata_name": {"type": "string"},
             "form_name": {"type": "string"}}}),
    dict(name="ui_close_form", danger="external",
         description="Закрыть форму (on_prompt — реакция на промпт при "
                     "закрытии, например сохранить/не сохранять). "
                     "ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "on_prompt": {"type": "string"}}}),
    dict(name="ui_form", danger="read",
         description="Выполнить команду формы: кнопка командной панели "
                     "(command_bar) или пункт меню (menu_choice). Внимание: "
                     "команда может изменить данные (Записать/Провести) — "
                     "вызывай осознанно и после проверки результата.",
         schema={"type": "object", "properties": {
             "command_bar": {"type": "string"},
             "menu_choice": {"type": "string"}}}),
    dict(name="ui_form_schema", danger="read", hook=True,
         description="JSON-схема текущей формы (полная модель элементов). "
                     "Требует расширение MCPQAClient в тестовой базе. "
                     "Безопасно.",
         schema={"type": "object", "properties": {}}),

    # ── Элементы и ввод ───────────────────────────────────────────────
    dict(name="ui_find", danger="read",
         description="Найти элементы формы по имени/заголовку/условию. "
                     "Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_select", danger="external",
         description="Выбрать элемент (строку списка, значение). "
                     "ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_click", danger="external",
         description="Нажать кнопку/гиперссылку формы. Результат проверяй "
                     "через ui_window_changes/ui_messages. ВНЕШНЕЕ ДЕЙСТВИЕ — "
                     "ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_input", danger="external",
         description="Ввести текст в поле. Действия, меняющие значение, "
                     "возвращают value_before/value_after/verified; "
                     "applied: false = текст ещё в редакторе поля (нужно "
                     "подтверждение). ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "value": {"type": "string"}}}),
    dict(name="ui_set", danger="external",
         description="Установить значение поля. Возвращает "
                     "value_before/value_after/verified. ВНЕШНЕЕ ДЕЙСТВИЕ — "
                     "ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_get_text", danger="read",
         description="Прочитать текст элемента/поля. Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_field", danger="external",
         description="Работа с полем формы (dropdown_select — выбор значения "
                     "в выпадающем списке). ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ "
                     "ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "action": {"type": "string",
                        "description": "например dropdown_select"}}}),
    dict(name="ui_dialog", danger="external",
         description="Ответить на диалог 1С (action: click + кнопка, "
                     "optional). Осторожно: подтверждает диалог — читай "
                     "текст диалога перед нажатием. ВНЕШНЕЕ ДЕЙСТВИЕ — "
                     "ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "action": {"type": "string", "description": "по умолчанию click"}}}),
    dict(name="ui_wait", danger="read",
         description="Ждать появления элемента/состояния/окна (до 900 с; "
                     "прочие команды — 120 с). Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_messages", danger="read",
         description="Сообщения пользователю на форме (панель сообщений). "
                     "Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_errors", danger="read",
         description="Ошибки, показанные формой. Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_assert", danger="read",
         description="Проверка утверждения (kind=\"element\", checked) — "
                     "формализует ожидания сценария тестирования. Безопасно.",
         schema={"type": "object", "properties": {
             "kind": {"type": "string"},
             "checked": {}}}),
    dict(name="ui_list", danger="read",
         description="Прочитать список/таблицу целиком. Лимиты объёма: "
                     "max_rows/max_table_rows ≤ 1000, max_columns ≤ 200, "
                     "max_rows × max_columns ≤ 20000, переход к строке "
                     "≤ 10000. Безопасно.",
         schema={"type": "object", "properties": {}}),
    dict(name="ui_table", danger="external",
         description="Работа с табличной частью/таблицей формы: select / edit "
                     "/ delete / copy / input_cell / end_edit. Лимиты: "
                     "max_rows ≤ 1000, max_columns ≤ 200, строки×колонки "
                     "≤ 20000. ВНЕШНЕЕ ДЕЙСТВИЕ — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
         schema={"type": "object", "properties": {
             "action": {"type": "string",
                        "enum": ["select", "edit", "delete", "copy",
                                 "input_cell", "end_edit"]}}}),

    # ── Обнаружение возможностей ──────────────────────────────────────
    dict(name="qa_tools_list", danger="read",
         description="Каталог инструментов MCP QA с описаниями, пометками "
                     "«нужно расширение MCPQAClient» и списком возможностей, "
                     "недоступных в Docker-контейнере (executor_capability). "
                     "Работает без обращения к контейнеру. Безопасно.",
         schema={"type": "object", "properties": {}}),
]

EXTERNAL_TOOLS = [t["name"] for t in TOOLS_DOC if t["danger"] == "external"]
HOOK_TOOLS = [t["name"] for t in TOOLS_DOC if t.get("hook")]
# Недоступны в Docker-контейнере (отвечают executor_capability) — прокси их
# не проксирует, см. docs/ONEC_QA_INTEGRATION.md:
EXECUTOR_CAPABILITY_MISSING = [
    "ui_screenshot", "qa_start(hidden_desktop)", "ui_eval", "qa_run_script",
    "qa_setup", "qa_install_client",
]


def _env(name: str, default: str = "") -> str:
    """Переменная окружения; значение вида "${VAR}" из settings.yaml = не задано."""
    v = os.environ.get(name, "")
    if not v or v.startswith("${"):
        return default
    return v


def _base_url() -> str:
    return _env("ONEC_QA_URL", "http://127.0.0.1:8020/mcp").rstrip("/")


def _token() -> str:
    return _env("ONEC_QA_HTTP_TOKEN")


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:MAX_TEXT]
    except Exception:
        return str(data)[:MAX_TEXT]


# ═══════════════════════════════════════════════════════════════════════
# MCP Streamable HTTP (JSON-RPC 2.0) с состоянием сеанса
# ═══════════════════════════════════════════════════════════════════════

_session_lock = threading.Lock()
_session = {"id": None}
_rpc_ids = count(1)  # id запросов к upstream


def _headers(session_id: str | None = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    token = _token()
    if token:
        h["Authorization"] = "Bearer %s" % token
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


def _upstream_error(text: str) -> dict:
    """Ошибка upstream + setup-hint (установка/запуск/диагностика)."""
    result = {"ok": False, "error": text}
    result.update(_setup_hint())
    return result


def _setup_hint() -> dict:
    return {
        "hint": [
            "1. Запустите контейнер MCP QA: %s" % _DOCKER_RUN,
            "2. Проверьте живость: curl http://127.0.0.1:8020/healthz "
            "(только HTTP-живость, НЕ доступность 1С); порт можно сменить "
            "MCP_QA_HTTP_PORT.",
            "3. Запустите тест-клиент 1С (платформа и лицензия В КОНТЕЙНЕРЕ "
            "НЕТ): тонкий клиент 1cv8c ENTERPRISE /F\"<имя базы>\" /TestClient "
            "-TPort1538 /DisableStartupDialogs или веб-клиент …?TestClient "
            "(тогда задайте MCP_QA_TESTCLIENT_ID).",
            "4. Проверьте окружение: ONEC_QA_URL (по умолчанию "
            "http://127.0.0.1:8020/mcp), ONEC_QA_HTTP_TOKEN (если в контейнере "
            "задан MCP_QA_HTTP_TOKEN), MCP_QA_TESTCLIENT "
            "(по умолчанию host.docker.internal:1538).",
            "5. Затем: qa_status → qa_start(connection=…) → ui_active_window.",
        ],
    }


def _augment_known_errors(text: str) -> str:
    """К известным ошибкам MCP QA добавить диагностическую подсказку."""
    low = (text or "").lower()
    extra = None
    if "отсутствует подходящий клиент тестирования" in low:
        extra = ("Тест-клиент не вошёл в базу или не совпал TestClientID: "
                 "запустите 1cv8c ENTERPRISE /F\"<база>\" /TestClient "
                 "-TPort1538 /DisableStartupDialogs, проверьте "
                 "MCP_QA_TESTCLIENT / MCP_QA_TESTCLIENT_ID и вызовите qa_doctor.")
    elif "executor_capability" in low or "не поддерживает" in low:
        extra = ("Эта возможность недоступна в Docker-контейнере qa_mcp "
                 "(%s) — см. docs/ONEC_QA_INTEGRATION.md."
                 % ", ".join(EXECUTOR_CAPABILITY_MISSING))
    elif "client_hook" in low or "mcpqaclient" in low:
        extra = ("Нужно расширение MCPQAClient.cfe в тестовой базе "
                 "(требуется для ui_form_schema, qa_data_candidates и ui_open "
                 "неосновных форм по имени).")
    if extra:
        return "%s\nПодсказка: %s" % (text, extra)
    return text


def _extract_text(data: dict) -> str:
    """Текст из result.content[] (как у sibling-прокси onec_designer_tools)."""
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
    """Ошибка соединения: уточняем по /healthz, жив ли контейнер."""
    if _healthz_ok():
        return _upstream_error(
            "%s: контейнер отвечает на /healthz, но MCP-эндпоинт не отвечает — "
            "проверьте ONEC_QA_URL (%s): %s" % (where, _base_url(), e))
    return _upstream_error("%s недоступен: %s (%s). "
                           "Если контейнер не запущен — см. hint ниже."
                           % (where, _base_url(), e))


def _initialize_session() -> str:
    """initialize → Mcp-Session-Id; затем notifications/initialized (202)."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "llm-agent-onec-qa-proxy", "version": "1.0.0"},
        },
    }, ensure_ascii=False).encode("utf-8")
    status, hdrs, raw = _http_post(_base_url(), body, _headers(None),
                                   CONNECT_TIMEOUT)
    if status != 200:
        raise RuntimeError("initialize: HTTP %s %s"
                           % (status, raw[:200].decode("utf-8", errors="replace")))
    session_id = hdrs.get("mcp-session-id")
    if not session_id:
        raise RuntimeError("initialize: в ответе нет заголовка Mcp-Session-Id")
    # notifications/initialized — ответ 202 игнорируем
    try:
        nb = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"},
                        ensure_ascii=False).encode("utf-8")
        _http_post(_base_url(), nb, _headers(session_id), CONNECT_TIMEOUT)
    except Exception:
        pass
    return session_id


def _get_session(force: bool = False) -> str:
    """Ленивый initialize с блокировкой; кэш session id в переменной модуля."""
    with _session_lock:
        if _session["id"] and not force:
            return _session["id"]
        _session["id"] = _initialize_session()
        logger.info("onec_qa: MCP-сеанс инициализирован (Mcp-Session-Id получен)")
        return _session["id"]


def _drop_session() -> None:
    with _session_lock:
        _session["id"] = None


def _call_timeout(tool: str, arguments: dict) -> float:
    if tool == "ui_wait":
        return UI_WAIT_TIMEOUT
    if tool == "qa_command_status":
        try:
            wait = float(arguments.get("wait_seconds") or 0)
        except (TypeError, ValueError):
            wait = 0.0
        return max(DEFAULT_TIMEOUT, min(wait + 60.0, UI_WAIT_TIMEOUT))
    return DEFAULT_TIMEOUT


def _rpc_call_sync(tool: str, arguments: dict) -> dict:
    """tools/call через Streamable HTTP; 404/потеря сеанса → re-init + 1 ретрай.
    Возвращает {"ok":…, "text"/"error":…}."""
    payload = {
        "jsonrpc": "2.0",
        "id": next(_rpc_ids),
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timeout = _call_timeout(tool, arguments)

    last_status, last_raw = 0, b""
    for attempt in (1, 2):
        try:
            session_id = _get_session(force=(attempt == 2))
        except Exception as e:
            return _conn_error("MCP QA недоступен при инициализации сеанса", e)
        try:
            status, hdrs, raw = _http_post(_base_url(), body,
                                           _headers(session_id), timeout)
        except Exception as e:
            return _conn_error("контейнер MCP QA", e)

        ctype = hdrs.get("content-type", "")
        data = None
        if "text/event-stream" in ctype:
            data = _pick_response(_parse_sse(raw), payload["id"])
        elif raw.strip():
            try:
                parsed = json.loads(raw.decode("utf-8", errors="replace"))
                data = parsed if isinstance(parsed, dict) else None
            except Exception:
                data = None

        if _is_session_lost(status, data):
            last_status, last_raw = status, raw
            _drop_session()
            if attempt == 1:
                logger.info("onec_qa: сеанс потерян (HTTP %s) — re-initialize "
                            "и ретрай %s", status, tool)
                continue
        if status == 401 or status == 403:
            return {"ok": False,
                    "error": "HTTP %s: контейнер MCP QA отклонил авторизацию. "
                             "Задайте ONEC_QA_HTTP_TOKEN тем же значением, что "
                             "MCP_QA_HTTP_TOKEN в config.env контейнера "
                             "(это НЕ лицензия)." % status}
        if status != 200:
            detail = raw[:300].decode("utf-8", errors="replace")
            return _upstream_error("HTTP %s от контейнера MCP QA: %s %s"
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
                        or "пустой ответ контейнера (HTTP %s)" % status}

    return _upstream_error("сеанс MCP QA теряется при каждом вызове "
                           "(последний ответ HTTP %s: %s)"
                           % (last_status,
                              last_raw[:200].decode("utf-8", errors="replace")))


def _healthz_ok() -> bool:
    """Проверка /healthz (только HTTP-живость, не доступность 1С)."""
    parts = urllib.parse.urlsplit(_base_url())
    try:
        from urllib.request import urlopen, Request
        url = "%s://%s/healthz" % (parts.scheme,
                                   parts.netloc or "%s:%s" % (parts.hostname, 8020))
        req = Request(url, method="GET")
        req.add_header("Accept", "application/json, text/plain")
        with urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# MCP-сервер (stdio); tools/list — статический, без обращения к upstream
# ═══════════════════════════════════════════════════════════════════════

app = Server("onec_qa")


def _build_tools() -> list:
    tools = []
    for spec in TOOLS_DOC:
        if spec["name"] == "qa_tools_list":
            continue
        note = ""
        if spec["name"] in HOOK_TOOLS:
            note = " [нужно расширение MCPQAClient]"
        tools.append(Tool(name=spec["name"],
                          description=spec["description"] + note,
                          inputSchema=spec["schema"]))
    tools.append(Tool(
        name="qa_tools_list",
        description=TOOLS_DOC[-1]["description"],
        inputSchema=TOOLS_DOC[-1]["schema"]))
    return tools


@app.list_tools()
async def list_tools() -> list:
    return _build_tools()


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    try:
        if name == "qa_tools_list":
            return [TextContent(type="text", text=_json_result({
                "source": "MCP QA (comol/qa_mcp) — тестирование управляемых "
                          "форм 1С через тест-клиент",
                "docs": "https://docs.onerpa.ru/mcp-servery-1c/servery/qa",
                "endpoint": _base_url(),
                "mcp_connection_name": "1c-qa",
                "tools": {t["name"]: {
                    "description": t["description"],
                    "danger": t["danger"],
                    "requires_client_hook": bool(t.get("hook")),
                } for t in TOOLS_DOC if t["name"] != "qa_tools_list"},
                "requires_client_hook": HOOK_TOOLS,
                "executor_capability_missing": EXECUTOR_CAPABILITY_MISSING,
                "limits": {
                    "max_nodes": 5000, "max_depth": 20,
                    "max_rows": 1000, "max_columns": 200,
                    "rows_x_columns": 20000, "goto_row": 10000,
                },
                "setup_hint": _setup_hint()["hint"],
            }))]

        if not any(t["name"] == name for t in TOOLS_DOC):
            return [TextContent(type="text",
                                text="Неизвестный инструмент: %s" % name)]

        res = await asyncio.to_thread(_rpc_call_sync, name, dict(arguments or {}))
        return [TextContent(type="text", text=_json_result(res))]
    except Exception as e:
        logger.exception("onec_qa tool %s failed", name)
        return [TextContent(type="text", text="Ошибка: %s" % e)]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
