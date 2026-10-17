"""MCP-сервер: Инструменты для разработки 1С (Конструктор MCP серверов).

Прокси 4 инструментов из репозитория comol/mcp_designer_tools к HTTP-эндпоинту
1С «Конструктора MCP серверов»:
  vcexecutecode(bslcode)      — выполнить код 1С
  vcexecutequery(querytext)   — выполнить запрос 1С (результат таблицей-текстом)
  vcvalidatequery(querytext)  — проверить синтаксис запроса без выполнения
  vcloggetlasterror()         — последняя ошибка Журнала регистрации за 24 ч

Транспорт: JSON-RPC 2.0 поверх HTTP POST (method "tools/call", params
{name, arguments}) на адрес ONEC_MCP_URL (по умолчанию
http://127.0.0.1:8795/mcp), опционально Bearer ONEC_MCP_TOKEN.
Если эндпоинт не настроен/недоступен — возвращается читаемая ошибка с
подсказкой по установке (см. README.md рядом с этим файлом).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("onec-designer-tools-mcp")

HTTP_TIMEOUT = 30.0  # выполнение кода/запросов в 1С может быть небыстрым

# ═══════════════════════════════════════════════════════════════════════
# Описание инструментов (статичное — из README репозитория comol/mcp_designer_tools)
# ═══════════════════════════════════════════════════════════════════════

VC_TOOLS_DOC = {
    "vcexecutecode": {
        "params": "bslcode — код на языке 1С",
        "description": "Выполняет произвольный код на языке 1С в базе данных. "
                       "Возвращает результат выполнения или описание ошибки.",
        "example": "Результат = Строка(ТекущаяДатаСеанса())",
    },
    "vcexecutequery": {
        "params": "querytext — текст запроса на языке запросов 1С",
        "description": "Выполняет запрос в базе и возвращает результат в текстовом "
                       "табличном виде (заголовки + строки, разделённые «|»). "
                       "Пустой результат — заголовки и «(0 строк)», ошибка — "
                       "«Ошибка запроса: …».",
        "example": "ВЫБРАТЬ ПЕРВЫЕ 10 Контрагенты.Наименование КАК Наименование, "
                   "Контрагенты.ИНН КАК ИНН ИЗ Справочник.Контрагенты КАК Контрагенты",
    },
    "vcvalidatequery": {
        "params": "querytext — текст запроса на языке запросов 1С",
        "description": "Проверяет синтаксическую корректность запроса без его "
                       "выполнения. Полезно для валидации сгенерированных "
                       "ИИ запросов перед запуском. Результат: «нет ошибок» "
                       "или описание найденной ошибки.",
        "example": "ВЫБРАТЬ Контрагенты.Ссылка ИЗ Справочник.Контрагенты КАК Контрагенты",
    },
    "vcloggetlasterror": {
        "params": "—",
        "description": "Возвращает последнюю ошибку из Журнала регистрации за "
                       "последние 24 часа (дата, событие, метаданные, данные, описание).",
        "example": "",
    },
}


def _env(name: str, default: str = "") -> str:
    """Переменная окружения; значение вида "${VAR}" из settings.yaml = не задано."""
    v = os.environ.get(name, "")
    if not v or v.startswith("${"):
        return default
    return v


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:30000]
    except Exception:
        return str(data)[:30000]


def _base_url() -> str:
    return _env("ONEC_MCP_URL", "http://127.0.0.1:8795/mcp").rstrip("/")


# ═══════════════════════════════════════════════════════════════════════
# JSON-RPC 2.0 over HTTP
# ═══════════════════════════════════════════════════════════════════════


def _rpc_call_sync(tool: str, arguments: dict) -> dict:
    """POST {"jsonrpc":"2.0","id":…,"method":"tools/call","params":{…}}.
    Возвращает {"ok":…, "text"/"error":…}."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_base_url(), data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    token = _env("ONEC_MCP_TOKEN")
    if token:
        req.add_header("Authorization", "Bearer %s" % token)

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return {"ok": False,
                "error": "HTTP %s от эндпоинта 1С: %s %s" % (e.code, e.reason, detail)}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        result = {
            "ok": False,
            "error": "эндпоинт Конструктора MCP 1С недоступен: %s (%s)"
                     % (_base_url(), reason),
        }
        result.update(_setup_hint())
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}

    try:
        data = json.loads(raw)
    except Exception:
        return {"ok": True, "text": raw[:20000]}

    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        return {"ok": False, "error": "JSON-RPC %s: %s"
                                      % (err.get("code"), err.get("message"))}

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
            text = "\n".join(parts) if parts else json.dumps(content, ensure_ascii=False)
        elif "text" in result:
            text = str(result["text"])
        elif result.get("isError"):
            text = json.dumps(result, ensure_ascii=False)
    if text is None:
        text = json.dumps(data, ensure_ascii=False)[:20000]
    return {"ok": True, "text": text[:20000]}


