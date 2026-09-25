"""MCP-сервер: 1С:Предприятие через OData (только чтение)."""
import json
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

app = Server("onec")

BASE_URL = os.getenv("ONEC_BASE_URL", "").rstrip("/")
USER = os.getenv("ONEC_USER", "")
PASSWORD = os.getenv("ONEC_PASSWORD", "")


def _client() -> httpx.AsyncClient:
    auth = (USER, PASSWORD) if USER else None
    return httpx.AsyncClient(base_url=BASE_URL, auth=auth, timeout=30.0)


async def _get(path: str, params: dict | None = None) -> str:
    if not BASE_URL:
        return "Ошибка: ONEC_BASE_URL не задан."
    async with _client() as client:
        try:
            r = await client.get(path, params=params, headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
            # отдаём максимум 50 записей
            if isinstance(data, dict) and "value" in data:
                data["value"] = data["value"][:50]
            return json.dumps(data, ensure_ascii=False, indent=2)[:30000]
        except httpx.HTTPStatusError as e:
            return f"HTTP {e.response.status_code}: {e.response.text[:500]}"
        except Exception as e:
            return f"Ошибка: {e}"


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="get_metadata",
             description="Список сущностей OData 1С ($metadata).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="get_catalog",
             description="Данные справочника по имени.",
             inputSchema={"type": "object",
                          "properties": {
                              "name": {"type": "string"},
                              "filter": {"type": "string"},
                              "top": {"type": "integer", "default": 20},
                          },
                          "required": ["name"]}),
        Tool(name="get_document",
             description="Данные документа по имени.",
             inputSchema={"type": "object",
                          "properties": {
                              "name": {"type": "string"},
                              "filter": {"type": "string"},
                              "top": {"type": "integer", "default": 20},
                          },
                          "required": ["name"]}),
        Tool(name="get_register",
             description="Данные регистра по имени.",
             inputSchema={"type": "object",
                          "properties": {
                              "name": {"type": "string"},
                              "filter": {"type": "string"},
                              "top": {"type": "integer", "default": 20},
                          },
                          "required": ["name"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "get_metadata":
        return [TextContent(type="text", text=await _get("/$metadata"))]

    if name in ("get_catalog", "get_document", "get_register"):
        entity = arguments["name"]
        params: dict = {"$format": "json", "$top": arguments.get("top", 20)}
        if arguments.get("filter"):
            params["$filter"] = arguments["filter"]
        # Стандартный префикс OData 1С: Catalog_XXX, Document_XXX, AccumulationRegister_XXX
        prefix = {
            "get_catalog": "Catalog_",
            "get_document": "Document_",
            "get_register": "AccumulationRegister_",
        }[name]
        if not entity.startswith(prefix):
            entity = prefix + entity
        return [TextContent(type="text", text=await _get(f"/{entity}", params))]

    return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
