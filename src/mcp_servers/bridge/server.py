"""MCP-сервер «bridge» — агенты видят браузер через расширение.

Инструменты (все read-only):
  bridge_tabs            — список открытых вкладок (снапшот расширения);
  bridge_read_tab        — прочитать текст вкладки: ставит задачу в
                           BridgeStore, расширение long-poll'ом забирает,
                           извлекает текст (chrome.scripting) и возвращает
                           результат; ждём до wait_s секунд;
  bridge_search_captures — полнотекстовый поиск по захватам страниц
                           и выделений (то, что пользователь сохранил);
  bridge_get_capture     — полный текст одного захвата.

Процесс: stdio MCP (`python -m src.mcp_servers.bridge.server`),
PROJECT_ROOT указывает на корень проекта — там data/bridge/.
Хранилище общее с API-фичей (src/bridge_store.py) — тот же код.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# PROJECT_ROOT: soft-требование — без него работаем от cwd
ROOT = Path(os.getenv("PROJECT_ROOT") or Path.cwd())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server import Server                     # noqa: E402
from mcp.server.stdio import stdio_server         # noqa: E402
from mcp.types import TextContent, Tool           # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("bridge-mcp")

from src.bridge_store import get_store            # noqa: E402

app = Server("bridge")


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2,
                          default=str)[:30000]
    except Exception:
        return str(data)[:30000]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="bridge_tabs",
            description=("Список открытых вкладок браузера пользователя "
                         "(через расширение LLM Agent Bridge). Возвращает "
                         "id, title, url и признак активной вкладки. "
                         "Возраст снапшота age_s>120 означает, что "
                         "браузер не на связи."),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="bridge_read_tab",
            description=("Прочитать содержимое открытой вкладки. Укажите "
                         "tab_id (из bridge_tabs) ИЛИ url_contains "
                         "(подстрока URL); без параметров берётся "
                         "активная вкладка. Расширение извлечёт основной "
                         "текст страницы. Ждёт ответ до wait_s секунд."),
            inputSchema={"type": "object", "properties": {
                "tab_id": {"type": ["integer", "string"],
                           "description": "id вкладки из bridge_tabs"},
                "url_contains": {"type": "string",
                                 "description": "подстрока URL"},
                "wait_s": {"type": "number",
                           "default": 60,
                           "maximum": 110},
                "max_chars": {"type": "integer", "default": 40000},
            }},
        ),
        Tool(
            name="bridge_search_captures",
            description=("Поиск по захватам из браузера (страницы и "
                         "выделенные фрагменты, сохранённые "
                         "пользователем). Ищет по тексту, заголовку и URL. "
                         "Возвращает id + превью — полный текст берите "
                         "bridge_get_capture."),
            inputSchema={"type": "object", "properties": {
                "q": {"type": "string", "description": "поисковая строка"},
                "limit": {"type": "integer", "default": 10, "maximum": 50},
            }},
        ),
        Tool(
            name="bridge_get_capture",
            description="Полный текст захвата по его id.",
            inputSchema={"type": "object", "properties": {
                "capture_id": {"type": "string"},
            }, "required": ["capture_id"]},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    store = get_store()
    args = arguments or {}

    if name == "bridge_tabs":
        return [TextContent(type="text",
                            text=_json_result(store.list_tabs()))]

    if name == "bridge_read_tab":
        job = store.put_job(
            "read_tab",
            payload={
                "tab_id": args.get("tab_id"),
                "url_contains": str(args.get("url_contains") or ""),
                "max_chars": int(args.get("max_chars") or 40000),
            },
            source="mcp")
        logger.info("bridge_read_tab: задача %s создана", job["id"])
        res = await store.wait_job(
            job["id"], timeout_s=float(args.get("wait_s") or 60))
        return [TextContent(type="text", text=_json_result(res))]

    if name == "bridge_search_captures":
        listing = store.list_captures(
            q=str(args.get("q") or ""),
            limit=int(args.get("limit") or 10))
        # превью вместо полного текста
        for c in listing["captures"]:
            c["preview"] = c["text"][:300]
            c.pop("text", None)
        return [TextContent(type="text", text=_json_result(listing))]

    if name == "bridge_get_capture":
        cap = store.get_capture(str(args.get("capture_id") or ""))
        if cap is None:
            return [TextContent(type="text",
                                text="захват не найден")]
        return [TextContent(type="text", text=_json_result(cap))]

    return [TextContent(type="text", text=f"неизвестный инструмент {name}")]


async def _main() -> None:
    logger.info("MCP 'bridge' запущен, 4 инструмента (stdio)")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
