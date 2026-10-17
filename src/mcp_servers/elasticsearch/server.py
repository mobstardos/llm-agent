# -*- coding: utf-8 -*-
"""Elasticsearch MCP — native Python implementation.

Использует `elasticsearch` пакет (>=8.15.0, уже в requirements.txt).

Tools:
  - es_search: поиск документов (FTS, query DSL)
  - es_index_document: индексация документа
  - es_create_index: создание индекса
  - es_delete_index: удаление индекса (destructive!)
  - es_aggregate: агрегации (avg, sum, terms, date_histogram)
  - es_list_indices: список индексов

Env:
  - ELASTICSEARCH_URL: https://user:pass@host:9200
  - ELASTICSEARCH_API_KEY: опционально, для API-ключей

mcp 1.x контракт: использует @app.list_tools() / @app.call_tool()
(см. tests/test_mcp_1x_contract.py)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from mcp import Server
from mcp.server import NotificationOptions
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)

_es_client = None


def get_client():
    """Ленивое создание Elasticsearch-клиента."""
    global _es_client
    if _es_client is not None:
        return _es_client
    from elasticsearch import Elasticsearch
    url = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
    api_key = os.getenv("ELASTICSEARCH_API_KEY")
    kwargs = {"hosts": url}
    if api_key:
        kwargs["api_key"] = api_key
    _es_client = Elasticsearch(**kwargs)
    return _es_client


async def es_search(index: str, query: str | dict = None, size: int = 10) -> dict:
    """FTS или DSL поиск документов в индексе."""
    es = get_client()
    if isinstance(query, str):
        body = {"query": {"match": {"_all": query}}, "size": size}
    else:
        body = {"query": query or {"match_all": {}}, "size": size}
    result = es.search(index=index, body=body)
    return {
        "total": result["hits"]["total"]["value"],
        "hits": [
            {"_id": h["_id"], "_source": h["_source"], "_score": h.get("_score")}
            for h in result["hits"]["hits"]
        ],
    }


async def es_index_document(index: str, document: dict, doc_id: str = None) -> dict:
    """Индексация документа в индекс (создаёт индекс если нет)."""
    es = get_client()
    result = es.index(index=index, id=doc_id, document=document, refresh=True)
    return {"result": result["result"], "_id": result["_id"], "_index": index}


async def es_create_index(index: str, mappings: dict = None) -> dict:
    """Создание индекса с опциональными mappings."""
    es = get_client()
    body = {"mappings": mappings} if mappings else {}
    result = es.indices.create(index=index, **({"body": body} if body else {}))
    return {"acknowledged": result.get("acknowledged", False), "index": index}


async def es_delete_index(index: str) -> dict:
    """Удаление индекса. DESTRUCTIVE!"""
    es = get_client()
    try:
        result = es.indices.delete(index=index)
        return {"acknowledged": result.get("acknowledged", False), "deleted": index}
    except Exception as e:
        return {"error": str(e)}


async def es_aggregate(index: str, aggs: dict, size: int = 0) -> dict:
    """Агрегации: avg, sum, terms, date_histogram, etc."""
    es = get_client()
    body = {"size": size, "aggs": aggs}
    result = es.search(index=index, body=body)
    return {"aggregations": result.get("aggregations", {})}


async def es_list_indices() -> dict:
    """Список всех индексов."""
    es = get_client()
    indices = es.indices.get(index="*")
    return {
        "indices": [
            {
                "name": name,
                "health": info.get("health"),
                "status": info.get("status"),
                "docs_count": info.get("docs", {}).get("count"),
                "size": info.get("store", {}).get("size"),
            }
            for name, info in indices.items()
            if not name.startswith(".")
        ]
    }


app = Server("elasticsearch-mcp")


@app.list_tools()
async def list_tools() -> list:
    """Список инструментов (mcp 1.x контракт)."""
    from mcp.types import Tool
    return [
        Tool(
            name="es_search",
            description="Поиск документов в Elasticsearch (FTS или DSL)",
            inputSchema={
                "type": "object",
                "properties": {
                    "index": {"type": "string", "description": "Имя индекса"},
                    "query": {"type": "string", "description": "Поисковый запрос (строка или DSL)"},
                    "size": {"type": "integer", "default": 10},
                },
                "required": ["index"],
            },
        ),
        Tool(
            name="es_index_document",
            description="Индексировать документ в Elasticsearch",
            inputSchema={
                "type": "object",
                "properties": {
                    "index": {"type": "string"},
                    "document": {"type": "object"},
                    "doc_id": {"type": "string"},
                },
                "required": ["index", "document"],
            },
        ),
        Tool(
            name="es_create_index",
            description="Создать индекс Elasticsearch",
            inputSchema={
                "type": "object",
                "properties": {
                    "index": {"type": "string"},
                    "mappings": {"type": "object"},
                },
                "required": ["index"],
            },
        ),
        Tool(
            name="es_delete_index",
            description="Удалить индекс Elasticsearch (DESTRUCTIVE!)",
            inputSchema={
                "type": "object",
                "properties": {"index": {"type": "string"}},
                "required": ["index"],
            },
        ),
        Tool(
            name="es_aggregate",
            description="Агрегации в Elasticsearch (avg, sum, terms, date_histogram)",
            inputSchema={
                "type": "object",
                "properties": {
                    "index": {"type": "string"},
                    "aggs": {"type": "object"},
                    "size": {"type": "integer", "default": 0},
                },
                "required": ["index", "aggs"],
            },
        ),
        Tool(
            name="es_list_indices",
            description="Список всех индексов Elasticsearch",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    """Выполнение инструмента по имени (mcp 1.x контракт)."""
    from mcp.types import TextContent
    import json
    try:
        if name == "es_search":
            result = await es_search(**arguments)
        elif name == "es_index_document":
            result = await es_index_document(**arguments)
        elif name == "es_create_index":
            result = await es_create_index(**arguments)
        elif name == "es_delete_index":
            result = await es_delete_index(**arguments)
        elif name == "es_aggregate":
            result = await es_aggregate(**arguments)
        elif name == "es_list_indices":
            result = await es_list_indices()
        else:
            result = {"error": f"Unknown tool: {name}"}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    except Exception as e:
        logger.exception("es tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


async def main():
    """Точка входа MCP-сервера (stdio)."""
    from mcp.server.stdio import stdio_server

    capabilities = NotificationOptions()
    init_options = InitializationOptions(
        server_name="elasticsearch-mcp",
        server_version="1.0.0",
        capabilities=capabilities,
    )
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