def _setup_hint() -> dict:
    return {
        "hint": [
            "1. Установите «Конструктор MCP серверов для 1С» в вашу базу "
            "(https://vibecoding1c.ru/#designer).",
            "2. Загрузите tools/ИнструментыДляРазработки.xml в Конструктор "
            "(справочник APA_Инструменты).",
            "3. Опубликуйте HTTP-сервис 1С (Apache + config/httpd.conf, "
            "config/default.vrd; адрес см. config/mcpconfig.json).",
            "4. Укажите окружение ONEC_MCP_URL=http://…/hs/mcp и при "
            "необходимости ONEC_MCP_TOKEN (config/settings.yaml, agents/.env).",
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
# MCP-сервер
# ═══════════════════════════════════════════════════════════════════════

app = Server("onec_designer_tools")


@app.list_tools()
async def list_tools() -> list:
    return [
        Tool(name="vc_tools_list",
             description="Описание 4 инструментов Конструктора MCP 1С "
                         "(vcexecutecode, vcexecutequery, vcvalidatequery, "
                         "vcloggetlasterror) с примерами. Безопасно.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="vcexecutecode",
             description="Выполнить код 1С (bslcode) в базе через Конструктор MCP. "
                         "ВНЕШНЕЕ ДЕЙСТВИЕ: меняет данные/конфигурацию — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
             inputSchema={"type": "object", "properties": {
                 "bslcode": {"type": "string"}}, "required": ["bslcode"]}),
        Tool(name="vcexecutequery",
             description="Выполнить запрос 1С (querytext), результат — текстовая таблица. "
                         "ВНЕШНЕЕ ДЕЙСТВИЕ (чтение данных из базы) — ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ.",
             inputSchema={"type": "object", "properties": {
                 "querytext": {"type": "string"}}, "required": ["querytext"]}),
        Tool(name="vcvalidatequery",
             description="Проверить синтаксис запроса 1С без выполнения. Безопасно.",
             inputSchema={"type": "object", "properties": {
                 "querytext": {"type": "string"}}, "required": ["querytext"]}),
        Tool(name="vcloggetlasterror",
             description="Последняя ошибка Журнала регистрации 1С за 24 часа. Безопасно.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    try:
        if name == "vc_tools_list":
            return [TextContent(type="text", text=_json_result({
                "source": "comol/mcp_designer_tools — Инструменты для разработки",
                "endpoint": _base_url(),
                "tools": VC_TOOLS_DOC,
                "install": "см. src/mcp_servers/onec_designer_tools/README.md",
            }))]

        if name == "vcexecutecode":
            code = str(arguments.get("bslcode") or "").strip()
            if not code:
                return [TextContent(type="text",
                                    text="Ошибка: пустой bslcode")]
            res = await asyncio.to_thread(_rpc_call_sync, "vcexecutecode",
                                          {"bslcode": code})
            return [TextContent(type="text", text=_json_result(res))]

        if name == "vcexecutequery":
            query = str(arguments.get("querytext") or "").strip()
            if not query:
                return [TextContent(type="text",
                                    text="Ошибка: пустой querytext")]
            res = await asyncio.to_thread(_rpc_call_sync, "vcexecutequery",
                                          {"querytext": query})
            return [TextContent(type="text", text=_json_result(res))]

        if name == "vcvalidatequery":
            query = str(arguments.get("querytext") or "").strip()
            if not query:
                return [TextContent(type="text",
                                    text="Ошибка: пустой querytext")]
            res = await asyncio.to_thread(_rpc_call_sync, "vcvalidatequery",
                                          {"querytext": query})
            return [TextContent(type="text", text=_json_result(res))]

        if name == "vcloggetlasterror":
            res = await asyncio.to_thread(_rpc_call_sync, "vcloggetlasterror", {})
            return [TextContent(type="text", text=_json_result(res))]

        return [TextContent(type="text", text="Неизвестный инструмент: %s" % name)]
    except Exception as e:
        logger.exception("onec_designer_tools tool %s failed", name)
        return [TextContent(type="text", text="Ошибка: %s" % e)]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
